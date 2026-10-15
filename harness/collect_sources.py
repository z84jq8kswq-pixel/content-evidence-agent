"""从真实网络采集「产品快照」候选材料：守 robots.txt、留原文快照、生成待确认清单。

用法：

    # 1) 采集（写快照 + 生成清单）
    python harness/collect_sources.py --list data/candidates/sources.todo.json --run-id collect-20261015

    # 2) 把已确认的候选导出成受控语料的待填清单（只生成清单，不直接改 data/corpus）
    python harness/collect_sources.py --export-review --run-id collect-20261015

为什么单独做一个入口，而不是直接往 `data/corpus/` 里写：

- **语料是评测输入，改它就等于换题**。现有 30+19 条任务、7 个实体名与 28 条事实都挂在
  旧语料上，直接改会让全部指标与测试一起对不上。所以先落到 `runs/<id>/snapshots/web/`
  作为**候选**，确认之后再走一次正式的语料替换（那是单独一步）。
- **报事实的人要能回看原文**。每条候选都留：原文快照、取回时间、URL、SHA-256，
  抽取结构化事实时逐条对照，出处可核。

合规与礼貌（硬约束）：

- 取回前检查 `robots.txt`，Disallow 的路径**不取**，并记进清单的 `robots` 字段；
- 同一主机两次请求之间 sleep（默认 1.5 秒），单次最多取 N 个 URL；
- 只取 http／https，拒绝内网与元数据地址（复用 `tools/websearch.check_url`）；
- 不执行 JavaScript，不做站点登录与反爬绕过；取不到就如实记失败，不算"没有材料"。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.tools import (  # noqa: E402
    FAILURE_STATUSES,
    SnapshotStore,
    build_fetcher,
    check_url,
    load_web_config,
)

#: 同一主机两次请求之间的最小间隔。官方站点尤其要克制。
DEFAULT_DELAY_S = 1.5

#: 单次运行的取回上限，防止一次点下去打出几十个请求。
DEFAULT_MAX_URLS = 12

ROBOTS_ALLOWED = "allowed"
ROBOTS_DISALLOWED = "disallowed"
ROBOTS_MISSING = "no-robots"
ROBOTS_UNREACHABLE = "robots-unreachable"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- robots.txt -------------------------------------------------------


def robots_url_for(target: str) -> str:
    parts = urllib.parse.urlsplit(target)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def _parse_robots(text: str, user_agent: str) -> list[str]:
    """抽出适用于我们的 Disallow 规则。

    只实现保守子集：取 `User-agent: *` 与匹配当前 UA 的分组，忽略 Allow 的优先级细节——
    遇到规则写复杂时宁可**不取**（保守方向与项目其他取舍一致）。
    """

    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    agent = user_agent.lower()

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if current is None or current.get("rules"):
                current = {"agents": [], "rules": []}
                groups.append(current)
            current["agents"].append(value.lower())
        elif field == "disallow" and current is not None:
            current["rules"].append(value)

    disallow: list[str] = []
    for group in groups:
        agents = group["agents"]
        if any(a == "*" or a in agent or agent in a for a in agents if a):
            disallow.extend(rule for rule in group["rules"] if rule)
    return disallow


def check_robots(
    target: str,
    *,
    user_agent: str,
    fetcher,
    timeout_s: float = 8.0,
) -> tuple[str, str]:
    """返回 (状态, 说明)。状态见 ROBOTS_* 常量。

    取 robots.txt 也走"标准库失败就换系统 curl"的同一条路径：部分官方站点的 TLS
    只认某些协商方式，用标准库取不到 robots.txt，会把**本来可以抓的站**误判成禁止。
    取不到 robots.txt 时保守处理（不抓），但会如实说明原因，便于人工复核。
    """

    url = robots_url_for(target)
    body = ""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read(200_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return ROBOTS_MISSING, "站点没有 robots.txt，按允许处理"
        return ROBOTS_UNREACHABLE, f"robots.txt 返回 HTTP {exc.code}，保守起见不取"
    except Exception as exc:  # noqa: BLE001 - 网络与 TLS 问题一律保守处理
        # 换系统 curl 再试一次（与网页获取同一条回退策略）
        from evidence_agent.tools.websearch import fetch_text_via_curl, find_curl

        curl = getattr(fetcher, "_curl", None) or find_curl()
        if curl:
            ok, payload = fetch_text_via_curl(
                curl, url, user_agent=user_agent, timeout_s=timeout_s
            )
            if ok:
                body = payload
            elif payload in ("HTTP 404", "HTTP 410"):
                # 404 的意思是"这个站没有 robots.txt"，不是"禁止抓取"。
                # 把它当成禁止会把大量站点误判成不可抓（实测：cqc、gsxt 都栽在这里）。
                return ROBOTS_MISSING, "站点没有 robots.txt（HTTP 404），按允许处理"
            else:
                return (
                    ROBOTS_UNREACHABLE,
                    f"robots.txt 取不到（{type(exc).__name__}；curl：{payload}），保守起见不取",
                )
        else:
            return ROBOTS_UNREACHABLE, f"robots.txt 取不到（{type(exc).__name__}），保守起见不取"

    if not body.strip():
        return ROBOTS_MISSING, "robots.txt 为空，按允许处理"

    path = urllib.parse.urlsplit(target).path or "/"
    for rule in _parse_robots(body, user_agent):
        if rule == "/" or path.startswith(rule):
            return ROBOTS_DISALLOWED, f"robots.txt 禁止：Disallow: {rule}"
    return ROBOTS_ALLOWED, "robots.txt 未禁止该路径"


# --- 采集 -------------------------------------------------------------


def load_todo(path: Path) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("候选清单必须是 JSON 对象")
    items = payload.get("sources") or []
    if not isinstance(items, list):
        raise ValueError("sources 必须是数组")
    return payload, [dict(item) for item in items]


def collect(
    root: Path,
    run_id: str,
    items: Sequence[Mapping[str, Any]],
    *,
    delay_s: float = DEFAULT_DELAY_S,
    max_urls: int = DEFAULT_MAX_URLS,
    respect_robots: bool = True,
    fetcher=None,
) -> dict[str, Any]:
    """按清单取回材料。返回清单载荷（含每条的 robots 结论、状态、快照与哈希）。"""

    config = load_web_config(root)
    fetcher = fetcher or build_fetcher(config)
    store = SnapshotStore(root / "runs" / run_id, subdir="snapshots/web")

    rows: list[dict[str, Any]] = []
    last_host = ""
    taken = 0

    for item in items:
        url = str(item.get("url", "")).strip()
        row: dict[str, Any] = {
            "label": str(item.get("label", "")),
            "kind": str(item.get("kind", "")),
            "entity": str(item.get("entity", "")),
            "metrics": list(item.get("metrics") or []),
            "url": url,
            "note": str(item.get("note", "")),
            "robots": "",
            "robots_note": "",
            "status": "",
            "fetched_at": "",
            "snapshot_meta": "",
            "raw_sha256": "",
            "text_path": "",
            "text_chars": 0,
            "error": "",
        }

        if taken >= max_urls:
            row.update(status="SKIPPED", error=f"本次已达取回上限（{max_urls}）")
            rows.append(row)
            continue

        check = check_url(url, resolver=fetcher.resolver)
        if not check.ok:
            row.update(status="UNSAFE_URL", error=check.reason)
            rows.append(row)
            continue

        host = urllib.parse.urlsplit(url).netloc.lower()
        if respect_robots:
            state, note = check_robots(url, user_agent=fetcher.user_agent, fetcher=fetcher)
            row["robots"], row["robots_note"] = state, note
            if state in (ROBOTS_DISALLOWED, ROBOTS_UNREACHABLE):
                row.update(status="ROBOTS_BLOCKED", error=note)
                rows.append(row)
                continue

        if host and host == last_host and delay_s > 0:
            time.sleep(delay_s)

        result = fetcher.fetch(url)
        taken += 1
        last_host = host
        if result.raw_bytes:
            # 文件名优先用 URL 主机名（可读、可定位），中文标签会被 safe_segment 清空，
            # 直接拿它当文件名会退化成 page / page-2 这种无法辨认的名字。
            from evidence_agent.tools.websearch import safe_segment

            base = safe_segment(str(item.get("label") or ""), fallback="")
            if not base:
                base = safe_segment(host.split(":")[0], fallback=f"page-{taken}")
            result = store.save(result, base=base)
        row.update(
            status=result.status,
            fetched_at=result.fetched_at,
            snapshot_meta=result.snapshot_meta_path,
            raw_sha256=result.raw_sha256,
            text_chars=len(result.text),
            error=result.error,
        )
        rows.append(row)

    ok = [row for row in rows if row["status"] == "OK"]
    return {
        "run_id": run_id,
        "created_at": now_iso(),
        "purpose": "真实产品快照候选材料。确认前不进入 data/corpus/，不参与任何判定。",
        "delay_s": delay_s,
        "max_urls": max_urls,
        "respect_robots": respect_robots,
        "counts": {
            "total": len(rows),
            "ok": len(ok),
            "blocked": len([r for r in rows if r["status"] == "ROBOTS_BLOCKED"]),
            "failed": len([r for r in rows if r["status"] in FAILURE_STATUSES]),
        },
        "items": rows,
    }


def write_collection(root: Path, run_id: str, payload: Mapping[str, Any]) -> Path:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "collection.json"
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def render_review_markdown(payload: Mapping[str, Any], store: SnapshotStore) -> str:
    """生成给人逐条核对的清单：每条材料的元信息 + 原文里出现的数值/日期线索。"""

    lines = [
        f"# 候选材料确认清单：{payload.get('run_id', '')}",
        "",
        f"- 生成时间：{payload.get('created_at', '')}",
        f"- 取材说明：{payload.get('purpose', '')}",
        f"- robots.txt 检查：{'开启' if payload.get('respect_robots') else '关闭'}",
        f"- 同一主机间隔：{payload.get('delay_s')} 秒；本次上限 {payload.get('max_urls')} 条",
        "",
        "> 用法：逐条核对下面每份材料的**数值、单位、测试条件、日期、发布者**，"
        "确认后我才把它们写成 `facts.json` 的记录。**原文片段不进公开仓库**，只留出处与哈希。",
        "",
    ]
    counts = payload.get("counts", {})
    lines.append(
        f"本次结果：共 {counts.get('total', 0)} 条 → 成功 {counts.get('ok', 0)}，"
        f"被 robots 拦下 {counts.get('blocked', 0)}，失败 {counts.get('failed', 0)}。"
    )
    lines.append("")

    for index, row in enumerate(payload.get("items", []), start=1):
        lines.append(f"## {index}. {row.get('label') or row.get('url')}")
        lines.append("")
        lines.append(f"- 规划用途：{row.get('kind') or '—'}｜对应实体：{row.get('entity') or '—'}")
        lines.append(f"- 想取的指标：{'、'.join(row.get('metrics') or []) or '—'}")
        lines.append(f"- URL：{row.get('url')}")
        lines.append(f"- 状态：`{row.get('status')}`" + (f"（{row.get('error')}）" if row.get("error") else ""))
        if row.get("robots"):
            lines.append(f"- robots.txt：{row.get('robots')}（{row.get('robots_note')}）")
        if row.get("fetched_at"):
            lines.append(f"- 取回时间：{row.get('fetched_at')}")
        if row.get("snapshot_meta"):
            lines.append(f"- 快照：`runs/{payload.get('run_id')}/{row.get('snapshot_meta')}`")
            lines.append(f"- SHA-256（原始字节）：`{row.get('raw_sha256')}`")
        lines.append("")
        if row.get("status") == "OK" and row.get("text_path"):
            lines.append("正文片段（**仅本地查看，不入仓库**）：")
            lines.append("")
            lines.append("```text")
            lines.append(row["text_path"])
            lines.append("```")
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 确认之后我要做的事")
    lines.append("")
    lines.append("1. 把确认过的数值写成 `data/corpus/facts.json` 的记录（entity/metric/value/unit/period/conditions/source_id/date）；")
    lines.append("2. 在 `data/corpus/sources.json` 里补对应资料条目：`kind`（来源类型）、`tier`、`date`、`publisher`，取样方式与资质按实际填；")
    lines.append("3. 实体别名表补真实型号；")
    lines.append("4. 重做受影响的任务集，重建评测基线，并在文档里写明「与旧基线不可比」。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实产品快照候选材料采集")
    parser.add_argument("--list", help="候选清单 JSON（含 sources 数组）")
    parser.add_argument("--run-id", required=True, help="运行目录编号")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_S, help="同一主机请求间隔秒数")
    parser.add_argument("--max-urls", type=int, default=DEFAULT_MAX_URLS, help="本次取回上限")
    parser.add_argument("--no-robots", action="store_true", help="跳过 robots.txt 检查（不建议）")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args(argv)

    if not args.list:
        parser.error("需要 --list 指向候选清单")

    payload_in, items = load_todo(Path(args.list))
    if not items:
        print("候选清单里没有 sources", file=sys.stderr)
        return 2

    payload = collect(
        ROOT,
        args.run_id,
        items,
        delay_s=args.delay,
        max_urls=args.max_urls,
        respect_robots=not args.no_robots,
    )
    # 生成清单时把正文写进 markdown 只是为了本地核对，正文本身来自快照文件
    store = SnapshotStore(ROOT / "runs" / args.run_id, subdir="snapshots/web")
    for row in payload["items"]:
        if row.get("snapshot_meta"):
            meta = store.load_meta(Path(row["snapshot_meta"]).stem)
            text_file = Path(store.root) / meta["files"]["text"]
            row["text_path"] = (
                text_file.read_text(encoding="utf-8")[:1500] if text_file.exists() else ""
            )

    path = write_collection(ROOT, args.run_id, payload)
    review = render_review_markdown(payload, store)
    review_path = path.parent / "collection-review.md"
    review_path.write_text(review, encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(review)
    print(f"\n清单：{path.relative_to(ROOT)}")
    print(f"待确认：{review_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

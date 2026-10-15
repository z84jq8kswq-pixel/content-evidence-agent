"""把浏览器另存的规格页 HTML 导入成「候选事实」，供人工确认后写入语料。

为什么要走"另存 HTML"这条路（而不是直接抓网页）：

- 真实电商与官网的规格页基本是 **JS 渲染或接口下发**，静态 HTML 里没有参数；
  本项目刻意不执行 JavaScript，所以直接抓只能拿到导航壳（实测：飞利浦专题页
  6000 字正文里只有一个 `70%`，没有规格数字）。
- 另存 HTML 是浏览器**渲染后**的 DOM，规格数字就在里面；
- 而且每个数值都由人亲眼见过，来源可核——这比"脚本抓下来直接入库"更符合
  本项目的证据纪律。

用法：

    # 1) 把网页另存到 data/imports/ 下，文件名标明品类，例如：
    #    data/imports/01-充电宝.html
    #    data/imports/02-电动牙刷.html
    # 2) 导入：
    python harness/import_spec.py
    # 3) 看清单，逐条确认指标名：
    #    runs/import-<时间>/review.md      ← 给人看的确认清单
    #    runs/import-<时间>/candidates.json ← 机器可读的候选事实

**这一步不写入 `data/corpus/`。** 确认之前它只是候选；写入语料是单独一步，
因为语料一改，评测指标与现有任务集全部要重建基线。

判定口径参考 `文档库/02-判定规则/来源分级.md`：导入的每条候选都带
`kind`（来源类型）、`tier`、`date`、`publisher` 四个待确认字段——它们决定这条
材料过不过门槛，不能由脚本猜，必须人工填。
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.tools.websearch import (  # noqa: E402
    decode_body,
    extract_text,
    guess_charset,
)

DEFAULT_IMPORT_DIR = ROOT / "data" / "imports"

#: 从文件名里认品类：`01-充电宝.html` → 品类「充电宝」。前缀数字用来排序。
FILE_NAME = re.compile(r"^\s*(?:\d+[-_\s]*)?(?P<name>.+?)\s*\.(?:html?|htm|txt|md)$", re.IGNORECASE)

#: 页面里的来源线索
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
CANONICAL = re.compile(
    r"""<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']""", re.IGNORECASE
)
OG_URL = re.compile(
    r"""<meta[^>]+property=["']og:url["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE
)
META_PUBLISHER = re.compile(
    r"""<meta[^>]+name=["'](?:author|publisher|og:site_name)["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE,
)
#: 发布时间。两种常见写法都要认：
#:   ① `<meta property="article:published_time" content="2026-03-11">`（字段名与日期在不同属性里）
#:   ② `"datePublished": "2026-03-11"`（JSON-LD）
PUBLISHED_TIME_META = re.compile(
    r"""<meta[^>]*(?:property|name)=["'](?:article:published_time|publish(?:ed)?[_-]?time|datePublished|og:published_time)["'][^>]*content=["']([0-9]{4}-[0-9]{2}-[0-9]{2})""",
    re.IGNORECASE,
)
PUBLISHED_TIME_META_REVERSED = re.compile(
    r"""<meta[^>]*content=["']([0-9]{4}-[0-9]{2}-[0-9]{2})["'][^>]*(?:property|name)=["'](?:article:published_time|publish(?:ed)?[_-]?time|datePublished)["']""",
    re.IGNORECASE,
)
PUBLISHED_TIME_JSON = re.compile(
    r"""["']datePublished["']\s*:\s*["']([0-9]{4}-[0-9]{2}-[0-9]{2})""", re.IGNORECASE
)


def _published_time(text: str) -> str:
    for pattern in (PUBLISHED_TIME_META, PUBLISHED_TIME_META_REVERSED, PUBLISHED_TIME_JSON):
        found = pattern.search(text)
        if found:
            return found.group(1)
    return ""

#: 带单位的数值。单位表刻意收窄到"本项目判定用得上的量"，
#: 不收"元/个/件"这类没有可比性的词，否则清单会被价格与数量淹没。
UNIT = (
    r"mAh|Wh|kWh|W|V|A|Hz|"
    r"小时|分钟|秒|天|个月|年|次|"
    r"毫升|ml|ML|升|L|克|g|G|kg|毫克|mg|微克|ug|μg|"
    r"%|％|℃|度|"
    r"英寸|寸|cm|mm|m|"
    r"dB|分贝|Pa|kPa|Mpa|"
    r"GPD|加仑|PPB|ppb|TDS"
)
NUMBER_WITH_UNIT = re.compile(
    rf"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>{UNIT})(?![A-Za-z\u4e00-\u9fff])"
)
#: 比例写法（1:1、5:1）——净水器废水比、功效比是这个形态
RATIO = re.compile(r"\d+\s*[:：]\s*\d+")
#: "字母+数字"的等级/规格写法：IPX4、IP67、HEPA13、4K。这类既不是纯数值也没有单位，
#: 但它是实打实可比对的规格（IPX4 vs IPX7 就是冲突），漏掉会让认证/防护类主张无从判定。
ALNUM_GRADE = re.compile(r"\b(?P<code>IP[X0-9]{2,3}|HEPA\s?\d{1,2}|[0-9]K)\b", re.IGNORECASE)

#: 上下文窗口（字符）。**按行取**：规格页通常一行一条"指标：数值"，
#: 跨行取上下文会把上一行的指标名称圈进来，导致 `74Wh` 被猜成"容量"
#: （实测踩到）。行内取不到时再向左右各扩一点。
CONTEXT_RADIUS = 30


def nearest_label(text: str, start: int, *, window: int = 14) -> str:
    """找数值**紧邻前面**的指标标签，返回规范指标名；找不到返回空串。

    规格页常常把多条挤在一行（`电池容量:20000mAh 额定能量:74Wh`），
    只按行取上下文会把前一个标签当成当前数值的指标（实测：74Wh 被判成"容量"）。
    所以先看紧邻的标签，看得到就用它，看不到才退回上下文猜。
    """

    left = text[max(0, start - window) : start]
    best: tuple[int, str] = (-1, "")
    for metric, hints in METRIC_HINTS:
        for hint in hints:
            position = left.rfind(hint)
            if position < 0:
                continue
            end = position + len(hint)
            # 标签后面允许跟冒号/等号/空格，不允许再夹着别的数字或句号
            tail = left[end:]
            if re.search(r"[0-9。；;]", tail):
                continue
            if end > best[0]:
                best = (end, metric)
    return best[1]


def context_of(text: str, start: int, end: int) -> str:
    """取数值所在**行**的上下文，压成一行。"""

    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    candidate = text[line_start:line_end].strip()
    if len(candidate) < 12:
        # 行太短（例如数值独占一行）时向左右各扩一点，帮助判断它在说什么
        left = max(0, start - CONTEXT_RADIUS)
        right = min(len(text), end + CONTEXT_RADIUS)
        candidate = text[left:right]
    return normalize(candidate.replace("\n", " "))


#: 指标词：用来把数值和"它大概是哪个指标"对上。命中不了就留空，由人工填。
METRIC_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("容量", ("容量", "电池容量", "电芯容量")),
    ("额定能量", ("额定能量", "能量", "Wh")),
    ("快充功率", ("快充", "充电功率", "输出功率", "输入功率", "功率")),
    ("续航", ("续航", "使用时间", "待机")),
    ("降噪续航", ("降噪续航", "开启降噪")),
    ("防水等级", ("防水", "IPX", "IP6", "IP5")),
    ("滤芯寿命", ("滤芯寿命", "滤芯", "寿命")),
    ("通量", ("通量", "GPD", "加仑")),
    ("废水比", ("废水比", "净废水比")),
    ("烟酰胺浓度", ("烟酰胺", "浓度", "含量")),
    ("含铁量", ("含铁量", "铁")),
    ("水箱容量", ("水箱", "容量")),
    ("加湿量", ("加湿量", "雾量")),
    ("重量", ("重量", "净重", "毛重")),
    ("尺寸", ("尺寸", "长宽高", "外形尺寸", "产品尺寸", "包装尺寸")),
    ("噪声", ("噪声", "噪音", "dB")),
    ("适用面积", ("适用面积", "面积")),
    ("电压", ("电压", "额定电压")),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(text: str) -> str:
    """全角转半角、压空白。规格页里全角数字与空格混用很常见。"""

    folded = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[ \t\u00a0]+", " ", folded).strip()


# --- 读文件 -----------------------------------------------------------


def read_page(path: Path) -> tuple[str, str]:
    """读一个另存的页面，返回 (正文文本, 编码)。自动识别 UTF-8／GBK。

    HTML 走与抓网页同一套抽取（去脚本样式、去标签、反转义），
    纯文本文件原样返回——两种输入得到同一种"正文"，后续抽取只有一份逻辑。
    """

    raw = path.read_bytes()
    charset = guess_charset(raw)
    body, charset = decode_body(raw, "")
    if path.suffix.lower() in (".txt", ".md"):
        return body, charset
    text, _title = extract_text(body)
    return text, charset


def page_meta(path: Path, body: str) -> dict[str, str]:
    """从另存的 HTML 里抽来源线索。抽不到就留空，绝不编。"""

    head = body[:20000]
    title = ""
    match = TITLE.search(head)
    if match:
        title = normalize(html_module.unescape(re.sub(r"<[^>]+>", "", match.group(1))))
    url = ""
    for pattern in (CANONICAL, OG_URL):
        found = pattern.search(head)
        if found:
            url = found.group(1).strip()
            break
    publisher = ""
    found = META_PUBLISHER.search(head)
    if found:
        publisher = normalize(html_module.unescape(found.group(1)))
    date = _published_time(head)
    return {
        "file": path.name,
        "title": title,
        "url": url,
        "publisher": publisher,
        "date": date,
        "sha256": sha256_hex(path.read_bytes()),
        "charset": "",
    }


def category_of(path: Path) -> str:
    match = FILE_NAME.match(path.name)
    return match.group("name") if match else path.stem


# --- 抽数值 -----------------------------------------------------------


def guess_metric(context: str) -> str:
    """按上下文猜指标名。猜不出返回空串——猜错比留空更糟。"""

    for metric, hints in METRIC_HINTS:
        if any(hint in context for hint in hints):
            return metric
    return ""


def extract_values(text: str, *, limit: int = 60) -> list[dict[str, Any]]:
    """抽出带单位的数值，带上上下文与指标名（含确定程度）。

    `certainty` 三档，写清楚是为了让确认清单一眼能看出哪些要重点核对：

    - `label`：数值紧跟在指标标签后面（`额定能量:74Wh`），最可信；
    - `context`：靠上下文里的指标词猜出来的，需要人工看；
    - `none`：猜不出来，必须人工填——**不猜**比猜错好。
    """

    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(value_text: str, unit: str, start: int, end: int, value: str) -> None:
        context = context_of(text, start, end)
        key = (value_text, context)
        if key in seen:
            return
        seen.add(key)
        label = nearest_label(text, start)
        if label:
            metric, certainty = label, "label"
        else:
            guessed = guess_metric(context)
            metric, certainty = guessed, ("context" if guessed else "none")
        found.append(
            {
                "value": value,
                "unit": unit,
                "text": value_text,
                "metric": metric,
                "metric_certainty": certainty,
                "context": context,
            }
        )

    for match in NUMBER_WITH_UNIT.finditer(text):
        add(
            f"{match.group('value')}{match.group('unit')}",
            match.group("unit"),
            match.start(),
            match.end(),
            match.group("value").replace(",", ""),
        )
        if len(found) >= limit:
            break
    # 比例写法（废水比 1:1）单独收
    for match in RATIO.finditer(text):
        add(match.group(0), "", match.start(), match.end(), match.group(0))
    # 字母+数字等级（IPX4／HEPA13）单独收
    for match in ALNUM_GRADE.finditer(text):
        code = match.group("code").upper().replace(" ", "")
        add(code, "", match.start(), match.end(), code)
    return found


def load_facts_entities(root: Path) -> dict[str, list[str]]:
    """现有语料里的实体 → 别名。用来提示"这条候选准备替换哪个示例实体"。"""

    path = root / "data" / "corpus" / "sources.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    aliases = payload.get("aliases") or {}
    return {str(k): [str(a) for a in v] for k, v in aliases.items()}


def import_dir(directory: Path, facts_entities: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    files = sorted(
        [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in (".html", ".htm", ".txt", ".md")],
        key=lambda p: p.name,
    )
    pages: list[dict[str, Any]] = []
    for path in files:
        text, charset = read_page(path)
        meta = page_meta(path, path.read_text(encoding="utf-8", errors="replace"))
        meta["charset"] = charset
        pages.append(
            {
                "category": category_of(path),
                "meta": meta,
                "chars": len(text),
                "values": extract_values(text),
            }
        )
    return {
        "imported_at": now_iso(),
        "directory": str(directory),
        "existing_entities": {k: list(v) for k, v in facts_entities.items()},
        "pages": pages,
    }


# --- 输出 -------------------------------------------------------------


def render_review(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# 规格页导入确认清单：{payload.get('imported_at', '')}",
        "",
        f"- 导入目录：`{payload.get('directory', '')}`",
        f"- 页面数：{len(payload.get('pages', []))}",
        "",
        "> 这个清单里的每条都要你确认：**它是什么指标**、**来源类型与等级**。",
        "> 确认之前不会写入 `data/corpus/`；`kind` 与 `tier` 决定这条材料过不过门槛，",
        "> 脚本不替你猜（口径见 `文档库/02-判定规则/来源分级.md`）。",
        "",
        "填写方式：在下面每条的 `指标` 与 `来源类型／等级` 上改成实际值（或直接告诉我）。",
        "",
    ]
    for index, page in enumerate(payload.get("pages", []), start=1):
        meta = page.get("meta", {})
        lines.append(f"## {index}. {page.get('category') or '未命名'}（{meta.get('file', '')}）")
        lines.append("")
        lines.append(f"- 页面标题：{meta.get('title') or '（未抽到，请手填）'}")
        lines.append(f"- 来源 URL：{meta.get('url') or '（未抽到，请手填）'}")
        lines.append(f"- 发布者：{meta.get('publisher') or '（未抽到，请手填）'}")
        lines.append(f"- 页面日期：{meta.get('date') or '（未抽到，请手填）'}")
        lines.append(f"- 编码：{meta.get('charset') or '—'}｜正文字符数：{page.get('chars')}")
        lines.append(f"- 文件 SHA-256：`{meta.get('sha256', '')}`")
        lines.append("")
        values = page.get("values") or []
        if not values:
            lines.append("**没有抽到带单位的数值**——可能是页面是图片、或者规格在 JS 里没被另存下来。")
            lines.append("")
            continue
        lines.append("| # | 数值 | 指标（请确认） | 确定程度 | 出现位置上下文 |")
        lines.append("|---|---|---|---|---|")
        certainty_label = {
            "label": "标签紧邻（可采信）",
            "context": "上下文推断（请核对）",
            "none": "**未识别（必须填）**",
        }
        for i, item in enumerate(values, start=1):
            metric = item.get("metric") or "？（请填）"
            context = str(item.get("context", "")).replace("|", "｜")
            certainty = certainty_label.get(str(item.get("metric_certainty")), "")
            lines.append(f"| {i} | `{item.get('text')}` | {metric} | {certainty} | {context} |")
        lines.append("")
        # 只汇总"必须人工填"的，方便一眼看完
        unknown = [item for item in values if item.get("metric_certainty") == "none"]
        if unknown:
            lines.append(f"需要你补指标名的有 {len(unknown)} 条：" + "、".join(
                f"`{item['text']}`" for item in unknown
            ))
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 确认之后我要做的事")
    lines.append("")
    lines.append("1. 把确认过数值写成 `data/corpus/facts.json` 的记录；")
    lines.append("2. 在 `data/corpus/sources.json` 里补资料条目（kind／tier／date／publisher／text 摘要）；")
    lines.append("3. 真实型号补进实体别名表；")
    lines.append("4. 重做受影响的任务集、重建基线，并在文档里写明「与旧基线不可比」。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入另存的规格页，生成候选事实清单")
    parser.add_argument("--dir", default=str(DEFAULT_IMPORT_DIR), help="另存 HTML 的目录")
    parser.add_argument("--out", default=None, help="输出运行目录编号，默认 import-<时间>")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    if not directory.exists() or not directory.is_dir():
        print(f"目录不存在：{directory}", file=sys.stderr)
        print("请先把网页另存到这个目录，再运行本命令。", file=sys.stderr)
        return 2

    run_id = args.out or f"import-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    from evidence_agent.runio import plan_run_dir

    try:
        run_dir = plan_run_dir(run_id, ROOT)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # plan_run_dir 只负责校验编号与拒绝覆盖，建目录由调用方负责
    run_dir.mkdir(parents=True, exist_ok=True)

    payload = import_dir(directory, load_facts_entities(ROOT))
    payload["run_id"] = run_id
    (run_dir / "candidates.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    review = render_review(payload)
    (run_dir / "review.md").write_text(review, encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(review)
    print(f"\n清单：runs/{run_id}/review.md")
    print(f"候选：runs/{run_id}/candidates.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

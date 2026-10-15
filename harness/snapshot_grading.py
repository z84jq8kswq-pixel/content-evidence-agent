"""抓取来源分级的修改前／修改后对照快照。

用途：本仓库没有可用的 git（本机没有 git.exe，`.git` 也没有任何提交），
所以改动前的可回溯基线靠这个脚本 + 目录备份建立。它抓三类东西：

1. **24 份语料的分级结果**：身份、取样、资质、门槛结论、展示权重、分级依据；
2. **排序结果**：三处需要"挑最权威来源"的地方（`assessor.best`、
   `assessor.compare_numeric`、`highlight.authoritative_value`）在真实语料上的选择；
3. **既有评测指标**：26 条缺口任务与 30 条评测任务的逐项指标。

用法：

    python harness/snapshot_grading.py --label before
    python harness/snapshot_grading.py --label after
    python harness/snapshot_grading.py --diff before after

抓指标那一步会跑评测（离线、零成本、确定性），产物写进 runs/ 下带标签的运行编号，
不会覆盖任何已有运行。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SNAPSHOT_DIR = ROOT / "harness" / "baseline"

#: 评测入口与运行编号前缀。跑出来的运行只是对照用的临时基线，
#: 不占用既有编号（既有产物一个都不动）。
#: 注意两个入口的参数名不同：缺口评测用 --run-id，v1/v2 评测用 --out。
RUNS = [
    ("gap", ["harness/run_agent_eval.py", "--run-id", "{run_id}", "--force", "--json"]),
    (
        "eval30",
        [
            "harness/run_eval.py",
            "--adapter",
            "stub",
            "--pipeline",
            "v2",
            "--out",
            "{run_dir}",
        ],
    ),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def corpus_grading() -> list[dict]:
    """24 份语料的三个维度 + 门槛结论 + 展示权重 + 分级依据。"""

    from evidence_agent.grading import grade_source
    from evidence_agent.tools import LocalCorpus

    corpus = LocalCorpus.from_dir(ROOT / "data" / "corpus")
    rows: list[dict] = []
    for doc in corpus.docs.values():
        grade = grade_source(
            kind=doc.kind,
            sampling=doc.sampling,
            accredited=doc.accredited,
            tier=doc.tier,
        )
        rows.append(
            {
                "id": doc.id,
                "title": doc.title,
                "kind": doc.kind,
                "sampling": doc.sampling,
                "accredited": doc.accredited,
                "tier": doc.tier,
                "identity": grade.identity,
                "strong": grade.strong,
                "weight": round(grade.weight, 4),
                "reasons": list(grade.reasons),
            }
        )
    return rows


def ranking_probe() -> dict:
    """三处"挑最权威来源"的结果。

    用真实语料里存在竞争的指标来问，而不是造数据：
    同一指标下有多条不同来源的记录时，看谁被选中。
    """

    from evidence_agent.highlight import authoritative_value
    from evidence_agent.runtime.assessor import compare_numeric
    from evidence_agent.tools import LocalCorpus

    corpus = LocalCorpus.from_dir(ROOT / "data" / "corpus")
    facts = corpus.enriched_facts()

    # ① highlight.authoritative_value：按指标分组，看每组选中哪条
    by_metric: dict[str, list[dict]] = {}
    for row in facts:
        unit = str(row.get("unit", "")).strip()
        if not unit:
            continue
        by_metric.setdefault(str(row.get("metric", "")), []).append(row)
    picks: list[dict] = []
    for metric, rows in sorted(by_metric.items()):
        if len(rows) < 2:
            continue
        unit = str(rows[0].get("unit", ""))
        found = authoritative_value(rows, unit)
        picks.append(
            {
                "metric": metric,
                "unit": unit,
                "candidates": [
                    {"source_id": str(r.get("source_id")), "value": str(r.get("value")),
                     "tier": str(r.get("tier")), "date": str(r.get("date"))}
                    for r in rows
                ],
                "picked": str(found[1].get("source_id")) if found else None,
                "picked_value": str(found[1].get("value")) if found else None,
            }
        )

    # ② compare_numeric：对每条缺口任务的主张问一次，看用的是哪条来源
    decisions: list[dict] = []
    gap = json.loads((ROOT / "data" / "gap_tasks.json").read_text(encoding="utf-8"))
    from evidence_agent.tools.base import Evidence
    from evidence_agent.runtime.assessor import assess

    for case in gap.get("cases", []):
        items: list[Evidence] = []
        for row in facts:
            items.append(
                Evidence(
                    source_id=str(row.get("source_id", "")),
                    tier=str(row.get("tier", "")),
                    date=str(row.get("date", "")),
                    text=f"{row.get('entity')} {row.get('metric')}：{row.get('value')}{row.get('unit', '')}",
                    meta=dict(row),
                )
            )
        for claim in case.get("expected_claims", []):
            assessment = assess(items, claim_metric="", claim_entity="")
            best = assessment.best()
            numeric = compare_numeric(claim.get("text", ""), items)
            decisions.append(
                {
                    "case_id": case.get("id"),
                    "claim": claim.get("text"),
                    "best": best.source_id if best else None,
                    "numeric_source": numeric[2][0] if numeric else None,
                    "numeric_verdict": numeric[0] if numeric else None,
                }
            )

    # ③ assessor.best：同单位同指标、跨身份的候选，逐组记录选择
    best_calls: list[dict] = []
    by_group: dict[tuple[str, str], list[dict]] = {}
    for row in facts:
        key = (str(row.get("metric", "")), str(row.get("unit", "")))
        by_group.setdefault(key, []).append(row)
    for (metric, unit), rows in sorted(by_group.items()):
        if len(rows) < 2:
            continue
        items = [
            Evidence(
                source_id=str(r.get("source_id", "")),
                tier=str(r.get("tier", "")),
                date=str(r.get("date", "")),
                text="",
                meta=dict(r),
            )
            for r in rows
        ]
        assessment = assess(items, claim_metric=metric, claim_entity=str(rows[0].get("entity", "")))
        best = assessment.best()
        best_calls.append(
            {
                "metric": metric,
                "unit": unit,
                "candidates": [str(r.get("source_id")) for r in rows],
                "best": best.source_id if best else None,
                "sufficient": assessment.sufficient,
                "flags": list(assessment.flags),
                "reason": assessment.reason,
                "decisive": list(assessment.decisive_sources),
            }
        )

    return {"authoritative_value": picks, "compare_numeric": decisions, "assessor_best": best_calls}


def run_evaluations(label: str) -> dict:
    """跑 26 条缺口任务与 30 条评测任务，抓逐项指标。"""

    started = now_iso()
    captured: dict[str, dict] = {}
    for name, template in RUNS:
        run_id = f"baseline-{label}-{name}"
        run_dir = f"runs/{run_id}"
        argv = [arg.format(run_id=run_id, run_dir=run_dir) for arg in template]
        proc = subprocess.run(
            [sys.executable, *argv],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        results_path = ROOT / "runs" / run_id / "results.json"
        metrics: dict = {}
        cases: list[dict] = []
        if results_path.exists():
            payload = json.loads(results_path.read_text(encoding="utf-8"))
            # 两类运行的指标位置不同：v3 缺口评测在 metrics，v1/v2 评测在 summary。
            metrics = payload.get("metrics") or payload.get("summary") or {}
            if name == "gap":
                # 主张级明细：指标相同但具体主张的判定变了也要能看出来
                for case in payload.get("cases", []):
                    for claim in case.get("claims", []):
                        cases.append(
                            {
                                "case_id": case.get("case_id"),
                                "claim": claim.get("claim"),
                                "state": claim.get("state"),
                                "verdict": claim.get("verdict"),
                                "flags": claim.get("flags"),
                                "tier": (claim.get("disposition") or {}).get("tier"),
                                "rule": (claim.get("disposition") or {}).get("rule"),
                                "decided_by": claim.get("decided_by"),
                                "evidence": claim.get("evidence"),
                            }
                        )
            else:
                for case in payload.get("cases", []):
                    for claim in case.get("predicted_claims", []):
                        cases.append(
                            {
                                "case_id": case.get("case_id"),
                                "claim": claim.get("text") or claim.get("claim"),
                                "verdict": claim.get("verdict"),
                                "flags": claim.get("flags"),
                            }
                        )
        captured[name] = {
            "run_id": run_id,
            "returncode": proc.returncode,
            "metrics": metrics,
            "cases": cases,
            "stderr_tail": (proc.stderr or "")[-400:],
        }
    return {"started_at": started, "runs": captured}


def collect(label: str, *, with_eval: bool) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": label,
        "created_at": now_iso(),
        "corpus_grading": corpus_grading(),
        "ranking": ranking_probe(),
    }
    if with_eval:
        payload["evaluations"] = run_evaluations(label)
    path = SNAPSHOT_DIR / f"{label}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def diff(left: str, right: str) -> int:
    a = json.loads((SNAPSHOT_DIR / f"{left}.json").read_text(encoding="utf-8"))
    b = json.loads((SNAPSHOT_DIR / f"{right}.json").read_text(encoding="utf-8"))
    changed = 0

    def compare(title: str, x, y) -> None:
        nonlocal changed
        if x == y:
            print(f"[相同] {title}")
            return
        changed += 1
        print(f"[不同] {title}")
        if isinstance(x, list) and isinstance(y, list):
            for i in range(max(len(x), len(y))):
                xi = x[i] if i < len(x) else None
                yi = y[i] if i < len(y) else None
                if xi != yi:
                    print(f"    #{i} 前：{json.dumps(xi, ensure_ascii=False)}")
                    print(f"    #{i} 后：{json.dumps(yi, ensure_ascii=False)}")
        elif isinstance(x, dict) and isinstance(y, dict):
            for key in sorted(set(x) | set(y)):
                if x.get(key) != y.get(key):
                    print(f"    {key}：{json.dumps(x.get(key), ensure_ascii=False)} → {json.dumps(y.get(key), ensure_ascii=False)}")
        else:
            print(f"    前：{json.dumps(x, ensure_ascii=False)}")
            print(f"    后：{json.dumps(y, ensure_ascii=False)}")

    for field in ("corpus_grading", "ranking"):
        compare(field, a.get(field), b.get(field))
    if "evaluations" in a and "evaluations" in b:
        for name in sorted(set(a["evaluations"]["runs"]) | set(b["evaluations"]["runs"])):
            ra = a["evaluations"]["runs"].get(name, {})
            rb = b["evaluations"]["runs"].get(name, {})
            compare(f"{name} 指标", ra.get("metrics"), rb.get("metrics"))
            compare(f"{name} 逐条主张", ra.get("cases"), rb.get("cases"))
    print(f"\n差异块数：{changed}")
    return 0 if changed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="来源分级对照快照")
    parser.add_argument("--label", help="快照标签，如 before / after")
    parser.add_argument("--no-eval", action="store_true", help="只抓分级与排序，不跑评测")
    parser.add_argument("--diff", nargs=2, metavar=("LEFT", "RIGHT"))
    args = parser.parse_args(argv)

    if args.diff:
        return diff(*args.diff)
    if not args.label:
        parser.error("需要 --label 或 --diff")
    path = collect(args.label, with_eval=not args.no_eval)
    print(f"快照已写入：{path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

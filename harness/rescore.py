"""Re-score an existing run without calling the model again.

Useful when the annotation set, the matching threshold or a metric definition
changed. Model calls are the expensive part of an evaluation, so fixing a
scoring bug should not require paying for them twice.

    python harness/rescore.py --run runs/20261003-165749-deepseek-v2
    python harness/rescore.py --run runs/<id> --threshold 0.25 --out rescored
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.dataset import load_cases  # noqa: E402
from evidence_agent.config import DEFAULTS  # noqa: E402
from evidence_agent.metrics import aggregate  # noqa: E402
from evidence_agent.models import (  # noqa: E402
    CallRecord,
    CaseResult,
    ModelResponse,
    PredictedClaim,
)
from evidence_agent.report import build_report  # noqa: E402


def rebuild_result(raw: dict) -> CaseResult:
    calls = [
        CallRecord(
            step=str(call.get("step", "")),
            response=ModelResponse(
                text="",
                model_id=str(call.get("model_id", "")),
                prompt_tokens=int(call.get("prompt_tokens", 0) or 0),
                completion_tokens=int(call.get("completion_tokens", 0) or 0),
                latency_ms=int(call.get("latency_ms", 0) or 0),
                attempts=int(call.get("attempts", 1) or 1),
                error=call.get("error"),
                error_code=call.get("error_code"),
                finish_reason=call.get("finish_reason"),
            ),
        )
        for call in raw.get("calls", [])
    ]
    return CaseResult(
        case_id=str(raw["case_id"]),
        category="",
        difficulty="",
        predicted_claims=[
            PredictedClaim.from_dict(item)
            for item in raw.get("predicted_claims", [])
        ],
        calls=calls,
        error=raw.get("error"),
        parse_error=raw.get("parse_error"),
        format_violation=raw.get("format_violation"),
        duration_ms=int(raw.get("duration_ms", 0) or 0),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用现有结果重新打分，不调用模型")
    parser.add_argument("--run", required=True, help="runs 目录下的运行编号或路径")
    parser.add_argument("--dataset", default=str(ROOT / "data" / "eval_tasks.json"))
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument(
        "--out",
        choices=["in-place", "rescored"],
        default="rescored",
        help="in-place 覆盖原报告，rescored 写到 report-rescored.md",
    )
    parser.add_argument(
        "--pricing-mode",
        choices=["off_peak", "peak"],
        help="覆盖计费时段",
    )
    args = parser.parse_args(argv)

    run_dir = Path(args.run)
    if not run_dir.is_absolute():
        candidate = ROOT / args.run
        run_dir = candidate if candidate.exists() else ROOT / "runs" / args.run
    if not run_dir.exists():
        print(f"找不到运行目录：{run_dir}", file=sys.stderr)
        return 2

    results_path = run_dir / "results.json"
    meta_path = run_dir / "run_meta.json"
    if not results_path.exists():
        print(f"缺少 results.json：{results_path}", file=sys.stderr)
        return 2

    payload = json.loads(results_path.read_text(encoding="utf-8"))
    meta = (
        json.loads(meta_path.read_text(encoding="utf-8"))
        if meta_path.exists()
        else payload.get("meta", {})
    )
    # Older runs stored a flat pricing block; merge the current defaults so a
    # rescore still produces a meaningful cost estimate.
    pricing = {**DEFAULTS["pricing"], **meta.get("pricing", {})}
    if args.pricing_mode:
        pricing["mode"] = args.pricing_mode
    meta["pricing"] = pricing

    _, all_cases = load_cases(args.dataset)
    by_id = {case.id: case for case in all_cases}

    raw_cases = payload.get("cases", [])
    results = [rebuild_result(item) for item in raw_cases]
    case_ids = [item["case_id"] for item in raw_cases]
    selected = [by_id[cid] for cid in case_ids if cid in by_id]
    missing = [cid for cid in case_ids if cid not in by_id]
    if missing:
        print(f"警告：结果里的这些任务已不在任务集中，已跳过：{missing}", file=sys.stderr)

    order = {case.id: index for index, case in enumerate(selected)}
    results = [r for r in results if r.case_id in order]
    results.sort(key=lambda r: order[r.case_id])

    summary = aggregate(selected, results, threshold=args.threshold)

    report_meta = dict(meta)
    report_meta["rescored_at"] = f"threshold={args.threshold}"
    report = build_report(selected, results, report_meta, threshold=args.threshold)
    target = (
        run_dir / "report.md"
        if args.out == "in-place"
        else run_dir / "report-rescored.md"
    )
    target.write_text(report, encoding="utf-8")

    print(f"重新打分完成：{run_dir.name}（阈值 {args.threshold}）")
    print(f"任务数 {summary['cases']}｜模型调用 {summary['model_calls']} 次（未重新调用）")
    print(
        f"无依据确定性结论率：{summary['unsupported_certainty_rate']} "
        f"({summary['unsupported_certain_count']}/{summary['insufficient_answered']})"
        f"｜证据不足主张漏检 {summary['insufficient_missed']} 条"
    )
    print(f"主判定准确率：{summary['verdict_accuracy']}")
    print(f"主张召回率：{summary['claim_recall']}")
    print(f"标志位召回率：{summary['flag_recall']}")
    print(f"P0 错误：{summary['p0_errors']}｜P1 错误：{summary['p1_errors']}")
    print(f"报告：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the evaluation harness.

Examples:
    python harness/run_eval.py --validate-only
    python harness/run_eval.py --adapter stub --pipeline v1
    python harness/run_eval.py --adapter stub --pipeline v2
    python harness/run_eval.py --adapter deepseek --pipeline v2 --limit 5
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.adapters import ADAPTERS, AdapterConfigError  # noqa: E402
from evidence_agent.config import load_config  # noqa: E402
from evidence_agent.dataset import DatasetError, load_cases  # noqa: E402
from evidence_agent.envfile import load_env_file  # noqa: E402
from evidence_agent.models import CaseResult  # noqa: E402
from evidence_agent.metrics import aggregate, score_case  # noqa: E402
from evidence_agent.pipeline import PIPELINES  # noqa: E402
from evidence_agent.report import build_report  # noqa: E402
from evidence_agent.trace import TraceWriter, now_iso  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="内容证据核查评测 harness")
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="跳过开跑前的鉴权探测",
    )
    parser.add_argument("--config", default=str(ROOT / "config" / "default.json"))
    parser.add_argument("--dataset", default=str(ROOT / "data" / "eval_tasks.json"))
    parser.add_argument("--adapter", choices=sorted(ADAPTERS), help="覆盖配置中的适配器")
    parser.add_argument("--pipeline", choices=sorted(PIPELINES), help="覆盖配置中的管线")
    parser.add_argument("--model-id", help="覆盖配置中的模型 id")
    parser.add_argument("--temperature", type=float, help="覆盖配置中的温度")
    parser.add_argument("--max-tokens", type=int, help="覆盖配置中的输出上限")
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high"],
        help="推理型模型的思考强度，none 表示尽量关闭",
    )
    parser.add_argument(
        "--thinking",
        choices=["enabled", "disabled"],
        help="DeepSeek 思考模式开关，disabled 表示关闭思维链",
    )
    parser.add_argument(
        "--pricing-mode",
        choices=["off_peak", "peak"],
        help="计费时段，仅影响报告里的成本估算",
    )
    parser.add_argument("--limit", type=int, help="只跑前 N 条任务")
    parser.add_argument("--cases", help="只跑指定任务 id，逗号分隔")
    parser.add_argument("--out", help="输出目录，默认写入 runs/<run_id>")
    parser.add_argument("--validate-only", action="store_true", help="只校验任务集")
    parser.add_argument("--no-trace", action="store_true", help="不写 trace.jsonl")
    return parser


def select_cases(cases, args):
    selected = cases
    if args.cases:
        wanted = {item.strip() for item in args.cases.split(",") if item.strip()}
        selected = [c for c in selected if c.id in wanted]
        missing = wanted - {c.id for c in selected}
        if missing:
            raise SystemExit(f"任务 id 不存在：{', '.join(sorted(missing))}")
    if args.limit:
        selected = selected[: args.limit]
    return selected


def summary_to_dict(summary: dict) -> dict:
    payload = dict(summary)
    payload["score_by_case"] = [
        dataclasses.asdict(score) for score in summary["score_by_case"]
    ]
    return payload


def dataset_fingerprint(path: str | Path) -> tuple[str, int]:
    """Identify the evaluation set a run was measured against.

    Scores from two runs are only comparable when they used the same task set,
    so the fingerprint travels with every run.
    """

    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:8]
    cases = json.loads(data.decode("utf-8")).get("cases", [])
    return digest, len(cases)


PREFLIGHT_MESSAGES = [
    {"role": "system", "content": "你是一个连通性检查程序，只输出 JSON。"},
    {"role": "user", "content": '输出 {"ok": true}'},
]

AUTH_MARKERS = ("401", "403", "Authentication Fails", "authentication_error")


def preflight(adapter) -> tuple[bool, str]:
    """One tiny call that catches a dead API key before a whole run is burned."""

    response = adapter.complete(PREFLIGHT_MESSAGES, max_tokens=32)
    if response.ok:
        return True, ""
    text = f"{response.error_code} {response.error}"
    if any(marker in text for marker in AUTH_MARKERS):
        return False, (
            "鉴权失败，评测已中止，未消耗评测调用。\n"
            f"接口返回：{response.error}\n"
            "请检查 DEEPSEEK_API_KEY 是否仍然有效，或到控制台重新生成密钥。"
        )
    return True, ""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    load_env_file(ROOT / ".env.local")

    try:
        dataset_meta, cases = load_cases(args.dataset)
    except (DatasetError, json.JSONDecodeError) as exc:
        print(f"任务集校验失败：{exc}", file=sys.stderr)
        return 1

    if args.validate_only:
        by_category = Counter(c.category for c in cases)
        claims = sum(len(c.expected_claims) for c in cases)
        print(f"任务集：{args.dataset}")
        print(f"案例数：{len(cases)}")
        print(f"标注主张数：{claims}")
        print(f"需要人工复核的案例：{sum(1 for c in cases if c.requires_human)}")
        print("类别分布：" + "，".join(f"{k}={v}" for k, v in sorted(by_category.items())))
        print(f"模型口径：{dataset_meta.get('model_under_test', 'n/a')}")
        return 0

    config = load_config(args.config)
    if args.adapter:
        config["adapter"] = args.adapter
    if args.pipeline:
        config["pipeline"] = args.pipeline
    if args.model_id:
        config["model_id"] = args.model_id
    if args.temperature is not None:
        config["temperature"] = args.temperature
    if args.max_tokens is not None:
        config["max_tokens"] = args.max_tokens
    if args.reasoning_effort is not None:
        config["reasoning_effort"] = args.reasoning_effort
    if args.thinking is not None:
        config["thinking"] = args.thinking
    if args.pricing_mode is not None:
        config.setdefault("pricing", {})["mode"] = args.pricing_mode

    selected = select_cases(cases, args)
    if not selected:
        print("没有选中任何任务", file=sys.stderr)
        return 1

    adapter_cls = ADAPTERS[config["adapter"]]
    adapter_kwargs = dict(
        model_id=config["model_id"],
        timeout_s=float(config.get("timeout_s", 60)),
        max_retries=int(config.get("max_retries", 2)),
        backoff_s=float(config.get("backoff_s", 1.5)),
        temperature=float(config.get("temperature", 0.0)),
        max_tokens=int(config.get("max_tokens", 1200)),
    )
    if adapter_cls.name == "deepseek":
        adapter_kwargs.update(
            base_url=config.get("base_url", "https://api.deepseek.com"),
            api_key_env=config.get("api_key_env", "DEEPSEEK_API_KEY"),
            json_mode=bool(config.get("json_mode", True)),
            reasoning_effort=config.get("reasoning_effort") or None,
            thinking=config.get("thinking") or None,
        )
    try:
        adapter = adapter_cls(**adapter_kwargs)
    except AdapterConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not args.skip_preflight:
        ok, message = preflight(adapter)
        if not ok:
            print(message, file=sys.stderr)
            return 2

    pipeline_cls = PIPELINES[config["pipeline"]]
    pipeline = pipeline_cls(
        temperature=float(config.get("temperature", 0.0)),
        max_tokens=int(config.get("max_tokens", 1200)),
        step_settings=config.get("steps", {}),
    )

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{adapter.name}-{pipeline.name}"
    run_dir = Path(args.out) if args.out else ROOT / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    eval_set_hash, eval_set_total = dataset_fingerprint(args.dataset)

    meta = {
        "run_id": run_id,
        "started_at": now_iso(),
        "config_path": str(args.config),
        "eval_set_hash": eval_set_hash,
        "eval_set_total": eval_set_total,
        "selected_cases": len(selected),
        "adapter": adapter.name,
        "model_id": adapter.model_id,
        "pipeline": pipeline.name,
        "temperature": config.get("temperature", 0.0),
        "max_tokens": config.get("max_tokens", 1200),
        "case_ids": [c.id for c in selected],
        "pricing": config.get("pricing", {}),
        "steps": config.get("steps", {}),
        "dataset": str(Path(args.dataset).name),
        "note": (
            "stub 为离线朴素基线，只用于验证评测管线，不代表模型能力"
            if adapter.name == "stub"
            else ""
        ),
    }

    print(f"运行 {run_id}")
    print(f"适配器 {adapter.name}｜模型 {adapter.model_id}｜管线 {pipeline.name}｜任务 {len(selected)}")
    print("-" * 68)

    results = []
    trace = None if args.no_trace else TraceWriter(run_dir)
    try:
        if trace:
            trace.log("run_started", **meta)
        for index, case in enumerate(selected, start=1):
            try:
                result = pipeline.run(case, adapter)
            except Exception as exc:  # one bad case must not kill the whole run
                result = CaseResult(
                    case_id=case.id,
                    category=case.category,
                    difficulty=case.difficulty,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            if trace:
                trace.log_case(result)
            score = score_case(case, result)
            flag = "OK" if not (score.p0_errors or result.failed) else "FAIL"
            reason = ""
            if result.error:
                reason = f" (调用失败：{result.error[:36]})"
            elif result.parse_error:
                reason = f" (解析失败：{result.parse_error[:36]})"
            elif result.format_violation:
                reason = " (格式已降级)"
            print(
                f"[{index:>2}/{len(selected)}] {case.id} {flag}{reason} "
                f"匹配 {score.matched}/{score.expected_total} "
                f"判定 {score.verdict_correct}/{score.matched} "
                f"P0={len(score.p0_errors)} P1={len(score.p1_errors)}"
            )
    finally:
        if trace:
            trace.close()

    threshold = float(config.get("match_threshold", 0.3))
    summary = aggregate(selected, results, threshold=threshold)

    results_path = run_dir / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "meta": meta,
                "summary": summary_to_dict(summary),
                "cases": [
                    {
                        "case_id": r.case_id,
                        "error": r.error,
                        "parse_error": r.parse_error,
                        "format_violation": r.format_violation,
                        "duration_ms": r.duration_ms,
                        "raw_output_tail": (
                            r.calls[-1].response.text[:2000]
                            if r.calls and (r.error or r.parse_error)
                            else None
                        ),
                        "predicted_claims": [dataclasses.asdict(c) for c in r.predicted_claims],
                        "calls": [
                            {
                                "step": c.step,
                                "model_id": c.response.model_id,
                                "latency_ms": c.response.latency_ms,
                                "attempts": c.response.attempts,
                                "prompt_tokens": c.response.prompt_tokens,
                                "completion_tokens": c.response.completion_tokens,
                                "finish_reason": c.response.finish_reason,
                                "raw_body": c.response.raw_body,
                                "error": c.response.error,
                                "error_code": c.response.error_code,
                            }
                            for c in r.calls
                        ],
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report_path = run_dir / "report.md"
    report_path.write_text(
        build_report(selected, results, meta, threshold=threshold), encoding="utf-8"
    )
    (run_dir / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("-" * 68)
    print(f"无依据确定性结论率：{summary['unsupported_certainty_rate']} "
          f"({summary['unsupported_certain_count']}/{summary['insufficient_answered']})"
          f"｜证据不足主张漏检 {summary['insufficient_missed']} 条")
    print(f"主判定准确率：{summary['verdict_accuracy']}")
    print(f"主张召回率：{summary['claim_recall']}")
    print(f"标志位召回率：{summary['flag_recall']}")
    print(
        f"输出格式违规：{len(summary['format_violation_cases'])} 个任务"
        + (
            "（" + "，".join(summary["format_violation_cases"]) + "）"
            if summary["format_violation_cases"]
            else ""
        )
    )
    print(f"P0 错误：{summary['p0_errors']}｜P1 错误：{summary['p1_errors']}")
    print(
        f"模型调用：{summary['model_calls']} 次｜"
        f"平均耗时：{summary['avg_latency_ms']} ms｜最长：{summary['max_latency_ms']} ms"
    )
    print(f"报告：{report_path}")
    print(f"结果：{results_path}")
    if trace:
        print(f"Trace：{trace.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

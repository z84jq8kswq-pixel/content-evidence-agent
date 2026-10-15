"""Comparison between two evaluation runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .config import DEFAULTS, resolve_pricing
from .matching import match_claims
from .metrics import aggregate
from .models import CaseResult, TaskCase
from .runio import load_run

METRICS: list[tuple[str, str, str]] = [
    ("verdict_accuracy", "主判定准确率", "higher"),
    ("claim_recall", "主张召回率", "higher"),
    ("claim_precision", "主张精确率", "higher"),
    ("flag_recall", "标志位召回率", "higher"),
    ("unsupported_certainty_rate", "无依据确定性结论率", "lower"),
    ("review_queue_agreement", "复核队列一致率（推导）", "higher"),
    ("p0_errors", "P0 错误", "lower"),
    ("p1_errors", "P1 错误", "lower"),
    ("insufficient_missed", "证据不足主张漏检", "lower"),
    ("model_calls", "模型调用次数", "lower"),
    ("prompt_tokens", "输入 token", "lower"),
    ("completion_tokens", "输出 token", "lower"),
    ("avg_latency_ms", "平均单任务耗时(ms)", "lower"),
]


def cost_of(meta: dict, summary: dict) -> float:
    pricing = {**DEFAULTS["pricing"], **(meta.get("pricing") or {})}
    prompt_rate, completion_rate, _ = resolve_pricing(pricing)
    return (
        summary["prompt_tokens"] / 1_000_000 * prompt_rate
        + summary["completion_tokens"] / 1_000_000 * completion_rate
    )


def verdicts_by_claim(
    case: TaskCase, result: CaseResult | None, threshold: float
) -> dict[str, str]:
    if result is None:
        return {c.text: "(无结果)" for c in case.expected_claims}
    pairs, unmatched_e, _ = match_claims(
        [c.text for c in case.expected_claims],
        [c.text for c in result.predicted_claims],
        threshold=threshold,
    )
    verdicts: dict[str, str] = {}
    for e_idx, p_idx, _ in pairs:
        verdicts[case.expected_claims[e_idx].text] = result.predicted_claims[p_idx].verdict
    for e_idx in unmatched_e:
        verdicts[case.expected_claims[e_idx].text] = "(未抽出)"
    return verdicts


def build_comparison(
    base_meta: dict,
    base_results: Sequence[CaseResult],
    new_meta: dict,
    new_results: Sequence[CaseResult],
    cases: Sequence[TaskCase],
    threshold: float = 0.3,
) -> dict[str, Any]:
    base_by_id = {r.case_id: r for r in base_results}
    new_by_id = {r.case_id: r for r in new_results}

    base_summary = aggregate(
        cases, [base_by_id[c.id] for c in cases if c.id in base_by_id], threshold
    )
    new_summary = aggregate(
        cases, [new_by_id[c.id] for c in cases if c.id in new_by_id], threshold
    )

    rows = []
    for key, name, direction in METRICS:
        base_value = base_summary.get(key)
        new_value = new_summary.get(key)
        if isinstance(base_value, (int, float)) and isinstance(new_value, (int, float)):
            delta = new_value - base_value
            if abs(delta) < 1e-9:
                verdict = "持平"
            elif (delta > 0) == (direction == "higher"):
                verdict = "改善"
            else:
                verdict = "变差"
        else:
            delta, verdict = None, "n/a"
        rows.append(
            {
                "key": key,
                "name": name,
                "base": base_value,
                "new": new_value,
                "delta": delta,
                "direction": direction,
                "verdict": verdict,
            }
        )

    base_cost = cost_of(base_meta, base_summary)
    new_cost = cost_of(new_meta, new_summary)
    rows.append(
        {
            "key": "cost",
            "name": "估算成本(元)",
            "base": round(base_cost, 4),
            "new": round(new_cost, 4),
            "delta": round(new_cost - base_cost, 4),
            "direction": "lower",
            "verdict": "改善" if new_cost < base_cost else ("持平" if abs(new_cost - base_cost) < 1e-9 else "变差"),
        }
    )

    changes = []
    for case in cases:
        base_v = verdicts_by_claim(case, base_by_id.get(case.id), threshold)
        new_v = verdicts_by_claim(case, new_by_id.get(case.id), threshold)
        for idx, claim in enumerate(case.expected_claims):
            before = base_v.get(claim.text, "(缺失)")
            after = new_v.get(claim.text, "(缺失)")
            if before == after:
                continue
            if after == claim.verdict:
                note = "改为正确"
            elif before == claim.verdict:
                note = "改为错误"
            elif before == "(未抽出)":
                note = "由漏检变为有判定"
            elif after == "(未抽出)":
                note = "由有判定变为漏检"
            else:
                note = "正误未变"
            changes.append(
                {
                    "case_id": case.id,
                    "claim_index": idx + 1,
                    "claim": claim.text,
                    "expected": claim.verdict,
                    "base": before,
                    "new": after,
                    "note": note,
                }
            )

    return {
        "base": {
            "run": base_meta.get("run_dir", ""),
            "config": base_meta.get("config_path", ""),
            "pipeline": base_meta.get("pipeline", ""),
            "model": base_meta.get("model_id", ""),
        },
        "new": {
            "run": new_meta.get("run_dir", ""),
            "config": new_meta.get("config_path", ""),
            "pipeline": new_meta.get("pipeline", ""),
            "model": new_meta.get("model_id", ""),
        },
        "rows": rows,
        "changes": changes,
        "base_summary": base_summary,
        "new_summary": new_summary,
    }


def comparison_for(
    base_ref: str, new_ref: str, project_root: Path, threshold: float = 0.3
) -> dict[str, Any]:
    from .dataset import load_cases

    base_meta, base_results, _ = load_run(base_ref, project_root)
    new_meta, new_results, _ = load_run(new_ref, project_root)
    _, all_cases = load_cases(project_root / "data" / "eval_tasks.json")
    by_id = {case.id: case for case in all_cases}
    case_ids = [r.case_id for r in base_results]
    cases = [by_id[cid] for cid in case_ids if cid in by_id]
    return build_comparison(
        base_meta, base_results, new_meta, new_results, cases, threshold
    )


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(
    comparison: dict[str, Any],
    label_base: str | None = None,
    label_new: str | None = None,
    threshold: float = 0.3,
) -> str:
    base_label = label_base or comparison["base"]["run"]
    new_label = label_new or comparison["new"]["run"]
    lines = [
        "# 运行对比",
        "",
        f"- 基准：`{base_label}`（配置 {comparison['base']['config'] or 'n/a'}）",
        f"- 对照：`{new_label}`（配置 {comparison['new']['config'] or 'n/a'}）",
        f"- 任务数：{comparison['base_summary']['cases']}｜匹配阈值：{threshold}",
        "",
        "## 指标对比",
        "",
        f"| 指标 | {base_label} | {new_label} | 变化 | 方向 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in comparison["rows"]:
        delta = row["delta"]
        if delta is None:
            delta_text = "n/a"
        elif isinstance(delta, float):
            delta_text = f"{delta:+.4f}"
        else:
            delta_text = f"{delta:+d}"
        lines.append(
            f"| {row['name']} | {_fmt(row['base'])} | {_fmt(row['new'])} | "
            f"{delta_text} | {row['verdict']} |"
        )
    lines.append("")
    lines.append("## 判定发生变化的案例")
    lines.append("")
    lines.append(f"| 任务 | 期望 | {base_label} | {new_label} | 变化 |")
    lines.append("| --- | --- | --- | --- | --- |")
    if comparison["changes"]:
        for change in comparison["changes"]:
            lines.append(
                f"| {change['case_id']}#{change['claim_index']} | {change['expected']} | "
                f"{change['base']} | {change['new']} | {change['note']} |"
            )
    else:
        lines.append("| - | - | - | - | 无变化 |")
    lines.append("")
    return "\n".join(lines)

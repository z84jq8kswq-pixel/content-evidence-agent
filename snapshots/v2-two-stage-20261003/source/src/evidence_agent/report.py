"""Render a human-readable evaluation report."""

from __future__ import annotations

from typing import Sequence

from .config import resolve_pricing
from .metrics import CaseScore, aggregate
from .models import CaseResult, TaskCase


def _fmt(value: float | int | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def build_report(
    cases: Sequence[TaskCase],
    results: Sequence[CaseResult],
    meta: dict,
    threshold: float = 0.3,
) -> str:
    summary = aggregate(cases, results, threshold=threshold)
    scores: list[CaseScore] = summary["score_by_case"]
    case_by_id = {c.id: c for c in cases}
    result_by_id = {r.case_id: r for r in results}
    prompt_rate, completion_rate, pricing_mode = resolve_pricing(meta.get("pricing", {}))
    prompt_cost = summary["prompt_tokens"] / 1_000_000 * prompt_rate
    completion_cost = summary["completion_tokens"] / 1_000_000 * completion_rate

    lines: list[str] = []
    lines.append("# 评测报告")
    lines.append("")
    lines.append(f"- 运行编号：`{meta.get('run_id', 'n/a')}`")
    lines.append(f"- 配置：`{meta.get('config_path', 'n/a')}`")
    lines.append(f"- 适配器：{meta.get('adapter', 'n/a')}")
    lines.append(f"- 模型：`{meta.get('model_id', 'n/a')}`")
    lines.append(f"- 管线：{meta.get('pipeline', 'n/a')}")
    lines.append(f"- 任务数：{summary['cases']}")
    lines.append(f"- 温度：{meta.get('temperature', 'n/a')}，匹配阈值：{threshold}")
    lines.append("")
    lines.append("## 核心指标")
    lines.append("")
    lines.append("| 指标 | 数值 | 说明 |")
    lines.append("| --- | --- | --- |")
    lines.append(
        f"| 无依据确定性结论率 | {_fmt(summary['unsupported_certainty_rate'])} | "
        f"系统实际判定的 {summary['insufficient_answered']} 条证据不足主张中，"
        f"有 {summary['unsupported_certain_count']} 条被判成确定结论；"
        f"另有 {summary['insufficient_missed']} 条未被拆出 |"
    )
    lines.append(
        f"| 主判定准确率 | {_fmt(summary['verdict_accuracy'])} | 已匹配主张中的四分类一致率 |"
    )
    lines.append(f"| 主张召回率 | {_fmt(summary['claim_recall'])} | 标注主张被拆出的比例 |")
    lines.append(f"| 主张精确率 | {_fmt(summary['claim_precision'])} | 拆出的主张中命中标注的比例 |")
    lines.append(f"| 标志位召回率 | {_fmt(summary['flag_recall'])} | 过期、冲突、范围错配等标志 |")
    lines.append(
        f"| 引用命中率（近似） | {_fmt(summary['citation_precision_proxy'])} | "
        "引用编号与标注证据的重合度，非严格判定 |"
    )
    lines.append(
        f"| 复核队列一致率 | {_fmt(summary['review_queue_agreement'])} | 是否该进人工复核队列 |"
    )
    lines.append(
        f"| 输出格式违规 | {len(summary['format_violation_cases'])} 个任务 | "
        "模型未按约定 schema 输出，已降级解析并单独计数 |"
    )
    lines.append(f"| P0 错误 | {summary['p0_errors']} | 影响处置方向 |")
    lines.append(f"| P1 错误 | {summary['p1_errors']} | 影响复核效率 |")
    lines.append("")
    lines.append("## 成本与耗时")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 模型调用次数 | {summary['model_calls']} |")
    lines.append(f"| 输入 token | {summary['prompt_tokens']} |")
    lines.append(f"| 输出 token | {summary['completion_tokens']} |")
    lines.append(f"| 平均单任务耗时 | {_fmt(summary['avg_latency_ms'])} ms |")
    lines.append(f"| 最长单任务耗时 | {_fmt(summary['max_latency_ms'])} ms |")
    lines.append(
        f"| 估算成本 | {prompt_cost + completion_cost:.4f} 元"
        f"（输入 {prompt_rate} / 输出 {completion_rate} 元每百万 token，{pricing_mode}） |"
    )
    lines.append(f"| 失败任务 | {', '.join(summary['failed_cases']) or '无'} |")
    lines.append("")
    lines.append("## 分案例结果")
    lines.append("")
    lines.append("| 任务 | 类别 | 匹配 | 判定正确 | P0 | P1 | 队列一致 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for score in scores:
        case = case_by_id.get(score.case_id)
        lines.append(
            f"| {score.case_id} | {case.category if case else ''} | "
            f"{score.matched}/{score.expected_total} | "
            f"{score.verdict_correct}/{score.matched} | "
            f"{len(score.p0_errors)} | {len(score.p1_errors)} | "
            f"{score.queue_agreement}/{score.matched} |"
        )
    lines.append("")

    lines.append("## 失败明细")
    lines.append("")
    any_failure = False
    for score in scores:
        result = result_by_id.get(score.case_id)
        case = case_by_id.get(score.case_id)
        if not (score.p0_errors or score.p1_errors or score.unmatched_expected or score.unmatched_predicted):
            continue
        any_failure = True
        lines.append(f"### {score.case_id}｜{case.what_it_tests if case else ''}")
        lines.append("")
        if result and result.error:
            lines.append(f"- 运行错误：{result.error}")
        if result and result.parse_error:
            lines.append(f"- 解析错误：{result.parse_error}")
        if result and result.format_violation:
            lines.append(f"- 格式违规：{result.format_violation}")
        for item in score.p0_errors:
            lines.append(f"- P0：{item}")
        for item in score.p1_errors:
            lines.append(f"- P1：{item}")
        for item in score.unmatched_expected:
            lines.append(f"- 漏检主张：{item}")
        for item in score.unmatched_predicted:
            lines.append(f"- 多余主张：{item}")
        lines.append("")
        if case:
            lines.append("标注答案：")
            lines.append("")
            lines.append("| 主张 | 期望判定 | 期望标志 | 期望证据 |")
            lines.append("| --- | --- | --- | --- |")
            for claim in case.expected_claims:
                lines.append(
                    f"| {claim.text} | {claim.verdict} | "
                    f"{'/'.join(claim.flags) or '-'} | "
                    f"{'/'.join(claim.evidence) or '-'} |"
                )
            lines.append("")
        if result and result.predicted_claims:
            lines.append("模型输出：")
            lines.append("")
            lines.append("| 主张 | 判定 | 标志位 | 证据 | 依据 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for claim in result.predicted_claims:
                reasoning = claim.reasoning.replace("|", "/")[:60]
                lines.append(
                    f"| {claim.text} | {claim.verdict} | "
                    f"{'/'.join(claim.flags) or '-'} | "
                    f"{'/'.join(claim.evidence) or '-'} | {reasoning} |"
                )
            lines.append("")
        if result and (result.parse_error or result.error) and result.calls:
            raw = result.calls[-1].response.text.strip()
            if raw:
                lines.append("模型原始返回（截断 800 字）：")
                lines.append("")
                lines.append("```")
                lines.append(raw[:800])
                lines.append("```")
                lines.append("")
    if not any_failure:
        lines.append("全部案例无 P0／P1 记录。")
        lines.append("")

    return "\n".join(lines)

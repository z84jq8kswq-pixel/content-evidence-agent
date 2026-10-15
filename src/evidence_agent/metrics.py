"""Evaluation metrics for the evidence-checking harness."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .matching import match_claims
from .models import (
    CERTAIN_VERDICTS,
    CaseResult,
    ExpectedClaim,
    PredictedClaim,
    TaskCase,
)


@dataclass
class CaseScore:
    case_id: str
    category: str
    matched: int = 0
    expected_total: int = 0
    predicted_total: int = 0
    verdict_correct: int = 0
    flag_hits: int = 0
    flag_expected: int = 0
    citation_hits: int = 0
    citation_predicted: int = 0
    queue_agreement: int = 0
    p0_errors: list[str] = field(default_factory=list)
    p1_errors: list[str] = field(default_factory=list)
    unmatched_expected: list[str] = field(default_factory=list)
    unmatched_predicted: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    #: 复核队列不一致时的归因（判定不同／标志位漏标／主张类型）
    queue_mismatch_reasons: list[str] = field(default_factory=list)
    #: 判定字段缺失或非法的条数：这不是"答错"，是"没回答"
    verdict_missing: int = 0


def _p0_reason(expected: ExpectedClaim, predicted: PredictedClaim) -> str | None:
    if expected.verdict == "INSUFFICIENT" and predicted.verdict in CERTAIN_VERDICTS:
        return "证据不足却被判为确定结论"
    if expected.verdict == "CONTRADICTED" and predicted.verdict == "SUPPORTED":
        return "冲突内容被判为支持"
    if expected.verdict == "SUPPORTED" and predicted.verdict == "CONTRADICTED":
        return "有据内容被判为冲突"
    return None


def _queue_mismatch_reason(
    expected: ExpectedClaim, predicted: PredictedClaim, expected_flags: set[str]
) -> str:
    """队列不一致的归因。顺序即因果：先看判定，再看标志位，最后才是主张类型。"""

    if expected.verdict != predicted.verdict:
        return "判定不同"
    if expected_flags - set(predicted.flags):
        return "标志位漏标"
    if expected.verdict == "INSUFFICIENT":
        return "主张类型（核心／非核心）"
    return "其他"


def score_case(case: TaskCase, result: CaseResult, threshold: float = 0.3) -> CaseScore:
    score = CaseScore(
        case_id=case.id,
        category=case.category,
        expected_total=len(case.expected_claims),
        predicted_total=len(result.predicted_claims),
    )
    if result.failed and not result.predicted_claims:
        score.p0_errors.append(
            f"运行失败：{result.error or result.parse_error or '未知错误'}"
        )
        score.unmatched_expected = [c.text for c in case.expected_claims]
        return score

    pairs, unmatched_e, unmatched_p = match_claims(
        [c.text for c in case.expected_claims],
        [c.text for c in result.predicted_claims],
        threshold=threshold,
    )
    score.matched = len(pairs)
    score.unmatched_expected = [case.expected_claims[i].text for i in unmatched_e]
    score.unmatched_predicted = [result.predicted_claims[i].text for i in unmatched_p]

    for e_idx, p_idx, sim in pairs:
        expected = case.expected_claims[e_idx]
        predicted = result.predicted_claims[p_idx]

        # 没有判定字段的输出不算"回答"：既不记为答对，也不进核心指标的分母。
        # 否则 schema 崩坏反而会拿到最安全的答案，指标看起来最好。
        if predicted.verdict_inferred:
            score.verdict_missing += 1
        elif expected.verdict == predicted.verdict:
            score.verdict_correct += 1
        reason = _p0_reason(expected, predicted)
        if reason:
            score.p0_errors.append(f"{expected.text[:24]}：{reason}")

        expected_flags = set(expected.flags)
        if expected_flags:
            score.flag_expected += len(expected_flags)
            score.flag_hits += len(expected_flags & set(predicted.flags))
        missing_flags = expected_flags - set(predicted.flags)
        extra_flags = set(predicted.flags) - expected_flags
        if (missing_flags or extra_flags) and not reason:
            detail = []
            if missing_flags:
                detail.append("漏标 " + "/".join(sorted(missing_flags)))
            if extra_flags:
                detail.append("多标 " + "/".join(sorted(extra_flags)))
            score.p1_errors.append(f"{expected.text[:24]}：{'，'.join(detail)}")

        expected_evidence = set(expected.evidence)
        if predicted.evidence:
            score.citation_predicted += len(predicted.evidence)
            score.citation_hits += len(expected_evidence & set(predicted.evidence))

        if expected.in_review_queue == predicted.in_review_queue:
            score.queue_agreement += 1
        else:
            # 队列判定是从判定／标志位／主张类型推出来的，不一致时必须归因：
            # 这个指标本身不独立（35 条标注里只有 2 条与规则不一致），
            # 不带归因地报一个百分比，等于给判定准确率换个名字再说一遍。
            score.queue_mismatch_reasons.append(
                _queue_mismatch_reason(expected, predicted, expected_flags)
            )
            score.p1_errors.append(
                f"{expected.text[:24]}：复核队列判断不一致"
                f"（期望 {'进' if expected.in_review_queue else '不进'}）"
            )

        score.details.append(
            f"{expected.verdict}→{predicted.verdict} sim={sim:.2f}"
        )

    return score


def aggregate(
    cases: Sequence[TaskCase],
    results: Sequence[CaseResult],
    threshold: float = 0.3,
) -> dict:
    by_id = {r.case_id: r for r in results}
    scores = [
        score_case(case, by_id[case.id], threshold=threshold)
        for case in cases
        if case.id in by_id
    ]

    expected_total = sum(s.expected_total for s in scores)
    matched_total = sum(s.matched for s in scores)
    predicted_total = sum(s.predicted_total for s in scores)
    verdict_correct = sum(s.verdict_correct for s in scores)
    flag_hits = sum(s.flag_hits for s in scores)
    flag_expected = sum(s.flag_expected for s in scores)
    citation_hits = sum(s.citation_hits for s in scores)
    citation_predicted = sum(s.citation_predicted for s in scores)
    queue_agreement = sum(s.queue_agreement for s in scores)

    # Core metric: of the expected INSUFFICIENT claims the system actually
    # answered, how many were given a definite verdict anyway. Claims that were
    # never extracted are a recall failure and are counted separately, so this
    # number means exactly what its name says.
    insufficient_expected = 0
    unsupported_certain = 0
    insufficient_missed = 0
    insufficient_unanswered = 0
    for case in cases:
        result = by_id.get(case.id)
        if result is None:
            continue
        expected_insufficient = [
            c for c in case.expected_claims if c.verdict == "INSUFFICIENT"
        ]
        if not expected_insufficient:
            continue
        pairs, _, _ = match_claims(
            [c.text for c in case.expected_claims],
            [c.text for c in result.predicted_claims],
            threshold=threshold,
        )
        predicted_by_expected = {e: result.predicted_claims[p] for e, p, _ in pairs}
        for e_idx, claim in enumerate(case.expected_claims):
            if claim.verdict != "INSUFFICIENT":
                continue
            insufficient_expected += 1
            predicted = predicted_by_expected.get(e_idx)
            if predicted is None:
                insufficient_missed += 1
            elif predicted.verdict_inferred:
                insufficient_unanswered += 1
            elif predicted.verdict in CERTAIN_VERDICTS:
                unsupported_certain += 1

    insufficient_answered = (
        insufficient_expected - insufficient_missed - insufficient_unanswered
    )

    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    total_calls = sum(len(r.calls) for r in results)
    total_prompt_tokens = sum(r.prompt_tokens for r in results)
    total_completion_tokens = sum(r.completion_tokens for r in results)
    failed_cases = [r.case_id for r in results if r.failed]
    latencies = [r.duration_ms for r in results if r.duration_ms is not None]
    format_violations = [r.case_id for r in results if r.format_violation]

    return {
        "cases": len(scores),
        "claim_recall": ratio(matched_total, expected_total),
        "claim_precision": ratio(matched_total, predicted_total),
        "verdict_accuracy": ratio(verdict_correct, matched_total),
        "flag_recall": ratio(flag_hits, flag_expected),
        "citation_precision_proxy": ratio(citation_hits, citation_predicted),
        "review_queue_agreement": ratio(queue_agreement, matched_total),
        "unsupported_certainty_rate": ratio(unsupported_certain, insufficient_answered),
        "insufficient_expected": insufficient_expected,
        "insufficient_answered": insufficient_answered,
        "insufficient_missed": insufficient_missed,
        "insufficient_unanswered": insufficient_unanswered,
        "verdict_missing_count": sum(s.verdict_missing for s in scores),
        "queue_mismatch_breakdown": dict(
            Counter(reason for s in scores for reason in s.queue_mismatch_reasons)
        ),
        "unsupported_certain_count": unsupported_certain,
        "p0_errors": sum(len(s.p0_errors) for s in scores),
        "p1_errors": sum(len(s.p1_errors) for s in scores),
        "failed_cases": failed_cases,
        "format_violation_cases": format_violations,
        "format_violation_rate": ratio(len(format_violations), len(results)),
        "model_calls": total_calls,
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "max_latency_ms": max(latencies) if latencies else None,
        "score_by_case": scores,
    }

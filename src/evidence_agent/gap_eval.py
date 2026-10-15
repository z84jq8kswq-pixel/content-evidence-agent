"""缺口任务的评测：加载标注、逐条打分、与 v2 的理论上界对比。

与 `metrics.py` 的分工：

- `metrics.py` 评的是**判定结果**（v1/v2 用同一个任务集打分）；
- 本模块评的是**过程**——工具选得对不对、要不要换策略、该不该交回给人。

v2 上界是本模块特有的口径。v2 的证据来源被限定在用户给的资料里，所以对
缺口任务它能到达的高度有一个先验上限：期望证据不在给定资料里的主张，v2
无论模型多强都够不着。这个上界是对 v2 最有利的假设（假设它判断全对），
因此用它做对照比"跑一次真模型然后比分数"更保守、也更公平。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .matching import match_claims
from .runtime.agent import JUDGED, ClaimOutcome, ContentOutcome
from .runtime.disposition import TIER_NEEDS_HUMAN


class GapDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class GapClaim:
    text: str
    expected_state: str
    verdict: str
    flags: tuple[str, ...]
    evidence: tuple[str, ...]
    optimal_tools: tuple[str, ...]
    reason_contains: str
    note: str
    #: 期望的处置档位（见 runtime/disposition.py）。空字符串表示这条不检查档位。
    expected_tier: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GapClaim":
        state = str(raw.get("expected_state", "JUDGED")).upper()
        if state not in (JUDGED, "ESCALATED", "TOOL_FAILED"):
            raise GapDataError(f"非法期望状态：{state}")
        return cls(
            text=str(raw["text"]),
            expected_state=state,
            verdict=str(raw.get("verdict", "INSUFFICIENT")).upper(),
            flags=tuple(str(f) for f in raw.get("flags", [])),
            evidence=tuple(str(e) for e in raw.get("evidence", [])),
            optimal_tools=tuple(str(t) for t in raw.get("optimal_tools", [])),
            reason_contains=str(raw.get("reason_contains", "")),
            note=str(raw.get("note", "")),
            expected_tier=str(raw.get("expected_tier", "")),
        )


@dataclass(frozen=True)
class GapCase:
    id: str
    category: str
    difficulty: str
    content: str
    given_sources: tuple[str, ...]
    expected_claims: tuple[GapClaim, ...]
    what_it_tests: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GapCase":
        return cls(
            id=str(raw["id"]),
            category=str(raw.get("category", "")),
            difficulty=str(raw.get("difficulty", "")),
            content=str(raw.get("content", "")),
            given_sources=tuple(str(s) for s in raw.get("given_sources", [])),
            expected_claims=tuple(
                GapClaim.from_dict(item) for item in raw.get("expected_claims", [])
            ),
            what_it_tests=str(raw.get("what_it_tests", "")),
        )


def load_gap_cases(path: str | Path) -> tuple[dict, list[GapCase]]:
    gap_path = Path(path)
    if not gap_path.exists():
        raise GapDataError(f"缺口任务集不存在：{gap_path}")
    raw = json.loads(gap_path.read_text(encoding="utf-8"))
    if "cases" not in raw or not isinstance(raw["cases"], list):
        raise GapDataError("缺口任务集缺少 cases 数组")

    cases: list[GapCase] = []
    seen: set[str] = set()
    for item in raw["cases"]:
        case = GapCase.from_dict(item)
        if case.id in seen:
            raise GapDataError(f"案例 id 重复：{case.id}")
        seen.add(case.id)
        if not case.expected_claims:
            raise GapDataError(f"{case.id} 没有标注主张")
        cases.append(case)
    return raw, cases


@dataclass
class ClaimScore:
    case_id: str
    category: str
    claim_text: str
    expected_state: str
    predicted_state: str
    state_ok: bool
    verdict_ok: bool | None = None
    flag_recall: float | None = None
    flag_precision: float | None = None
    missing_flags: list[str] = field(default_factory=list)
    spurious_flags: list[str] = field(default_factory=list)
    evidence_ok: bool | None = None
    #: 承载结论的来源（运行时给出），用于人工核对"关键证据到底有没有进结论"
    evidence_decisive: list[str] = field(default_factory=list)
    tool_selection: float | None = None
    missing_tools: list[str] = field(default_factory=list)
    reason_ok: bool | None = None
    tool_calls: int = 0
    retried: bool = False
    predicted_tools: list[str] = field(default_factory=list)
    reason: str = ""
    expected_tier: str = ""
    predicted_tier: str = ""
    tier_ok: bool | None = None
    needs_human: bool = False
    #: 这条期望主张有没有被系统自己拆出来（对齐按文本相似度，不按顺序）
    matched: bool = True
    #: 这是系统多拆出来的主张，标注里没有对应项
    spurious: bool = False
    #: 该交给人的有没有交到人手里（按处置档位评，不按原始状态）
    routing_ok: bool | None = None


def score_claim(
    case: GapCase, expected: GapClaim, outcome: ClaimOutcome
) -> ClaimScore:
    predicted_state = outcome.state
    score = ClaimScore(
        case_id=case.id,
        category=case.category,
        claim_text=expected.text,
        expected_state=expected.expected_state,
        predicted_state=predicted_state,
        state_ok=predicted_state == expected.expected_state,
        tool_calls=outcome.tool_calls,
        retried=outcome.tool_calls > 1,
        predicted_tools=[attempt.plan.tool for attempt in outcome.attempts],
        reason=outcome.reason,
    )
    score.expected_tier = expected.expected_tier
    if outcome.disposition is not None:
        score.predicted_tier = outcome.disposition.tier
        score.needs_human = outcome.disposition.needs_human
        if expected.expected_tier:
            score.tier_ok = outcome.disposition.tier == expected.expected_tier
    else:
        score.needs_human = outcome.state != JUDGED
    score.routing_ok = expected_needs_human(expected) == score.needs_human

    # 结论只在"期望有结论且实际也给了结论"时才有意义
    if expected.expected_state == JUDGED and predicted_state == JUDGED:
        score.verdict_ok = outcome.verdict == expected.verdict
    elif expected.expected_state == JUDGED:
        score.verdict_ok = False

    expected_flags = set(expected.flags)
    predicted_flags = set(outcome.flags)
    if expected_flags:
        score.flag_recall = len(expected_flags & predicted_flags) / len(expected_flags)
        score.missing_flags = sorted(expected_flags - predicted_flags)
    score.flag_precision = (
        len(expected_flags & predicted_flags) / len(predicted_flags)
        if predicted_flags
        else 1.0
    )
    score.spurious_flags = sorted(predicted_flags - expected_flags)

    # 关键证据必须出现在**承载结论的来源**里，才算真的用上了。
    # 以前这里比的是 outcome.evidence（检索到的整堆材料），碰到过就算命中：
    # GAP-001 的结论其实用的是 S-002（第三方 19.5 天），标注写的是 S-001，照样记命中。
    # 没有承载来源可查时（工具失败等）留 None，不进分母。
    score.evidence_decisive = list(outcome.decided_by)
    if expected.evidence and outcome.decided_by:
        score.evidence_ok = bool(set(expected.evidence) & set(outcome.decided_by))

    if expected.optimal_tools:
        wanted = set(expected.optimal_tools)
        got = set(score.predicted_tools)
        score.tool_selection = len(wanted & got) / len(wanted)
        score.missing_tools = sorted(wanted - got)

    if expected.reason_contains:
        score.reason_ok = expected.reason_contains in outcome.reason

    return score


def expected_needs_human(expected: GapClaim) -> bool:
    """标注期望的"要不要人过手"。

    以标注的处置档位为准（档位到 needs_human 的映射在 disposition 里），没标档位时退回
    "没给结论就要人看"。

    为什么按动作而不是按原始状态评：状态只区分"有结论／交给人"，而"有结论但结论是
    证据不足"在系统里会经 R12 落回转人工——那种情况动作一致、状态不同，不该记成错误。
    """

    if expected.expected_tier:
        return TIER_NEEDS_HUMAN.get(expected.expected_tier, True)
    return expected.expected_state != JUDGED


def _missing_row(case: GapCase, expected: GapClaim, reason: str) -> ClaimScore:
    return ClaimScore(
        case_id=case.id,
        category=case.category,
        claim_text=expected.text,
        expected_state=expected.expected_state,
        predicted_state="MISSING",
        state_ok=False,
        verdict_ok=False if expected.expected_state == JUDGED else None,
        matched=False,
        reason=reason,
        expected_tier=expected.expected_tier,
        # 该交给人却没交到人手里，是分诊失败；不需要人的主张没拆出来则不计这栏（召回率会记）
        routing_ok=False if expected_needs_human(expected) else None,
    )


def _spurious_row(case: GapCase, outcome: ClaimOutcome) -> ClaimScore:
    """多拆出来的主张：标注里没有对应项。它不影响结果类指标，单独计数。"""

    return ClaimScore(
        case_id=case.id,
        category=case.category,
        claim_text=outcome.claim_text,
        expected_state="",
        predicted_state=outcome.state,
        state_ok=False,
        spurious=True,
        matched=False,
        tool_calls=outcome.tool_calls,
        retried=outcome.tool_calls > 1,
        predicted_tools=[attempt.plan.tool for attempt in outcome.attempts],
        reason=outcome.reason,
        predicted_tier=outcome.disposition.tier if outcome.disposition else "",
        needs_human=outcome.in_review_queue,
    )


def score_case(
    case: GapCase, outcome: ContentOutcome | None, threshold: float = 0.3
) -> list[ClaimScore]:
    """把一条内容的运行结果与标注逐条对齐，返回期望主张的行 + 多拆出来的行。

    对齐按**文本相似度**，不按顺序：主张由系统自己拆（见 runtime/extract.py），
    条数和顺序都不保证与标注一致。按顺序对齐会在"漏拆第一条"时把后面每一条都错位
    配错——那种失败不会报错，只会让所有指标一起变难看。
    """

    if outcome is None:
        return [
            _missing_row(case, expected, "没有产生任何结果")
            for expected in case.expected_claims
        ]

    pairs, _unmatched_expected, unmatched_predicted = match_claims(
        [claim.text for claim in case.expected_claims],
        [claim.claim_text for claim in outcome.claims],
        threshold=threshold,
    )
    paired = {
        expected_index: predicted_index
        for expected_index, predicted_index, _similarity in pairs
    }

    scores: list[ClaimScore] = []
    for index, expected in enumerate(case.expected_claims):
        predicted_index = paired.get(index)
        if predicted_index is None:
            scores.append(_missing_row(case, expected, "该主张没有被拆出来"))
            continue
        scores.append(score_claim(case, expected, outcome.claims[predicted_index]))
    for predicted_index in unmatched_predicted:
        scores.append(_spurious_row(case, outcome.claims[predicted_index]))
    return scores


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def _rate(values: Iterable[bool | None]) -> float | None:
    present = [1.0 if v else 0.0 for v in values if v is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def aggregate(scores: Sequence[ClaimScore]) -> dict[str, Any]:
    """总体指标。

    分母有三种，别混：

    - `claims` 是**标注**主张数，召回率的分母；
    - `extracted_claim_count` 是系统自己拆出来的条数，精确率的分母；
    - 其余结果类指标只在期望主张上算，多拆出来的主张单独计数。
    """

    expected = [s for s in scores if not s.spurious]
    spurious = [s for s in scores if s.spurious]
    judged_expected = [s for s in expected if s.expected_state == JUDGED]
    matched = [s for s in expected if s.matched]
    predicted_total = len(matched) + len(spurious)
    return {
        "claims": len(expected),
        "matched_count": len(matched),
        "extracted_claim_count": predicted_total,
        "spurious_claim_count": len(spurious),
        "missing_claims": [s.claim_text for s in expected if not s.matched],
        "spurious_claims": [s.claim_text for s in spurious],
        "claim_recall": _rate(s.matched for s in expected),
        "claim_precision": (
            round(len(matched) / predicted_total, 4) if predicted_total else None
        ),
        "routing_accuracy": _rate(s.routing_ok for s in expected),
        "state_accuracy": _rate(s.state_ok for s in expected),
        "verdict_accuracy": _rate(s.verdict_ok for s in judged_expected),
        "flag_recall": _mean(s.flag_recall for s in expected),
        "flag_precision": _mean(s.flag_precision for s in expected),
        "spurious_flag_count": sum(len(s.spurious_flags) for s in expected),
        "missing_flag_count": sum(len(s.missing_flags) for s in expected),
        "evidence_hit_rate": _rate(s.evidence_ok for s in expected),
        "evidence_scored_count": sum(1 for s in expected if s.evidence_ok is not None),
        "evidence_unscored_count": sum(
            1 for s in expected if s.evidence_ok is None and s.matched
        ),
        "tool_selection_accuracy": _mean(s.tool_selection for s in expected),
        "reason_match_rate": _rate(s.reason_ok for s in expected),
        "tier_accuracy": _rate(s.tier_ok for s in expected),
        "avg_tool_calls_per_claim": _mean(float(s.tool_calls) for s in expected),
        "retry_rate": _rate(s.retried for s in expected),
        "escalation_rate": _rate(s.predicted_state != JUDGED for s in expected),
        "judged_count": sum(1 for s in expected if s.predicted_state == JUDGED),
        "escalated_count": sum(1 for s in expected if s.predicted_state == "ESCALATED"),
        "tool_failed_count": sum(1 for s in expected if s.predicted_state == "TOOL_FAILED"),
    }


def content_summary(cases: Sequence[GapCase], scores: Sequence[ClaimScore]) -> list[dict]:
    """内容条级口径：用户真正感知的是"每百条内容要处理多少"。"""

    by_case: dict[str, list[ClaimScore]] = {}
    for score in scores:
        by_case.setdefault(score.case_id, []).append(score)

    rows: list[dict] = []
    for case in cases:
        case_scores = by_case.get(case.id, [])
        expected = [s for s in case_scores if not s.spurious]
        spurious = [s for s in case_scores if s.spurious]
        needs_human = [s for s in expected if s.needs_human]
        rows.append(
            {
                "case_id": case.id,
                "category": case.category,
                "claims": len(expected),
                "extracted": sum(1 for s in expected if s.matched) + len(spurious),
                "spurious": len(spurious),
                "missing": [s.claim_text for s in expected if not s.matched],
                "spurious_claims": [s.claim_text for s in spurious],
                "judged": len(expected) - len(needs_human),
                "needs_human": len(needs_human),
                "tool_calls": sum(s.tool_calls for s in case_scores),
                "state_ok": all(s.state_ok for s in expected) if expected else False,
                "tiers": sorted({s.predicted_tier for s in expected if s.predicted_tier}),
            }
        )
    return rows


def v2_ceiling(cases: Sequence[GapCase]) -> dict[str, Any]:
    """v2 在缺口任务上的理论可达上界。

    口径：期望能给出结论的主张里，期望证据**全部**在用户给定资料中的比例。
    上界是对 v2 最有利的假设——它只需要判断正确，不需要检索能力。
    """

    judged_expected: list[GapClaim] = []
    reachable = 0
    unreachable_ids: list[str] = []
    for case in cases:
        given = set(case.given_sources)
        for expected in case.expected_claims:
            if expected.expected_state != JUDGED:
                continue
            judged_expected.append(expected)
            if set(expected.evidence) and set(expected.evidence) <= given:
                reachable += 1
            else:
                unreachable_ids.append(case.id)

    total = len(judged_expected)
    return {
        "judged_expected": total,
        "reachable": reachable,
        "ceiling": round(reachable / total, 4) if total else None,
        "unreachable_cases": unreachable_ids,
        "note": "上界假设 v2 判断全对，只衡量它拿不到关键证据这一件事",
    }

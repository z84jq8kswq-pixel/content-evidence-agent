"""Unit tests for the disposition framework (E / V / H → tier)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.runtime import (  # noqa: E402
    DOWNRANK,
    ESCALATED,
    JUDGED,
    LABEL,
    MANUAL,
    NOTIFY_CREATOR,
    RELEASE,
    TOOL_FAILED,
    VIOLATION,
    Assessment,
    classify,
    distribution,
    evidence_strength,
    harm_level,
    violation_level,
)
from evidence_agent.tools import Evidence  # noqa: E402


def ev(source_id: str, tier: str, text: str = "证据", publisher: str = "") -> Evidence:
    """测试用证据。

    默认带上发布者：真实语料里每条资料都有发布者，独立来源数按发布者去重，
    缺了它就算不出 E 档（E3 是允许对外动作的门槛）。
    """

    meta = {"publisher": publisher or (source_id + " 的发布方"), "tier": tier}
    return Evidence(
        source_id=source_id, tier=tier, date="2025-01-01", text=text, meta=meta
    )


def assessment(items, sufficient: bool = True) -> Assessment:
    return Assessment(sufficient=sufficient, items=list(items), reason="测试")


STRONG = assessment([ev("S-001", "A"), ev("S-002", "B")])
SINGLE = assessment([ev("S-001", "A")])
WEAK = assessment([ev("S-004", "D")], sufficient=False)


def vendor_ev(source_id: str, publisher: str = "") -> Evidence:
    """厂商自述类的证据（品牌官方声明页）。"""

    meta = {
        "publisher": publisher or (source_id + " 的发布方"),
        "tier": "A",
        "kind": "品牌官方声明",
    }
    return Evidence(
        source_id=source_id, tier="A", date="2025-01-01", text="厂商标称", meta=meta
    )


def independent_ev(source_id: str) -> Evidence:
    meta = {"publisher": source_id + " 检测机构", "tier": "B", "kind": "独立检测"}
    return Evidence(
        source_id=source_id, tier="B", date="2025-01-01", text="检测报告", meta=meta
    )


VENDOR_ONLY = assessment([vendor_ev("S-008"), vendor_ev("S-009")])
VENDOR_PLUS_INDEPENDENT = assessment([vendor_ev("S-008"), independent_ev("S-002")])


class EvidenceStrengthTests(unittest.TestCase):
    def test_tool_failure_is_e0(self) -> None:
        self.assertEqual(evidence_strength(TOOL_FAILED, None), "E0")

    def test_insufficient_is_e1(self) -> None:
        self.assertEqual(evidence_strength(ESCALATED, WEAK), "E1")

    def test_single_strong_source_is_e2(self) -> None:
        self.assertEqual(evidence_strength(JUDGED, SINGLE), "E2")

    def test_two_independent_strong_sources_is_e3(self) -> None:
        self.assertEqual(evidence_strength(JUDGED, STRONG), "E3")

    def test_two_weak_sources_do_not_count(self) -> None:
        weak_pair = assessment([ev("S-003", "C"), ev("S-004", "D")], sufficient=False)
        self.assertEqual(evidence_strength(JUDGED, weak_pair), "E1")

    def test_two_documents_from_one_publisher_are_e2(self) -> None:
        """同一家机构的两份材料只是一个来源：E3 撑不起按资料编号去重。"""

        same = assessment(
            [ev("S-001", "A", publisher="同一家机构"), ev("S-002", "B", publisher="同一家机构")]
        )
        self.assertEqual(evidence_strength(JUDGED, same), "E2")

    def test_missing_publisher_is_not_independent(self) -> None:
        """发布者不明时不能声称独立：退回资料编号会虚增来源数，抬高档位。"""

        unknown = assessment(
            [
                Evidence(source_id="S-001", tier="A", date="2025-01-01", text="证据"),
                Evidence(source_id="S-002", tier="B", date="2025-01-01", text="证据"),
            ]
        )
        self.assertEqual(evidence_strength(JUDGED, unknown), "E2")


class HarmLevelTests(unittest.TestCase):
    def test_claim_type_wins(self) -> None:
        self.assertEqual(harm_level("health_safety", "随便一句话"), "H3")
        self.assertEqual(harm_level("certification", "随便一句话"), "H3")

    def test_high_risk_keywords(self) -> None:
        self.assertEqual(harm_level("numeric", "电池过热隐患"), "H3")
        self.assertEqual(harm_level("numeric", "额定能量 74Wh"), "H3")
        self.assertEqual(harm_level("other", "坐飞机直接带上机"), "H3")

    def test_consumption_word_wins_over_subjective_word(self) -> None:
        """同一句里既有消费参数又有主观词时，按消费参数判档（H2）。

        顺序反了会判成 H1（体验类），处置跟着变轻：H1 走 R10 只通知创作者，
        H2 该走 R8 降级分发 + 通知。2026-10-14 修。
        """

        self.assertEqual(harm_level("other", "容量 20000mAh，推荐"), "H2")
        self.assertEqual(harm_level("other", "续航 21 天，用着挺好用"), "H2")
        self.assertEqual(harm_level("other", "用着挺好用"), "H1")


    def test_consumption_keywords_are_medium(self) -> None:
        self.assertEqual(harm_level("numeric", "续航 60 天"), "H2")

    def test_subjective_words_are_low(self) -> None:
        self.assertEqual(harm_level("other", "我用着很好用"), "H1")

    def test_default_is_medium(self) -> None:
        self.assertEqual(harm_level("other", "这是一句没有关键词的话"), "H2")


class ViolationLevelTests(unittest.TestCase):
    def test_supported_claim_is_never_a_violation(self) -> None:
        self.assertEqual(violation_level("SUPPORTED", "获得认证"), "V0")

    def test_insufficient_claim_cannot_be_accused(self) -> None:
        self.assertEqual(violation_level("INSUFFICIENT", "获得认证"), "V0")

    def test_contradicted_with_marketing_is_v2(self) -> None:
        self.assertEqual(
            violation_level("CONTRADICTED", "续航 60 天", "本篇为品牌方合作推广"), "V2"
        )

    def test_contradicted_without_marketing_is_v1(self) -> None:
        self.assertEqual(violation_level("CONTRADICTED", "续航 60 天", "使用体验分享"), "V1")

    def test_partial_is_v1(self) -> None:
        self.assertEqual(violation_level("PARTIAL", "续航不错"), "V1")


class ClassifyTests(unittest.TestCase):
    def _classify(self, *, state=JUDGED, verdict="CONTRADICTED", claim_type="numeric",
                  claim_text="续航 60 天", assessment_obj=STRONG, content=""):
        return classify(
            state=state,
            verdict=verdict,
            claim_type=claim_type,
            claim_text=claim_text,
            assessment=assessment_obj,
            content=content,
        )

    def test_tool_failure_goes_to_manual(self) -> None:
        result = self._classify(state=TOOL_FAILED, assessment_obj=None)
        self.assertEqual(result.tier, MANUAL)
        self.assertEqual(result.rule, "R1")

    def test_weak_evidence_on_high_harm_is_high_priority(self) -> None:
        result = self._classify(
            state=ESCALATED,
            verdict="INSUFFICIENT",
            claim_type="health_safety",
            claim_text="电池过热隐患",
            assessment_obj=WEAK,
        )
        self.assertEqual(result.tier, MANUAL)
        self.assertEqual(result.rule, "R2")
        self.assertEqual(result.priority, "high")

    def test_weak_evidence_blocks_all_outbound_actions(self) -> None:
        result = self._classify(
            state=ESCALATED, verdict="INSUFFICIENT", assessment_obj=WEAK
        )
        self.assertEqual(result.tier, MANUAL)
        self.assertFalse(result.notify_creator)

    def test_supported_claim_is_released(self) -> None:
        result = self._classify(verdict="SUPPORTED", claim_text="容量 20000mAh")
        self.assertEqual(result.tier, RELEASE)
        self.assertFalse(result.needs_human)

    def test_contradicted_marketing_claim_goes_to_violation(self) -> None:
        result = self._classify(content="品牌方合作推广，续航 60 天")
        self.assertEqual(result.violation, "V2")
        self.assertEqual(result.tier, VIOLATION)
        self.assertTrue(result.needs_human)

    def test_contradicted_high_harm_is_labelled(self) -> None:
        result = self._classify(
            claim_type="certification", claim_text="获得认证", content="笔记"
        )
        self.assertEqual(result.tier, LABEL)
        self.assertEqual(result.rule, "R6")
        self.assertFalse(result.needs_human)

    def test_contradicted_medium_harm_is_downranked_and_notified(self) -> None:
        result = self._classify(claim_text="续航 60 天")
        self.assertEqual(result.tier, DOWNRANK)
        self.assertEqual(result.rule, "R8")
        self.assertTrue(result.notify_creator)
        self.assertFalse(result.needs_human)

    def test_single_source_is_not_enough_for_outbound_action(self) -> None:
        result = self._classify(assessment_obj=SINGLE)
        self.assertEqual(result.tier, MANUAL)
        self.assertEqual(result.rule, "R9")

    def test_sufficient_evidence_without_conclusion_goes_to_manual(self) -> None:
        """证据够了却给不出结论，是能力缺口，不能据此对外动手。"""

        result = self._classify(verdict="INSUFFICIENT", assessment_obj=STRONG)
        self.assertEqual(result.tier, MANUAL)
        self.assertEqual(result.rule, "R12")
        self.assertFalse(result.notify_creator)

    def test_low_harm_only_notifies_creator(self) -> None:
        result = self._classify(claim_text="我用着很好用")
        self.assertEqual(result.tier, NOTIFY_CREATOR)
        self.assertEqual(result.rule, "R10")

    def test_order_is_evidence_then_violation_then_harm(self) -> None:
        """先看证据：证据不足时，危害再大也不能对外动手。"""

        result = self._classify(
            state=ESCALATED,
            verdict="CONTRADICTED",
            claim_type="health_safety",
            claim_text="电池过热会爆炸",
            assessment_obj=WEAK,
        )
        self.assertEqual(result.tier, MANUAL)
        self.assertEqual(result.notify_creator, False)

    def test_disposition_label_and_dict(self) -> None:
        result = self._classify(claim_text="续航 60 天")
        self.assertEqual(result.label, "降级分发")
        payload = result.to_dict()
        self.assertEqual(payload["tier"], DOWNRANK)
        self.assertEqual(payload["evidence"], "E3")
        self.assertEqual(payload["harm"], "H2")
        self.assertEqual(payload["violation"], "V1")
        self.assertEqual(result.risk, 2)

    def test_vendor_only_blocks_outward_action(self) -> None:
        """依据只有厂商自述时，最重的动作止步于转人工。"""

        result = self._classify(
            assessment_obj=VENDOR_ONLY,
            claim_text="续航 60 天",
            content="本条为推广内容",
        )
        self.assertEqual(result.tier, MANUAL)
        self.assertEqual(result.rule, "R13")
        self.assertTrue(result.vendor_only)
        self.assertFalse(result.notify_creator)

    def test_vendor_only_still_records_supported_conclusion(self) -> None:
        """结论照常给出：被拦的是动作，不是结论。"""

        result = self._classify(
            verdict="SUPPORTED", assessment_obj=VENDOR_ONLY, claim_text="容量 20000mAh"
        )
        self.assertEqual(result.tier, RELEASE)
        self.assertEqual(result.rule, "R4")
        self.assertTrue(result.vendor_only)

    def test_vendor_only_keeps_creator_notice(self) -> None:
        """通知创作者不是对外动作，厂商自述下的轻动作保留。"""

        result = self._classify(
            assessment_obj=VENDOR_ONLY, claim_text="我用着很好用", content=""
        )
        self.assertEqual(result.tier, NOTIFY_CREATOR)
        self.assertEqual(result.rule, "R10")
        self.assertTrue(result.notify_creator)

    def test_independent_source_keeps_outward_action(self) -> None:
        """有独立来源兜底时不受这条门槛限制。"""

        result = self._classify(
            assessment_obj=VENDOR_PLUS_INDEPENDENT,
            claim_text="续航 60 天",
            content="本条为推广内容",
        )
        self.assertEqual(result.tier, VIOLATION)
        self.assertEqual(result.rule, "R5")
        self.assertFalse(result.vendor_only)

    def test_vendor_only_flag_is_recorded(self) -> None:
        payload = self._classify(assessment_obj=VENDOR_ONLY).to_dict()
        self.assertTrue(payload["vendor_only"])

    def test_distribution_counts_tiers(self) -> None:
        rows = [
            self._classify(claim_text="续航 60 天"),
            self._classify(verdict="SUPPORTED", claim_text="容量 20000mAh"),
            self._classify(claim_text="续航 60 天"),
        ]
        counts = distribution(rows)
        self.assertEqual(counts[DOWNRANK], 2)
        self.assertEqual(counts[RELEASE], 1)


if __name__ == "__main__":
    unittest.main()

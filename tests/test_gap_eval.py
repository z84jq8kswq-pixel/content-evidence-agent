"""Unit tests for gap-task loading, scoring and the v2 ceiling."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.gap_eval import (  # noqa: E402
    ClaimScore,
    GapDataError,
    aggregate,
    content_summary,
    load_gap_cases,
    score_case,
    score_claim,
    v2_ceiling,
)
from evidence_agent.runtime import (  # noqa: E402
    ClaimOutcome,
    ContentOutcome,
    extract_claim_texts,
    make_agent,
)
from evidence_agent.tools import LocalCorpus  # noqa: E402

GAP_FILE = ROOT / "data" / "gap_tasks.json"
CORPUS_DIR = ROOT / "data" / "corpus"


def write_gap(payload: dict) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "gap.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return tmp


def claim_dict(**overrides) -> dict:
    base = {
        "text": "青禾 X1 续航 60 天",
        "expected_state": "JUDGED",
        "verdict": "CONTRADICTED",
        "flags": [],
        "evidence": ["S-001"],
        "optimal_tools": ["lookup_fact"],
        "reason_contains": "相差",
        "note": "",
    }
    base.update(overrides)
    return base


def case_dict(**overrides) -> dict:
    base = {
        "id": "GAP-X",
        "category": "测试",
        "difficulty": "easy",
        "content": "青禾 X1 的内容",
        "given_sources": ["S-004"],
        "expected_claims": [claim_dict()],
        "what_it_tests": "测试",
    }
    base.update(overrides)
    return base


class LoaderTests(unittest.TestCase):
    def test_loads_real_gap_file(self) -> None:
        raw, cases = load_gap_cases(GAP_FILE)
        self.assertEqual(raw["gap_version"], "g4")
        self.assertEqual(len(cases), 19)
        self.assertEqual(cases[0].id, "GAP-001")
        self.assertEqual(cases[4].given_sources, ())

    def test_rejects_duplicate_ids(self) -> None:
        payload = {"cases": [case_dict(), case_dict()]}
        with self.assertRaises(GapDataError):
            load_gap_cases(write_gap(payload))

    def test_rejects_case_without_claims(self) -> None:
        payload = {"cases": [case_dict(expected_claims=[])]}
        with self.assertRaises(GapDataError):
            load_gap_cases(write_gap(payload))

    def test_rejects_unknown_expected_state(self) -> None:
        payload = {"cases": [case_dict(expected_claims=[claim_dict(expected_state="MAYBE")])]}
        with self.assertRaises(GapDataError):
            load_gap_cases(write_gap(payload))

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(GapDataError):
            load_gap_cases(ROOT / "data" / "does-not-exist.json")


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        _, self.cases = load_gap_cases(GAP_FILE)
        self.case = self.cases[0]
        self.expected = self.case.expected_claims[0]

    def test_perfect_outcome_scores_full(self) -> None:
        outcome = ClaimOutcome(
            claim_text=self.expected.text,
            claim_type="numeric",
            state="JUDGED",
            verdict="CONTRADICTED",
            reason="主张数值 60天 与最权威来源相差 186%",
            flags=[],
            evidence=["S-001"],
            decided_by=["S-001"],
            attempts=[_fake_attempt("lookup_fact")],
        )
        score = score_claim(self.case, self.expected, outcome)
        self.assertTrue(score.state_ok)
        self.assertTrue(score.verdict_ok)
        self.assertEqual(score.tool_selection, 1.0)
        self.assertTrue(score.reason_ok)
        self.assertTrue(score.evidence_ok)
        self.assertEqual(score.flag_precision, 1.0)
        self.assertFalse(score.retried)

    def test_verdict_is_ignored_when_model_should_have_escalated(self) -> None:
        expected = self.expected.__class__(
            **{**self.expected.__dict__, "expected_state": "ESCALATED", "verdict": "INSUFFICIENT"}
        )
        outcome = ClaimOutcome(
            claim_text=expected.text,
            claim_type="numeric",
            state="JUDGED",
            verdict="CONTRADICTED",
            reason="相差",
        )
        score = score_claim(self.case, expected, outcome)
        self.assertFalse(score.state_ok)
        self.assertIsNone(score.verdict_ok)  # 期望转人工时，判定对错没有意义

    def test_missing_expected_verdict_counts_as_wrong(self) -> None:
        outcome = ClaimOutcome(
            claim_text=self.expected.text,
            claim_type="numeric",
            state="ESCALATED",
            verdict="INSUFFICIENT",
            reason="检索策略已用尽",
        )
        score = score_claim(self.case, self.expected, outcome)
        self.assertFalse(score.verdict_ok)

    def test_spurious_flag_is_recorded(self) -> None:
        expected = self.expected.__class__(
            **{**self.expected.__dict__, "expected_state": "ESCALATED", "flags": ()}
        )
        outcome = ClaimOutcome(
            claim_text=expected.text,
            claim_type="certification",
            state="ESCALATED",
            verdict="INSUFFICIENT",
            reason="过期",
            flags=["STALE"],
        )
        score = score_claim(self.case, expected, outcome)
        self.assertEqual(score.spurious_flags, ["STALE"])
        self.assertEqual(score.flag_precision, 0.0)

    def test_missing_flag_is_recorded(self) -> None:
        expected = self.expected.__class__(
            **{**self.expected.__dict__, "flags": ("STALE", "SOURCE_CONFLICT")}
        )
        outcome = ClaimOutcome(
            claim_text=expected.text,
            claim_type="certification",
            state="ESCALATED",
            verdict="INSUFFICIENT",
            reason="冲突",
            flags=["STALE"],
        )
        score = score_claim(self.case, expected, outcome)
        self.assertEqual(score.missing_flags, ["SOURCE_CONFLICT"])
        self.assertEqual(score.flag_recall, 0.5)

    def test_missing_tool_is_recorded(self) -> None:
        expected = self.expected.__class__(
            **{**self.expected.__dict__, "optimal_tools": ("lookup_fact", "fetch_source")}
        )
        outcome = ClaimOutcome(
            claim_text=expected.text,
            claim_type="numeric",
            state="JUDGED",
            verdict="CONTRADICTED",
            reason="相差",
            attempts=[_fake_attempt("lookup_fact")],
        )
        score = score_claim(self.case, expected, outcome)
        self.assertEqual(score.tool_selection, 0.5)
        self.assertEqual(score.missing_tools, ["fetch_source"])

    def test_no_outcome_marks_case_as_missing(self) -> None:
        rows = score_case(self.case, None)
        self.assertEqual(len(rows), len(self.case.expected_claims))
        self.assertEqual(rows[0].predicted_state, "MISSING")
        self.assertFalse(rows[0].state_ok)

    def test_alignment_is_by_text_not_by_order(self) -> None:
        """对齐按文本相似度：前面多一条不该把后面的判定全部错位配错。"""

        outcome = ContentOutcome(
            content_id=self.case.id,
            claims=[
                ClaimOutcome(
                    claim_text="多出来的另一条主张",
                    claim_type="other",
                    state="JUDGED",
                    verdict="SUPPORTED",
                    reason="",
                ),
                ClaimOutcome(
                    claim_text=self.expected.text,
                    claim_type="numeric",
                    state="JUDGED",
                    verdict="CONTRADICTED",
                    reason="相差",
                ),
            ],
        )
        rows = score_case(self.case, outcome)
        expected_row = next(row for row in rows if not row.spurious)
        spurious = [row for row in rows if row.spurious]

        self.assertTrue(expected_row.matched)
        self.assertTrue(expected_row.state_ok)
        self.assertEqual(len(spurious), 1)
        self.assertEqual(spurious[0].claim_text, "多出来的另一条主张")

    def test_evidence_must_be_carried_by_the_decision(self) -> None:
        """关键证据必须出现在承载结论的来源里。

        以前比的是"检索到过的整堆材料"，碰到过就算命中：GAP-001 的结论其实用的是
        S-002（第三方 19.5 天），标注写的是 S-001（厂商标称 21 天），照样记命中。
        """

        outcome = ClaimOutcome(
            claim_text=self.expected.text,
            claim_type="numeric",
            state="JUDGED",
            verdict="CONTRADICTED",
            reason="相差",
            evidence=["S-001", "S-002"],
            decided_by=["S-002"],
        )
        score = score_claim(self.case, self.expected, outcome)

        self.assertEqual(score.evidence_decisive, ["S-002"])
        self.assertFalse(score.evidence_ok)

    def test_evidence_is_unscored_without_a_decisive_source(self) -> None:
        """工具失败这类情况没有承载来源：不能算命中，也不能算漏。"""

        outcome = ClaimOutcome(
            claim_text=self.expected.text,
            claim_type="numeric",
            state="TOOL_FAILED",
            verdict="INSUFFICIENT",
            reason="工具不可用",
            evidence=["S-001"],
        )
        score = score_claim(self.case, self.expected, outcome)
        self.assertIsNone(score.evidence_ok)

    def test_unmatched_expected_claim_is_marked_as_not_extracted(self) -> None:
        outcome = ContentOutcome(
            content_id=self.case.id,
            claims=[
                ClaimOutcome(
                    claim_text="完全无关的句子",
                    claim_type="other",
                    state="JUDGED",
                    verdict="SUPPORTED",
                    reason="",
                )
            ],
        )
        rows = score_case(self.case, outcome)
        expected_row = next(row for row in rows if not row.spurious)

        self.assertFalse(expected_row.matched)
        self.assertEqual(expected_row.predicted_state, "MISSING")
        self.assertIn("没有被拆出来", expected_row.reason)

    def test_extra_outcomes_are_recorded_as_spurious(self) -> None:
        outcome = ContentOutcome(
            content_id=self.case.id,
            claims=[
                ClaimOutcome(
                    claim_text=self.expected.text,
                    claim_type="numeric",
                    state="JUDGED",
                    verdict="CONTRADICTED",
                    reason="相差",
                ),
                ClaimOutcome(
                    claim_text="标注里没有的多余主张",
                    claim_type="other",
                    state="JUDGED",
                    verdict="SUPPORTED",
                    reason="",
                ),
            ],
        )
        rows = score_case(self.case, outcome)
        self.assertEqual(len(rows), 2)
        spurious = [row for row in rows if row.spurious]
        self.assertEqual([row.claim_text for row in spurious], ["标注里没有的多余主张"])
        self.assertEqual(self.metrics_of(rows)["claim_precision"], 0.5)

    @staticmethod
    def metrics_of(rows):
        return aggregate(rows)


def _fake_attempt(tool: str):
    from evidence_agent.runtime.agent import Attempt
    from evidence_agent.runtime.planner import Plan
    from evidence_agent.tools import STATUS_OK, ToolResult

    return Attempt(
        plan=Plan(tool=tool, params={}, reason="测试", strategy="baseline"),
        result=ToolResult(tool=tool, status=STATUS_OK),
        assessment=None,  # type: ignore[arg-type]
    )


class AggregateTests(unittest.TestCase):
    def _rows(self) -> list[ClaimScore]:
        return [
            ClaimScore("A", "x", "t1", "JUDGED", "JUDGED", True, verdict_ok=True, tool_selection=1.0, tool_calls=1),
            ClaimScore("B", "x", "t2", "ESCALATED", "JUDGED", False, tool_selection=1.0, tool_calls=2, retried=True),
            ClaimScore("C", "x", "t3", "ESCALATED", "ESCALATED", True, tool_selection=0.5, tool_calls=1),
        ]

    def test_aggregate_rates(self) -> None:
        metrics = aggregate(self._rows())
        self.assertEqual(metrics["claims"], 3)
        self.assertAlmostEqual(metrics["state_accuracy"], 2 / 3, places=4)
        self.assertEqual(metrics["verdict_accuracy"], 1.0)  # 只有 A 期望有结论
        self.assertAlmostEqual(metrics["tool_selection_accuracy"], 2.5 / 3, places=4)
        self.assertAlmostEqual(metrics["retry_rate"], 1 / 3, places=4)
        self.assertAlmostEqual(metrics["escalation_rate"], 1 / 3, places=4)
        self.assertEqual(metrics["judged_count"], 2)
        self.assertEqual(metrics["escalated_count"], 1)

    def test_spurious_and_missing_flag_counts(self) -> None:
        rows = self._rows()
        rows[0].spurious_flags = ["STALE"]
        rows[1].missing_flags = ["SOURCE_CONFLICT"]
        metrics = aggregate(rows)
        self.assertEqual(metrics["spurious_flag_count"], 1)
        self.assertEqual(metrics["missing_flag_count"], 1)

    def test_content_summary_counts_per_content(self) -> None:
        _, cases = load_gap_cases(GAP_FILE)
        rows = [
            ClaimScore(cases[0].id, cases[0].category, "t", "JUDGED", "JUDGED", True, tool_calls=1),
            ClaimScore(
                cases[1].id,
                cases[1].category,
                "t",
                "ESCALATED",
                "ESCALATED",
                True,
                tool_calls=2,
                needs_human=True,
                predicted_tier="manual",
            ),
        ]
        summary = content_summary(cases[:2], rows)
        self.assertEqual(summary[0]["judged"], 1)
        self.assertEqual(summary[0]["needs_human"], 0)
        self.assertEqual(summary[1]["needs_human"], 1)
        self.assertEqual(summary[1]["tool_calls"], 2)
        self.assertEqual(summary[1]["tiers"], ["manual"])


class CeilingTests(unittest.TestCase):
    def test_ceiling_counts_unreachable_claims(self) -> None:
        _, cases = load_gap_cases(GAP_FILE)
        ceiling = v2_ceiling(cases)
        self.assertEqual(ceiling["judged_expected"], 14)
        self.assertEqual(ceiling["reachable"], 0)
        self.assertEqual(ceiling["ceiling"], 0.0)
        self.assertEqual(
            ceiling["unreachable_cases"],
            [
                "GAP-001",
                "GAP-003",
                "GAP-005",
                "GAP-008",
                "GAP-008",
                "GAP-008",
                "GAP-009",
                "GAP-009",
                "GAP-010",
                "GAP-010",
                "GAP-011",
                "GAP-013",
                "GAP-014",
                "GAP-014",
            ],
        )

    def test_ceiling_is_one_when_evidence_is_given(self) -> None:
        payload = {
            "cases": [
                case_dict(
                    given_sources=["S-001", "S-004"],
                    expected_claims=[claim_dict(evidence=["S-001"])],
                )
            ]
        }
        _, cases = load_gap_cases(write_gap(payload))
        ceiling = v2_ceiling(cases)
        self.assertEqual(ceiling["ceiling"], 1.0)

    def test_ceiling_ignores_escalation_claims(self) -> None:
        payload = {
            "cases": [
                case_dict(
                    given_sources=[],
                    expected_claims=[claim_dict(expected_state="ESCALATED", flags=(), evidence=())],
                )
            ]
        }
        _, cases = load_gap_cases(write_gap(payload))
        ceiling = v2_ceiling(cases)
        self.assertEqual(ceiling["judged_expected"], 0)
        self.assertIsNone(ceiling["ceiling"])


class EndToEndTests(unittest.TestCase):
    """跑真实的缺口任务集，把基线指标锁住——指标变了必须是有意的。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = LocalCorpus.from_dir(CORPUS_DIR)
        _, cls.cases = load_gap_cases(GAP_FILE)
        agent = make_agent(cls.corpus)
        cls.scores = []
        for case in cls.cases:
            # 关键：主张由系统自己拆，不读标注。拿标注当输入等于把答案抄进题面。
            claim_texts = extract_claim_texts(case.content)
            outcome = (
                agent.run_content(case.id, claim_texts, content=case.content)
                if claim_texts
                else None
            )
            cls.scores.extend(score_case(case, outcome))
        cls.metrics = aggregate(cls.scores)

    def test_extraction_metrics_are_locked(self) -> None:
        """拆主张这一步的指标锁在这里：它现在是端到端口径的一部分。

        两条多拆出来的都是内容里真实存在的说法，标注没标——属于任务集的已知缺口，
        不是运行时错误。真要收紧精确率，得先把这两条标注进去或明确允许。
        """

        self.assertEqual(self.metrics["claim_recall"], 1.0)
        self.assertEqual(self.metrics["claim_precision"], 0.9286)
        self.assertEqual(self.metrics["spurious_claim_count"], 2)
        self.assertEqual(self.metrics["missing_claims"], [])
        self.assertEqual(
            self.metrics["spurious_claims"],
            ["云石 P20 充电宝容量 20000mAh", "20000mAh 大容量"],
        )

    def test_routing_is_scored_by_action_not_by_state(self) -> None:
        """分诊一致率按处置档位评：规则基线的动作全对（8/8）。"""

        self.assertEqual(self.metrics["routing_accuracy"], 1.0)

    def test_rule_baseline_misses_the_low_quality_flag(self) -> None:
        """GAP-004 的标志位标注补上后，规则基线的标志位召回掉到 2/3。

        规则层判"证据充分"时用的是最权威的那条（S-007），根本没把 S-012 当证据，
        所以标不出 LOW_SOURCE_QUALITY；模型判定那条会标。这个差异以前被漏标的标注盖住了。
        """

        self.assertEqual(self.metrics["flag_recall"], 0.8571)
        missed = [s.claim_text for s in self.scores if s.missing_flags]
        self.assertIn("云石 P20 有电池过热隐患", missed)

    def test_known_defects_have_a_locked_signature(self) -> None:
        """扩样之后剩下的错误应当只有这两个已知缺陷。

        它们是这套任务集存在的理由：认证粒度太粗（三题全中）、同一主体的权威文档被当成
        证据（一题）。数量变了就说明缺陷被修了或被扩大了，必须是有意的。
        """

        missed_flags = sorted(
            (s.case_id, flag) for s in self.scores for flag in s.missing_flags
        )
        spurious_flags = sorted(
            (s.case_id, flag) for s in self.scores for flag in s.spurious_flags
        )
        reason_misses = sorted(s.case_id for s in self.scores if s.reason_ok is False)

        self.assertEqual(
            missed_flags,
            [("GAP-004", "LOW_SOURCE_QUALITY")],
        )
        self.assertEqual(
            spurious_flags,
            [
                ("GAP-007", "STALE"),
                ("GAP-017", "STALE"),
            ],
        )
        self.assertEqual(
            reason_misses,
            [
                "GAP-004",
                "GAP-007",
                "GAP-017",
                "GAP-018",
                "GAP-019",
            ],
        )

    def test_state_accuracy_is_perfect_on_baseline(self) -> None:
        self.assertEqual(self.metrics["state_accuracy"], 1.0)

    def test_verdict_and_tool_selection_are_perfect(self) -> None:
        self.assertEqual(self.metrics["verdict_accuracy"], 1.0)
        self.assertEqual(self.metrics["tool_selection_accuracy"], 1.0)

    def test_core_evidence_rate_counts_only_what_carried_the_decision(self) -> None:
        """关键证据进结论率：26 条里 20 条可评，命中 18 条。

        漏掉的两条正是这次收紧口径要暴露的：

        - GAP-001：结论由 S-002（第三方 19.5 天）承载，标注的关键证据是 S-001（厂商标称 21 天）；
        - GAP-004：结论由 S-007（监管文档）顶成"证据充分"，标注的关键证据是低等级帖 S-012。

        另两条（GAP-006／GAP-007）标注没给关键证据，不参与评分。
        """

        self.assertEqual(self.metrics["evidence_hit_rate"], 0.9)
        self.assertEqual(self.metrics["evidence_scored_count"], 20)
        self.assertEqual(self.metrics["evidence_unscored_count"], 6)
        missed = [s.case_id for s in self.scores if s.evidence_ok is False]
        self.assertEqual(missed, ["GAP-001", "GAP-004"])

    def test_baseline_still_has_known_reason_and_flag_defects(self) -> None:
        """这两个指标故意不满分——它们记录的是已知缺陷，修好之后应当上升。"""

        self.assertLess(self.metrics["reason_match_rate"], 1.0)
        self.assertLess(self.metrics["flag_precision"], 1.0)

    def test_retry_path_is_exercised(self) -> None:
        self.assertGreater(self.metrics["retry_rate"], 0.0)

    def test_every_claim_escalation_is_justified(self) -> None:
        for score in [s for s in self.scores if not s.spurious]:
            if score.predicted_state != "JUDGED":
                self.assertTrue(score.reason, score.claim_text)


if __name__ == "__main__":
    unittest.main()

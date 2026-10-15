"""Unit tests for claim highlighting."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.highlight import (  # noqa: E402
    Highlight,
    authoritative_value,
    highlights_for,
    metric_near,
    number_verdict,
    split_number,
)
from evidence_agent.tools import LocalCorpus  # noqa: E402

CORPUS_DIR = ROOT / "data" / "corpus"

FACTS = [
    {"entity": "青禾 X1", "metric": "续航", "value": 21, "unit": "天",
     "tier": "A", "date": "2025-03-12", "source_id": "S-001"},
    {"entity": "青禾 X1", "metric": "续航", "value": 19.5, "unit": "天",
     "tier": "B", "date": "2025-05-20", "source_id": "S-002"},
    {"entity": "青禾 X1", "metric": "续航", "value": 60, "unit": "天",
     "tier": "D", "date": "2024-06-15", "source_id": "S-004"},
]


class SplitNumberTests(unittest.TestCase):
    def test_splits_value_and_unit(self) -> None:
        self.assertEqual(split_number("60 天"), (60.0, "天"))
        self.assertEqual(split_number("74Wh"), (74.0, "Wh"))
        self.assertEqual(split_number("20000mAh"), (20000.0, "mAh"))

    def test_handles_decimal(self) -> None:
        self.assertEqual(split_number("19.5 天"), (19.5, "天"))


class AuthoritativeValueTests(unittest.TestCase):
    def test_prefers_higher_tier(self) -> None:
        value, row = authoritative_value(FACTS, "天")
        self.assertEqual(value, 21.0)
        self.assertEqual(row["source_id"], "S-001")

    def test_newer_wins_when_tiers_tie(self) -> None:
        """等级相同时应取更新的记录，否则会拿旧值去比新主张。"""

        facts = [
            {"value": 74, "unit": "Wh", "tier": "B", "date": "2025-04-18", "source_id": "S-008"},
            {"value": 62, "unit": "Wh", "tier": "B", "date": "2024-12-05", "source_id": "S-010"},
        ]
        value, row = authoritative_value(facts, "Wh")
        self.assertEqual(value, 74.0)
        self.assertEqual(row["source_id"], "S-008")

    def test_returns_none_for_unknown_unit(self) -> None:
        self.assertIsNone(authoritative_value(FACTS, "公斤"))

    def test_ignores_non_numeric_values(self) -> None:
        facts = [{"value": "未列入", "unit": "", "tier": "A", "date": "2025-06-01"}]
        self.assertIsNone(authoritative_value(facts, ""))


class HighlightTests(unittest.TestCase):
    def test_flags_mismatched_number(self) -> None:
        marks = highlights_for("青禾 X1 续航 60 天", FACTS)
        numbers = [m for m in marks if m.kind.startswith("number")]
        self.assertEqual(len(numbers), 1)
        self.assertEqual(numbers[0].kind, "number-bad")
        self.assertIn("186%", numbers[0].note)
        self.assertIn("S-001", numbers[0].note)

    def test_flags_matching_number(self) -> None:
        marks = highlights_for("青禾 X1 续航 21 天", FACTS)
        numbers = [m for m in marks if m.kind.startswith("number")]
        self.assertEqual(numbers[0].kind, "number-ok")

    def test_unknown_unit_is_neutral(self) -> None:
        marks = highlights_for("一次能给手机充 5 次", FACTS)
        self.assertTrue(any(m.kind == "number" for m in marks))
        self.assertFalse(any(m.kind == "number-bad" for m in marks))

    def test_flags_assertion_words(self) -> None:
        marks = highlights_for("获得口腔护理协会认证", FACTS)
        self.assertTrue(any(m.kind == "assertion" for m in marks))
        self.assertEqual([m.text for m in marks if m.kind == "assertion"], ["认证"])

    def test_offsets_point_at_the_marked_text(self) -> None:
        text = "青禾 X1 续航 60 天，还获得过认证。"
        for mark in highlights_for(text, FACTS):
            self.assertEqual(text[mark.start : mark.end], mark.text)

    def test_marks_are_sorted_by_position(self) -> None:
        text = "唯一一款续航 60 天并通过认证的产品"
        marks = highlights_for(text, FACTS)
        starts = [m.start for m in marks]
        self.assertEqual(starts, sorted(starts))

    def test_repeated_assertion_word_is_found_each_time(self) -> None:
        marks = highlights_for("认证之后再认证", FACTS)
        self.assertEqual(len([m for m in marks if m.text == "认证"]), 2)

    def test_flags_capability_wording_without_numbers(self) -> None:
        """"可以带上飞机"没有数字也不是排他词，但它是可核实的许可类说法。"""

        marks = highlights_for("云石 P20 额定能量 74Wh，可以带上飞机。", FACTS)
        texts = [m.text for m in marks if m.kind == "assertion"]
        self.assertIn("可以带上飞机", texts)

    def test_longer_match_wins_at_same_position(self) -> None:
        marks = highlights_for("可以带上飞机", ())
        self.assertEqual([m.text for m in marks], ["可以带上飞机"])

    def test_empty_text_is_safe(self) -> None:
        self.assertEqual(highlights_for("", FACTS), [])

    def test_highlight_dict_shape(self) -> None:
        mark = Highlight("60 天", 7, 11, "number-bad", "差得多")
        self.assertEqual(
            set(mark.to_dict()), {"text", "start", "end", "kind", "note"}
        )

    def test_real_corpus_produces_highlights(self) -> None:
        corpus = LocalCorpus.from_dir(CORPUS_DIR)
        marks = highlights_for("云石 P20 额定能量 74Wh", corpus.facts)
        self.assertTrue(marks)
        self.assertTrue(all(0 <= m.start < m.end for m in marks))


class NumberVerdictTests(unittest.TestCase):
    def test_metric_is_picked_by_distance(self) -> None:
        """一句话里两个数值属于不同指标时，各取最近的那个。"""

        text = "云石 P20 充电宝开箱：20000mAh 大容量，额定能量 74Wh。"
        first = text.index("20000mAh")
        second = text.index("74Wh")
        self.assertEqual(metric_near(text, first, first + 8), "容量")
        self.assertEqual(metric_near(text, second, second + 4), "额定能量")

    def test_metric_near_returns_none_when_far_away(self) -> None:
        text = "这是一句跟指标无关的话，里面有 5 个单位不明的东西。"
        self.assertIsNone(metric_near(text, text.index("5"), text.index("5") + 1))
    def test_conflicting_credible_sources_are_flagged_as_conflict(self) -> None:
        """来源本身就打架时，不该说这个数值"对"或"错"。"""

        facts = [
            {"value": 74, "unit": "Wh", "tier": "B", "date": "2025-04-18", "source_id": "S-008"},
            {"value": 62, "unit": "Wh", "tier": "B", "date": "2024-12-05", "source_id": "S-010"},
        ]
        kind, note = number_verdict(74, "Wh", facts)
        self.assertEqual(kind, "number-conflict")
        self.assertIn("74Wh", note)
        self.assertIn("62Wh", note)

    def test_regulation_limit_is_not_treated_as_a_measurement(self) -> None:
        facts = [
            {"value": 74, "unit": "Wh", "tier": "B", "date": "2025-04-18",
             "source_id": "S-008", "metric": "额定能量"},
            {"value": 62, "unit": "Wh", "tier": "B", "date": "2024-12-05",
             "source_id": "S-010", "metric": "额定能量"},
            {"value": 100, "unit": "Wh", "tier": "A", "date": "2025-07-08",
             "source_id": "S-007", "metric": "可随身携带额定能量上限"},
        ]
        kind, note = number_verdict(74, "Wh", facts, metric="额定能量")
        self.assertEqual(kind, "number-conflict")
        self.assertNotIn("100Wh", note)

    def test_close_values_are_not_a_conflict(self) -> None:
        kind, note = number_verdict(60, "天", FACTS)
        self.assertEqual(kind, "number-bad")
        self.assertIn("S-001", note)

    def test_real_corpus_marks_74wh_as_agreeing_with_the_newest_source(self) -> None:
        corpus = LocalCorpus.from_dir(CORPUS_DIR)
        kind, _ = number_verdict(74, "Wh", corpus.facts)
        self.assertEqual(kind, "number-conflict")


if __name__ == "__main__":
    unittest.main()

"""规则抽取器的单元测试。它不读标注，所以这些用例只能用内容本身断言。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.gap_eval import load_gap_cases  # noqa: E402
from evidence_agent.matching import match_claims  # noqa: E402
from evidence_agent.runtime import extract_claim_texts  # noqa: E402

GAP_FILE = ROOT / "data" / "gap_tasks.json"


class ExtractorTests(unittest.TestCase):
    def test_splits_clauses_and_keeps_units(self) -> None:
        content = "云石 P20 充电宝开箱：20000mAh 大容量，额定能量 74Wh，可以带上飞机。"
        self.assertEqual(
            extract_claim_texts(content),
            ["20000mAh 大容量", "额定能量 74Wh", "可以带上飞机"],
        )

    def test_drops_topic_fragment_and_model_number(self) -> None:
        """P20 里的 20 不算可核查的量，"实际参数"这类标题也不是主张。"""

        content = "云石 P20 充电宝的实际参数：容量 20000mAh。"
        self.assertEqual(extract_claim_texts(content), ["容量 20000mAh"])

    def test_subjective_clause_is_dropped_without_losing_the_rest(self) -> None:
        """整段过滤会连"还获得过认证"一起丢掉——这是这一版真实踩到的坑。"""

        content = "青禾 X1 电动牙刷使用体验：刷牙震感舒服，续航表现不错，还获得过口腔护理协会认证。"
        self.assertEqual(extract_claim_texts(content), ["还获得过口腔护理协会认证"])

    def test_assertion_without_number_is_kept(self) -> None:
        content = "云石 P20 充电宝的负面汇总：有用户反映电池过热隐患，建议先别买。"
        self.assertEqual(extract_claim_texts(content), ["有用户反映电池过热隐患"])

    def test_ratio_values_count_as_verifiable(self) -> None:
        """比例写法（1:1）是净水器废水比的常见形态，第一版把它整条丢掉了。"""

        content = "沁澜 R3 净水器：通量 1200G，废水比 1:1，滤芯寿命 3 年。"
        claims = extract_claim_texts(content)
        self.assertIn("废水比 1:1", claims)

    def test_dedupes_and_caps(self) -> None:
        content = "容量 20000mAh。容量 20000mAh。额定能量 74Wh。"
        self.assertEqual(extract_claim_texts(content), ["容量 20000mAh", "额定能量 74Wh"])
        self.assertEqual(
            len(extract_claim_texts("容量 1mAh，容量 2mAh，容量 3mAh", max_claims=2)), 2
        )

    def test_every_annotated_claim_is_matched_on_the_gap_set(self) -> None:
        """评测集上规则抽取的召回率是 1.0（数字锁在 gap_eval 的端到端用例里）。"""

        _, cases = load_gap_cases(GAP_FILE)
        for case in cases:
            _pairs, unmatched_expected, _unmatched_predicted = match_claims(
                [claim.text for claim in case.expected_claims],
                extract_claim_texts(case.content),
                threshold=0.3,
            )
            self.assertEqual(unmatched_expected, [], case.id)


if __name__ == "__main__":
    unittest.main()

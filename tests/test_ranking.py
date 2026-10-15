"""来源权威性排序与展示权重分离的回归测试（2026-10-15 重构）。

这一批测试锁的是"三层职责分开"这件事本身：

- 权威性排序用 `rank_key`（有序元组），**不经过任何分数**；
- 展示权重只用于展示，不再由它决定谁更权威；
- R1–R13 的处置结论不因展示权重改档。

与 `tests/test_grading.py` 的分工：那边测单条来源的三层取值，这里测
**用到排序的三处调用点**（`assessor.best()`、`compare_numeric()`、
`highlight.authoritative_value()`）以及它们与处置规则的联动。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.grading import (  # noqa: E402
    WEIGHT_STRONG,
    grade_of,
    grade_source,
    rank_key_for_row,
    weight_for_row,
)
from evidence_agent.highlight import authoritative_value  # noqa: E402
from evidence_agent.runtime import (  # noqa: E402
    DOWNRANK,
    MANUAL,
    RELEASE,
    Assessment,
    assess,
    classify,
    compare_numeric,
)
from evidence_agent.tools import Evidence  # noqa: E402


def item(
    source_id: str,
    *,
    kind: str,
    sampling: str = "",
    accredited: bool = False,
    tier: str = "B",
    date: str = "2025-01-01",
    value: str | None = None,
    unit: str = "",
    metric: str = "",
    entity: str = "",
    publisher: str = "",
) -> Evidence:
    meta = {
        "kind": kind,
        "identity": "",
        "sampling": sampling,
        "accredited": accredited,
        "tier": tier,
        "publisher": publisher or f"{source_id} 发布方",
        "date": date,
    }
    if value is not None:
        meta.update({"value": value, "unit": unit, "metric": metric, "entity": entity})
    return Evidence(
        source_id=source_id,
        tier=tier,
        date=date,
        text=f"{entity} {metric}：{value}{unit}",
        meta=meta,
    )


class RankKeyIsUsedNotWeightTests(unittest.TestCase):
    """权威性排序必须由 rank_key 保证，不能依赖展示分数。"""

    def test_regulator_wins_even_when_the_other_side_has_every_bonus(self) -> None:
        """监管来源 vs 取样最好＋有资质的独立第三方：身份优先，不能被加分翻转。"""

        regulator = grade_source(kind="监管公开库")
        best_third_party = grade_source(
            kind="独立检测", sampling="市场随机购买", accredited=True
        )
        self.assertLess(regulator.rank_key, best_third_party.rank_key)
        # 两者展示权重可能相同（都过门槛），但这不参与比较——排序键已经分出高下
        self.assertEqual(regulator.weight, WEIGHT_STRONG)
        self.assertEqual(best_third_party.weight, WEIGHT_STRONG)

    def test_best_uses_rank_key_not_weight(self) -> None:
        """身份不同时，assessor.best() 必须选身份更高的那条，哪怕它更旧。"""

        regulator = item("S-007", kind="监管公开库", date="2020-01-01")
        vendor = item("S-008", kind="品牌官方声明", date="2026-12-31")
        assessment = assess([vendor, regulator])
        self.assertIsNotNone(assessment.best())
        self.assertEqual(assessment.best().source_id, "S-007")
        # 展示权重确实不同档，但排序不经过它：这条测试的含义是"身份优先于时效"
        self.assertNotEqual(grade_of(regulator).weight, grade_of(vendor).weight)

    def test_best_falls_back_to_evidence_date_when_meta_has_none(self) -> None:
        """`grade_of` 必须读证据对象自己的 date——否则"取更新的那条"会失效。

        这是本次重构真实踩到的缺陷：`rank_key` 末段用日期，而 `grade_of` 只从
        meta 取日期，`Evidence.date` 被忽略，于是同身份的证据全部同键，`min()`
        退化成"取第一条"。
        """

        old = item("S-001", kind="品牌官方声明", date="2023-01-01")
        new = item("S-013", kind="品牌官方声明", date="2025-09-10")
        for candidate in (old, new):
            del candidate.meta["date"]
        self.assertLess(grade_of(new).rank_key, grade_of(old).rank_key)
        self.assertEqual(assess([old, new]).best().source_id, "S-013")
        self.assertEqual(assess([new, old]).best().source_id, "S-013")

    def test_best_prefers_newer_date_within_same_identity(self) -> None:
        old = item("S-001", kind="品牌官方声明", date="2023-01-01")
        new = item("S-013", kind="品牌官方声明", date="2025-09-10")
        # 顺序无关：谁更新听谁的
        self.assertEqual(assess([old, new]).best().source_id, "S-013")
        self.assertEqual(assess([new, old]).best().source_id, "S-013")

    def test_best_treats_missing_date_as_least_recent(self) -> None:
        dated = item("S-001", kind="品牌官方声明", date="2023-01-01")
        undated = item("S-002", kind="品牌官方声明", date="")
        self.assertEqual(assess([dated, undated]).best().source_id, "S-001")

    def test_compare_numeric_picks_by_rank_key_across_identities(self) -> None:
        """数值比对的两条候选来自不同身份时，用身份决定听谁的。"""

        claim = "云石 P20 容量 20000mAh"
        regulator = item(
            "S-007",
            kind="监管公开库",
            tier="A",
            value="20000",
            unit="mAh",
            metric="容量",
            entity="云石 P20",
        )
        vendor = item(
            "S-008",
            kind="品牌官方声明",
            value="20000",
            unit="mAh",
            metric="容量",
            entity="云石 P20",
        )
        result = compare_numeric(claim, [vendor, regulator])
        self.assertIsNotNone(result)
        self.assertEqual(result[2], ("S-007",))

    def test_authoritative_value_uses_the_same_rank_rule(self) -> None:
        """高亮挑"最权威取值"的口径必须与判定一致，否则界面与结论会互相打脸。"""

        rows = [
            {
                "source_id": "S-008",
                "kind": "品牌官方声明",
                "tier": "B",
                "value": "74",
                "unit": "Wh",
                "metric": "额定能量",
                "date": "2025-09-10",
            },
            {
                "source_id": "S-010",
                "kind": "独立检测",
                "sampling": "市场随机购买",
                "accredited": True,
                "tier": "B",
                "value": "62",
                "unit": "Wh",
                "metric": "额定能量",
                "date": "2025-06-20",
            },
        ]
        found = authoritative_value(rows, "Wh")
        self.assertIsNotNone(found)
        self.assertEqual(found[1]["source_id"], "S-010")
        self.assertEqual(found[0], 62.0)

    def test_authoritative_value_ignores_accreditation_bonus_for_ranking(self) -> None:
        """资质只影响门槛：两条都够格时，资质不再额外抬高排序。"""

        plain = {
            "source_id": "S-001",
            "kind": "独立检测",
            "sampling": "市场随机购买",
            "accredited": False,
            "tier": "B",
            "value": "10",
            "unit": "Wh",
            "date": "2025-01-01",
        }
        credited = dict(plain, source_id="S-002", accredited=True)
        # 同身份同取样，资质在排序里只占第三位；两条除资质外完全相同，
        # 所以有资质的那条胜出——这是排序规则的一部分，不是权重加分
        found = authoritative_value([plain, credited], "Wh")
        self.assertEqual(found[1]["source_id"], "S-002")

    def test_weight_for_row_is_display_only(self) -> None:
        """展示权重是三档值；权威性排序键是元组，两者类型与语义都不同。"""

        row = {"kind": "独立检测", "sampling": "厂商送检", "tier": "B", "date": ""}
        self.assertEqual(weight_for_row(row), 0.40)
        self.assertIsInstance(rank_key_for_row(row), tuple)
        # 同一份资料在两条路径上得到一致的排序键
        self.assertEqual(rank_key_for_row(row), grade_of(Evidence(
            source_id="S-001", tier="B", date="", text="", meta=row
        )).rank_key)


class DispositionUnaffectedTests(unittest.TestCase):
    """展示权重改动不得改变 R1–R13 的结果。"""

    def test_r13_still_gates_outward_actions_from_vendor_only_evidence(self) -> None:
        from evidence_agent.runtime.disposition import vendor_only_evidence

        vendor_assessment = Assessment(
            sufficient=True,
            items=[item("S-008", kind="品牌官方声明"), item("S-009", kind="品牌官方声明")],
            decisive_sources=("S-008", "S-009"),
        )
        self.assertTrue(vendor_only_evidence(vendor_assessment))
        disposition = classify(
            state="JUDGED",
            verdict="CONTRADICTED",
            claim_type="numeric",
            claim_text="云石 P20 额定能量 74Wh",
            assessment=vendor_assessment,
            content="带货文案",
        )
        self.assertEqual(disposition.rule, "R13")
        self.assertEqual(disposition.tier, MANUAL)

    def test_r13_does_not_fire_when_an_independent_source_backs_the_conclusion(self) -> None:
        from evidence_agent.runtime.disposition import vendor_only_evidence

        mixed = Assessment(
            sufficient=True,
            items=[item("S-008", kind="品牌官方声明"), item("S-010", kind="独立检测")],
            decisive_sources=("S-010",),
        )
        self.assertFalse(vendor_only_evidence(mixed))

    def test_supported_claim_is_released_under_both_weight_schemes(self) -> None:
        """R4 与权重无关：支持就放行，不受展示权重分档影响。"""

        assessment = Assessment(
            sufficient=True,
            items=[item("S-001", kind="品牌官方声明")],
            decisive_sources=("S-001",),
        )
        disposition = classify(
            state="JUDGED",
            verdict="SUPPORTED",
            claim_type="numeric",
            claim_text="云石 P20 容量 20000mAh",
            assessment=assessment,
        )
        self.assertEqual(disposition.rule, "R4")
        self.assertEqual(disposition.tier, RELEASE)

    def test_evidence_strength_still_counts_independent_publishers(self) -> None:
        from evidence_agent.runtime.disposition import evidence_strength

        one = Assessment(
            sufficient=True,
            items=[item("S-010", kind="独立检测", publisher="同一家机构")],
        )
        two = Assessment(
            sufficient=True,
            items=[
                item("S-010", kind="独立检测", publisher="甲机构"),
                item("S-014", kind="独立检测", publisher="乙机构"),
            ],
        )
        self.assertEqual(evidence_strength("JUDGED", one), "E2")
        self.assertEqual(evidence_strength("JUDGED", two), "E3")

    def test_downrank_still_requires_strong_evidence(self) -> None:
        """R8/R9 的差别是证据强度，与展示权重分档无关。"""

        strong = Assessment(
            sufficient=True,
            items=[
                item("S-010", kind="独立检测", publisher="甲机构"),
                item("S-014", kind="独立检测", publisher="乙机构"),
            ],
            decisive_sources=("S-010",),
        )
        single = Assessment(
            sufficient=True,
            items=[item("S-010", kind="独立检测", publisher="甲机构")],
            decisive_sources=("S-010",),
        )
        common = dict(
            state="JUDGED",
            verdict="CONTRADICTED",
            claim_type="numeric",
            claim_text="青禾 X1 续航 60 天",
            content="",
        )
        self.assertEqual(classify(assessment=strong, **common).tier, DOWNRANK)
        self.assertEqual(classify(assessment=single, **common).tier, MANUAL)


class CorpusConsistencyTests(unittest.TestCase):
    """真实语料上的排序必须稳定、可解释。"""

    def setUp(self) -> None:
        from evidence_agent.tools import LocalCorpus

        self.corpus = LocalCorpus.from_dir(ROOT / "data" / "corpus")

    def test_every_corpus_row_has_a_rank_key_and_three_bucket_weight(self) -> None:
        for row in self.corpus.enriched_facts():
            key = rank_key_for_row(row)
            self.assertEqual(len(key), 4, row)
            self.assertIn(weight_for_row(row), (0.90, 0.70, 0.40, 0.95, 0.85, 0.25, 0.35))
            self.assertLessEqual(key[0], 5)

    def test_regulator_rows_outrank_vendor_rows_on_the_same_metric(self) -> None:
        facts = self.corpus.enriched_facts()
        regulator = [
            row for row in facts if rank_key_for_row(row)[0] == 0
        ]
        vendor = [row for row in facts if rank_key_for_row(row)[0] == 3]
        if not regulator or not vendor:
            self.skipTest("语料里缺少监管或厂商记录，无法比较")
        self.assertLess(
            min(rank_key_for_row(row) for row in regulator),
            min(rank_key_for_row(row) for row in vendor),
        )

    def test_ranking_is_deterministic(self) -> None:
        facts = list(self.corpus.enriched_facts())
        first = [rank_key_for_row(row) for row in facts]
        second = [rank_key_for_row(row) for row in facts]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

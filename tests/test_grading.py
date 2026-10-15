"""Unit tests for multi-dimensional source grading.

三层职责分开测，一条断言只动一个变量——这样失败时能直接看出是哪条规则坏了：

1. 资格门槛：`strong`；
2. 权威性排序：`rank_key`；
3. 展示权重：`weight`。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.grading import (  # noqa: E402
    STRONG_THRESHOLD,
    WEIGHT_INELIGIBLE,
    WEIGHT_STRONG,
    WEIGHT_VENDOR,
    grade_of,
    grade_source,
    identity_of,
    independent_sources,
    rank_key,
    rank_key_for_row,
    weight_for_row,
)
from evidence_agent.tools import Evidence  # noqa: E402


def ev(source_id: str, tier: str, meta: dict | None = None) -> Evidence:
    return Evidence(
        source_id=source_id, tier=tier, date="2025-01-01", text="x", meta=meta or {}
    )


class IdentityTests(unittest.TestCase):
    def test_kind_maps_to_identity(self) -> None:
        self.assertEqual(identity_of("监管公开库"), "监管机构")
        self.assertEqual(identity_of("独立检测"), "独立第三方")
        self.assertEqual(identity_of("品牌官方声明"), "厂商自述")
        self.assertEqual(identity_of("站内营销内容"), "用户自述")

    def test_unknown_kind_falls_back(self) -> None:
        self.assertEqual(identity_of("没见过的类型"), "未知")

    def test_explicit_identity_wins(self) -> None:
        self.assertEqual(identity_of("独立检测", explicit="监管机构"), "监管机构")


class GradeTests(unittest.TestCase):
    """展示权重：只由门槛结论与身份推三档。"""

    def test_regulator_outranks_vendor_by_rank_not_by_weight_value(self) -> None:
        """权威性由排序键保证，而不是"监管恰好拿到 1.00 分"。"""

        regulator = grade_source(kind="监管公开库")
        vendor = grade_source(kind="品牌官方声明")
        self.assertLess(regulator.rank_key, vendor.rank_key)

    def test_weight_is_one_of_three_buckets(self) -> None:
        self.assertEqual(grade_source(kind="监管公开库").weight, WEIGHT_STRONG)
        self.assertEqual(grade_source(kind="独立检测").weight, WEIGHT_STRONG)
        self.assertEqual(grade_source(kind="品牌官方声明").weight, WEIGHT_VENDOR)
        self.assertEqual(grade_source(kind="站内用户内容").weight, WEIGHT_INELIGIBLE)

    def test_vendor_spec_is_still_strong(self) -> None:
        """"厂商标称"是核查"是否夸大宣传"的基准，不能当弱来源。"""

        grade = grade_source(kind="品牌官方声明")
        self.assertTrue(grade.strong)

    def test_user_content_is_weak(self) -> None:
        self.assertFalse(grade_source(kind="站内用户内容").strong)
        self.assertFalse(grade_source(kind="站内营销内容").strong)

    def test_accreditation_does_not_add_weight(self) -> None:
        """资质只影响门槛，不产生额外权重加分（这是 2026-10-15 修掉的问题）。"""

        without = grade_source(kind="独立检测", accredited=False)
        with_credit = grade_source(kind="独立检测", accredited=True)
        self.assertEqual(without.weight, with_credit.weight)
        self.assertTrue(without.strong)
        self.assertTrue(with_credit.strong)

    def test_market_sampling_does_not_add_weight(self) -> None:
        """取样方式只影响门槛与排序，不再额外加 0.10。"""

        plain = grade_source(kind="独立检测", sampling="取样方式未说明")
        market = grade_source(kind="独立检测", sampling="市场随机购买")
        self.assertEqual(plain.weight, market.weight)

    def test_vendor_submitted_sample_fails_the_gate_not_the_weight(self) -> None:
        """厂商送检不通过门槛，而不是靠负权重被扣下去。"""

        submitted = grade_source(kind="独立检测", sampling="厂商送检")
        self.assertFalse(submitted.strong)
        self.assertEqual(submitted.weight, WEIGHT_INELIGIBLE)
        self.assertLess(submitted.weight, STRONG_THRESHOLD)

    def test_vendor_spec_plus_vendor_submitted_becomes_weak(self) -> None:
        grade = grade_source(kind="品牌官方声明", sampling="厂商送检")
        self.assertFalse(grade.strong)

    def test_weight_is_clamped(self) -> None:
        grade = grade_source(kind="监管公开库", sampling="市场随机购买", accredited=True)
        self.assertLessEqual(grade.weight, 1.0)
        self.assertGreaterEqual(grade.weight, 0.0)

    def test_threshold_boundary(self) -> None:
        self.assertLess(STRONG_THRESHOLD, 1.0)
        self.assertGreater(STRONG_THRESHOLD, 0.5)

    def test_reasons_do_not_show_additive_arithmetic(self) -> None:
        """报告里不能再出现"0.85 + 0.10"这类看着像在算资格的算式。"""

        grade = grade_source(
            kind="独立检测", sampling="市场随机购买", accredited=True, date="2025-01-01"
        )
        joined = "；".join(grade.reasons)
        self.assertNotIn("+", joined)
        self.assertNotIn("-0.", joined)
        self.assertIn("不参与资格判定", joined)


class RankKeyTests(unittest.TestCase):
    """权威性排序：有序元组，不经过任何分数。"""

    def test_identity_order_is_documented_order(self) -> None:
        self.assertLess(rank_key(kind="监管公开库"), rank_key(kind="独立检测"))
        self.assertLess(rank_key(kind="独立检测"), rank_key(kind="平台规则"))
        self.assertLess(rank_key(kind="平台规则"), rank_key(kind="品牌官方声明"))
        self.assertLess(rank_key(kind="品牌官方声明"), rank_key(kind="站内用户内容"))

    def test_platform_outranks_vendor_regardless_of_date(self) -> None:
        """平台规则比厂商自述权威，不能因为厂商文件更新就翻转。"""

        platform = rank_key(kind="平台规则", date="2020-01-01")
        vendor = rank_key(kind="品牌官方声明", date="2026-12-31")
        self.assertLess(platform, vendor)

    def test_no_cross_identity_flip_from_dimension_bonuses(self) -> None:
        """低身份无论取样多好、资质多全，都不会超过高身份——跨维度累加已经不参与比较。"""

        best_low = rank_key(
            kind="品牌官方声明",
            sampling="市场随机购买",
            accredited=True,
            date="2026-12-31",
        )
        worst_high = rank_key(kind="监管公开库", date="")

        self.assertLess(worst_high, best_low)

    def test_same_identity_compares_sampling_then_accreditation(self) -> None:
        market = rank_key(kind="独立检测", sampling="市场随机购买", date="2025-01-01")
        unknown_sampling = rank_key(kind="独立检测", sampling="", date="2025-01-01")
        submitted = rank_key(kind="独立检测", sampling="厂商送检", date="2025-01-01")
        self.assertLess(market, unknown_sampling)
        self.assertLess(unknown_sampling, submitted)

        accredited = rank_key(kind="独立检测", accredited=True, date="2025-01-01")
        plain = rank_key(kind="独立检测", accredited=False, date="2025-01-01")
        self.assertLess(accredited, plain)

    def test_date_breaks_ties_and_missing_date_loses(self) -> None:
        newer = rank_key(kind="独立检测", date="2025-06-01")
        older = rank_key(kind="独立检测", date="2024-06-01")
        missing = rank_key(kind="独立检测", date="")
        # 日期段折算成"越新越小"，与前三段方向一致（越小越权威）
        self.assertLess(newer, older)
        self.assertLess(older, missing)

    def test_fresher_date_wins_in_every_comparison_direction(self) -> None:
        """方向一致性：同一身份的证据，谁新谁权威，与传入顺序无关。"""

        old = grade_source(kind="独立检测", date="2023-01-01")
        new = grade_source(kind="独立检测", date="2025-09-10")
        self.assertLess(new.rank_key, old.rank_key)
        self.assertLess(new.rank_key[-1], old.rank_key[-1])

    def test_unparseable_date_is_treated_as_missing(self) -> None:
        garbage = rank_key(kind="独立检测", date="去年")
        missing = rank_key(kind="独立检测", date="")
        self.assertEqual(garbage, missing)

    def test_rank_tuple_is_json_safe(self) -> None:
        """排序键会进 to_dict()／接口，不能出现 Infinity。"""

        import json as _json

        payload = grade_source(kind="独立检测", date="").to_dict()
        text = _json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("Infinity", text)
        self.assertNotIn("NaN", text)
        _json.loads(text)

    def test_missing_fields_have_stable_positions(self) -> None:
        """缺字段不能抛异常，也不能排到随机位置。"""

        unknown_identity = rank_key()
        user = rank_key(kind="站内用户内容")
        # 用户自述可核验性最差（身份等级 5），落在"未知"（4）之后
        self.assertLess(unknown_identity, user)
        self.assertEqual(rank_key(), rank_key(tier=""))  # 同输入同输出

    def test_legacy_tier_rows_remain_comparable(self) -> None:
        """旧数据（只有 A–D／E1–E4）要能继续与带维度的证据比，而不是全排到最后。"""

        tier_a = rank_key(tier="A")
        tier_b = rank_key(tier="B")
        tier_d = rank_key(tier="D")
        self.assertLess(tier_a, tier_b)
        self.assertLess(tier_b, tier_d)
        self.assertLess(tier_a, rank_key(kind="独立检测"))
        # 旧等级 D 折算成身份等级 3（厂商自述一档），比"用户自述"（5）高，
        # 与旧权重口径一致（D 级 0.25 高于用户内容 0.30 的强度）
        self.assertLess(tier_d, rank_key(kind="站内用户内容"))

    def test_rank_key_for_row_matches_grade_of(self) -> None:
        row = {
            "kind": "独立检测",
            "sampling": "市场随机购买",
            "accredited": True,
            "tier": "B",
            "date": "2025-09-10",
        }
        self.assertEqual(rank_key_for_row(row), grade_source(**row).rank_key)

    def test_rank_key_for_row_falls_back_for_rows_without_dimensions(self) -> None:
        self.assertEqual(rank_key_for_row({"tier": "A"}), rank_key(tier="A"))
        self.assertEqual(rank_key_for_row({}), rank_key())

    def test_rank_is_stable_under_repeated_calls(self) -> None:
        first = rank_key(kind="独立检测", sampling="市场随机购买", date="2025-01-01")
        second = rank_key(kind="独立检测", sampling="市场随机购买", date="2025-01-01")
        self.assertEqual(first, second)


class GradeOfEvidenceTests(unittest.TestCase):
    def test_reads_dimensions_from_meta(self) -> None:
        item = ev("S-010", "B", {"kind": "独立检测", "sampling": "市场随机购买", "accredited": True})
        grade = grade_of(item)
        self.assertEqual(grade.identity, "独立第三方")
        self.assertTrue(grade.strong)

    def test_falls_back_to_tier_when_dimensions_are_missing(self) -> None:
        self.assertTrue(grade_of(ev("S-001", "A")).strong)
        self.assertFalse(grade_of(ev("S-004", "D")).strong)
        self.assertIn("折算", grade_of(ev("S-001", "A")).reasons[0])


class WeightForRowTests(unittest.TestCase):
    def test_row_with_kind_uses_identity_path(self) -> None:
        row = {"kind": "监管公开库", "tier": "A"}
        self.assertEqual(weight_for_row(row), grade_source(kind="监管公开库").weight)

    def test_row_without_kind_uses_tier_fallback(self) -> None:
        self.assertEqual(weight_for_row({"tier": "A"}), 0.95)
        self.assertEqual(weight_for_row({"tier": "D"}), 0.25)
        self.assertEqual(weight_for_row({}), 0.35)

    def test_eval_task_tiers_are_mapped(self) -> None:
        """评测任务集用 E1–E4（语义见 docs/02-labeling-rules.md），不能落到"未知"。"""

        self.assertEqual(weight_for_row({"tier": "E1"}), 0.95)
        self.assertEqual(weight_for_row({"tier": "E2"}), 0.85)
        self.assertEqual(weight_for_row({"tier": "E3"}), 0.40)
        self.assertEqual(weight_for_row({"tier": "E4"}), 0.25)
        self.assertTrue(grade_of(ev("S1", "E1")).strong)
        self.assertFalse(grade_of(ev("S4", "E4")).strong)

    def test_every_tier_in_the_eval_set_has_a_weight(self) -> None:
        """任务集里出现的每个等级都必须有折算权重：缺项不报错，只会静默降级。"""

        payload = json.loads(
            (ROOT / "data" / "eval_tasks.json").read_text(encoding="utf-8")
        )
        tiers = {
            source["tier"] for case in payload["cases"] for source in case["sources"]
        }
        self.assertTrue(tiers)
        for tier in tiers:
            with self.subTest(tier=tier):
                self.assertNotEqual(weight_for_row({"tier": tier}), 0.35)


class IndependenceTests(unittest.TestCase):
    def test_same_publisher_counts_once(self) -> None:
        """同一家机构的两次检测只是一个来源。"""

        items = [
            ev("S-002", "B", {"kind": "独立检测", "publisher": "第三方检测机构"}),
            ev("S-003", "B", {"kind": "独立检测", "publisher": "第三方检测机构"}),
        ]
        self.assertEqual(len(independent_sources(items)), 1)

    def test_different_publishers_count_separately(self) -> None:
        items = [
            ev("S-001", "A", {"kind": "品牌官方声明", "publisher": "青禾官网"}),
            ev("S-002", "B", {"kind": "独立检测", "publisher": "第三方检测机构"}),
        ]
        self.assertEqual(len(independent_sources(items)), 2)

    def test_weak_sources_do_not_count(self) -> None:
        items = [
            ev("S-003", "C", {"kind": "站内用户内容", "publisher": "社区用户"}),
            ev("S-004", "D", {"kind": "站内营销内容", "publisher": "推广账号"}),
        ]
        self.assertEqual(independent_sources(items), set())

    def test_missing_publisher_is_not_counted_as_independent(self) -> None:
        """发布者不明时不能声称"独立"：退回资料编号会虚增来源数，抬高 E 档。"""

        items = [ev("S-002", "B", {"kind": "独立检测"})]
        self.assertEqual(independent_sources(items), set())


if __name__ == "__main__":
    unittest.main()

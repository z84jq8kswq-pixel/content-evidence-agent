"""Unit tests for the runtime: planner, assessor and the agent state machine."""

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

from evidence_agent.models import ModelResponse  # noqa: E402
from evidence_agent.runtime import (  # noqa: E402
    ESCALATED,
    JUDGED,
    TOOL_FAILED,
    EvidenceAgent,
    Planner,
    assess,
    claim_numbers,
    compare_permission,
    compare_numeric,
    infer_claim_type,
    infer_metric,
    make_agent,
)
from evidence_agent.tools import (  # noqa: E402
    STATUS_EMPTY,
    STATUS_UPSTREAM_ERROR,
    Evidence,
    LocalCorpus,
    Outcome,
    ParamSpec,
    Tool,
    ToolRegistry,
    ToolSpec,
)
from evidence_agent.trace import TraceWriter  # noqa: E402

CORPUS_DIR = ROOT / "data" / "corpus"


def ev(
    source_id: str,
    tier: str,
    date: str,
    value=None,
    unit: str = "",
    metric: str | None = None,
    entity: str = "青禾 X1",
    text: str = "证据片段",
    score: float = 1.0,
    **extra,
) -> Evidence:
    meta = None
    if metric is not None:
        meta = {
            "entity": entity,
            "metric": metric,
            "value": value,
            "unit": unit,
            "date": date,
            "tier": tier,
            "source_id": source_id,
            **extra,
        }
    return Evidence(
        source_id=source_id, tier=tier, date=date, text=text, score=score, meta=meta
    )


class AssessorTests(unittest.TestCase):
    def test_no_evidence_is_not_sufficient(self) -> None:
        result = assess([])
        self.assertFalse(result.sufficient)
        self.assertEqual(result.reason, "没有任何证据")
        self.assertIsNone(result.best())

    def test_low_tier_only_is_not_sufficient(self) -> None:
        result = assess([ev("S-003", "C", "2024-11-02", metric="续航", value=45, unit="天")])
        self.assertFalse(result.sufficient)
        self.assertIn("LOW_SOURCE_QUALITY", result.flags)
        self.assertFalse(result.blocking)

    def test_single_tier_a_source_is_sufficient(self) -> None:
        result = assess([ev("S-001", "A", "2025-03-12", metric="续航", value=21, unit="天")])
        self.assertTrue(result.sufficient)
        self.assertEqual(result.best().source_id, "S-001")

    def test_close_values_are_not_a_conflict(self) -> None:
        result = assess(
            [
                ev("S-001", "A", "2025-03-12", metric="续航", value=21, unit="天"),
                ev("S-002", "B", "2025-05-20", metric="续航", value=19.5, unit="天"),
            ]
        )
        self.assertTrue(result.sufficient)
        self.assertEqual(result.flags, [])

    def test_conflicting_values_block(self) -> None:
        result = assess(
            [
                ev("S-008", "B", "2025-04-18", metric="额定能量", value=74, unit="Wh"),
                ev("S-010", "B", "2024-12-05", metric="额定能量", value=62, unit="Wh"),
            ]
        )
        self.assertFalse(result.sufficient)
        self.assertEqual(result.flags, ["SOURCE_CONFLICT"])
        self.assertTrue(result.blocking)
        self.assertIn("冲突", result.reason)

    def test_newer_record_supersedes_older(self) -> None:
        result = assess(
            [
                ev("S-006", "B", "2023-09-10", metric="认证", value="在列"),
                ev("S-005", "A", "2025-06-01", metric="认证", value="未列入 2025 年度名录"),
            ]
        )
        self.assertFalse(result.sufficient)
        self.assertEqual(result.flags, ["STALE"])
        self.assertTrue(result.blocking)
        self.assertIn("过期", result.reason)

    def test_low_tier_disagreement_is_not_a_conflict(self) -> None:
        """夸大文案之间互相矛盾不算来源冲突，否则任何噪音都会阻塞结论。"""

        result = assess(
            [
                ev("S-003", "C", "2024-11-02", metric="续航", value=45, unit="天"),
                ev("S-004", "D", "2024-06-15", metric="续航", value=60, unit="天"),
            ]
        )
        self.assertEqual(result.flags, ["LOW_SOURCE_QUALITY"])
        self.assertFalse(result.blocking)

    def test_different_conditions_are_not_comparable(self) -> None:
        """厂商标称与负载下实测不是同一件事：不可比，不是矛盾。"""

        result = assess(
            [
                ev("S-008", "B", "2025-04-18", metric="额定能量", value=74, unit="Wh",
                   conditions="厂商标称"),
                ev("S-010", "B", "2024-12-05", metric="额定能量", value=62, unit="Wh",
                   conditions="5V/2A 输出条件下实测"),
            ]
        )
        self.assertIn("NOT_COMPARABLE", result.flags)
        self.assertNotIn("SOURCE_CONFLICT", result.flags)
        self.assertTrue(result.blocking)

    def test_same_conditions_can_still_conflict(self) -> None:
        result = assess(
            [
                ev("S-001", "A", "2025-03-12", metric="容量", value=20000, unit="mAh",
                   conditions="标准放电"),
                ev("S-002", "B", "2025-05-20", metric="容量", value=17000, unit="mAh",
                   conditions="标准放电"),
            ]
        )
        self.assertIn("SOURCE_CONFLICT", result.flags)

    def test_tolerance_depends_on_metric(self) -> None:
        """续航是测量类，容差宽；容量是标称类，容差窄。"""

        def pair(metric: str, low: float, high: float, unit: str):
            return [
                ev("S-001", "A", "2025-03-12", metric=metric, value=high, unit=unit),
                ev("S-002", "B", "2025-05-20", metric=metric, value=low, unit=unit),
            ]

        loose = assess(pair("续航", 18, 21, "天"))  # 差 14%，在 20% 容差内
        tight = assess(pair("容量", 18500, 20000, "mAh"))  # 差 7.5%，超出 5% 容差
        self.assertNotIn("SOURCE_CONFLICT", loose.flags)
        self.assertIn("SOURCE_CONFLICT", tight.flags)

    def test_free_text_claims_are_not_numerically_comparable(self) -> None:
        self.assertIsNone(
            compare_numeric("青禾 X1 防水等级达到 IPX7", [ev("S-001", "A", "2025-03-12")])
        )

    def test_retrieved_evidence_exposes_structured_conflicts(self) -> None:
        """检索路径拿到的资料，其结构化事实必须参与冲突判定。

        修之前：search_notes 返回的证据没有实体与指标，冲突判定整组看不见，
        74Wh 与 62Wh 会被判成"证据充分"。
        """

        corpus = LocalCorpus.from_dir(CORPUS_DIR)
        items = corpus.search("云石 P20 额定能量", top_k=3)
        self.assertTrue(any(item.meta.get("facts") for item in items))

        with_metric = assess(
            items, claim_metric="额定能量", claim_entity="云石 P20"
        )
        self.assertFalse(with_metric.sufficient)
        self.assertIn("NOT_COMPARABLE", with_metric.flags)
        self.assertTrue(with_metric.blocking)

        # 指标未知时不去猜：否则同一份资料里与主张无关的指标会把结论拖进队列
        without_metric = assess(items)
        self.assertTrue(without_metric.sufficient)
        self.assertEqual(without_metric.flags, [])

    def test_same_unit_other_metric_is_not_used_as_the_target(self) -> None:
        """同单位不等于同指标：续航 40 小时和降噪续航 28 小时都是小时。

        拿错那条比会得出相反的结论（28 会被判成与 40 相差 30%）。
        """

        items = [
            ev("S-013", "B", "2025-05-10", metric="续航", value=40, unit="小时"),
            ev(
                "S-013",
                "B",
                "2025-05-10",
                metric="降噪续航",
                value=28,
                unit="小时",
            ),
        ]
        outcome = compare_numeric("开启降噪 28 小时", items)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome[0], "SUPPORTED")
        self.assertEqual(outcome[2], ("S-013",))

    def test_ratio_metric_is_comparable(self) -> None:
        """比例型指标（废水比 1:1）要能比对。

        以前只认 数字+单位 的写法，这类主张永远走不到规则判定，只能转人工。
        """

        items = [
            ev(
                "S-015",
                "B",
                "2025-06-15",
                metric="废水比",
                value=1,
                unit=":1",
            )
        ]
        outcome = compare_numeric("废水比 1:1", items)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome[0], "SUPPORTED")

        worse = compare_numeric("废水比 4:1", items)
        self.assertIsNotNone(worse)
        self.assertEqual(worse[0], "CONTRADICTED")


    def test_retrieved_facts_from_another_entity_are_ignored(self) -> None:
        """检索顺手带回来的结构化事实，实体对不上就不能用。

        扩样时真实撞到过：加了净水器语料后，莱茵 H9（语料里没有这个产品）的主张
        检索到了沁澜 R3 的滤芯寿命记录，被判成来源过期。
        """

        corpus = LocalCorpus.from_dir(CORPUS_DIR)
        items = corpus.search("滤芯寿命 3 年", top_k=5)
        self.assertTrue(any(item.meta.get("facts") for item in items))

        # 主体认不出来（莱茵 H9 不在语料里）→ 检索带回的事实不参与冲突／过期判定
        unknown_entity = assess(items, claim_metric="滤芯寿命")
        self.assertEqual(unknown_entity.flags, [])

        # 主体认得出且对得上 → 才参与（沁澜 R3 的两条记录构成过期）
        matched = assess(items, claim_metric="滤芯寿命", claim_entity="沁澜 R3")
        self.assertIn("STALE", matched.flags)

    def test_unknown_entity_falls_back_to_retrieval_but_keeps_the_reason(self) -> None:
        """认不出实体时改走检索，冲突仍要看得见，理由不能说成"证据充分"。"""

        # 正文里出现主体，实体才认得出来——检索路径能判冲突／不可比的前提
        agent = make_agent(LocalCorpus.from_dir(CORPUS_DIR))
        claim = agent.run_claim(
            "云石 P20 充电宝的开箱记录", "这款移动电源额定能量 62Wh"
        )
        self.assertEqual(claim.state, ESCALATED)
        self.assertIn("NOT_COMPARABLE", claim.flags)
        self.assertIn("测试条件不同", claim.reason)

    def test_numeric_claim_supported(self) -> None:
        outcome = compare_numeric(
            "云石 P20 容量 20000mAh",
            [ev("S-008", "B", "2025-04-18", metric="容量", value=20000, unit="mAh")],
        )
        self.assertEqual(outcome[0], "SUPPORTED")

    def test_numeric_claim_contradicted(self) -> None:
        outcome = compare_numeric(
            "青禾 X1 续航 60 天",
            [
                ev("S-001", "A", "2025-03-12", metric="续航", value=21, unit="天"),
                ev("S-004", "D", "2024-06-15", metric="续航", value=60, unit="天"),
            ],
        )
        self.assertEqual(outcome[0], "CONTRADICTED")
        self.assertIn("S-001", outcome[1])


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = LocalCorpus.from_dir(CORPUS_DIR)
        self.planner = Planner(resolve_entity=self.corpus.resolve_entity)

    def test_infer_claim_type(self) -> None:
        self.assertEqual(infer_claim_type("青禾 X1 续航 60 天"), "numeric")
        self.assertEqual(infer_claim_type("该产品获得口腔护理协会认证"), "certification")
        self.assertEqual(infer_claim_type("全系唯一支持无线充电"), "exclusive")
        self.assertEqual(infer_claim_type("这是一款不错的产品"), "other")

    def test_infer_metric(self) -> None:
        self.assertEqual(infer_metric("青禾 X1 续航 60 天"), "续航")
        self.assertEqual(infer_metric("额定能量 74Wh"), "额定能量")
        self.assertIsNone(infer_metric("这是一款不错的产品"))

    def test_claim_numbers(self) -> None:
        self.assertEqual(claim_numbers("续航 60 天"), [(60.0, "天")])
        self.assertEqual(claim_numbers("容量 20000mAh"), [(20000.0, "mAh")])

    def test_numeric_claim_falls_back_to_search(self) -> None:
        plans = self.planner.strategies("青禾 X1 续航 60 天", "numeric")
        self.assertEqual([plan.tool for plan in plans], ["lookup_fact", "search_notes"])
        self.assertEqual(plans[0].params["entity"], "青禾 X1")
        self.assertEqual(plans[0].params["metric"], "续航")
        self.assertEqual(plans[0].strategy, "baseline")
        self.assertEqual(plans[1].strategy, "switch")

    def test_search_claim_rewrites_query_on_second_round(self) -> None:
        plans = self.planner.strategies("青禾 X1 续航实测 60 天能用多久", "other")
        self.assertEqual([plan.strategy for plan in plans], ["baseline", "rewrite"])
        self.assertIn("青禾 X1", plans[1].params["query"])
        self.assertIn("60天", plans[1].params["query"])

    def test_fetch_span_only_after_evidence_exists(self) -> None:
        before = self.planner.strategies("青禾 X1 续航 60 天", "numeric")
        self.assertNotIn("fetch_source", [plan.tool for plan in before])
        after = self.planner.strategies(
            "青禾 X1 续航 60 天", "numeric", [ev("S-001", "A", "2025-03-12", score=3.0)]
        )
        self.assertEqual(after[-1].tool, "fetch_source")
        self.assertEqual(after[-1].params["source_id"], "S-001")

    def test_unknown_entity_drops_fact_lookup_plan(self) -> None:
        plans = self.planner.strategies("某品牌某型号续航 60 天", "numeric")
        self.assertNotIn("lookup_fact", [plan.tool for plan in plans])
        self.assertEqual([plan.tool for plan in plans], ["search_notes", "search_notes"])

    def test_entity_can_come_from_content_context(self) -> None:
        claim = "获得口腔护理协会认证"
        without = self.planner.strategies(claim, "certification")
        self.assertNotIn("lookup_fact", [plan.tool for plan in without])
        with_context = self.planner.strategies(
            claim, "certification", context="青禾 X1 电动牙刷使用体验分享"
        )
        self.assertEqual(with_context[0].tool, "lookup_fact")
        self.assertEqual(with_context[0].params["entity"], "青禾 X1")

    def test_missing_metric_falls_back_to_search(self) -> None:
        """只有实体没有指标时，事实库查询会拉回整实体的记录，容易误触冲突。"""

        plans = self.planner.strategies("云石 P20 一次能给手机充 5 次", "numeric")
        self.assertEqual(plans[0].tool, "search_notes")
        self.assertIn("指标", plans[0].reason)

    def test_permission_claim_looks_up_subject_first(self) -> None:
        plans = self.planner.strategies(
            "可以带上飞机", "other", context="云石 P20 充电宝开箱"
        )
        self.assertEqual(plans[0].tool, "lookup_fact")
        self.assertEqual(plans[0].strategy, "permission_fact")
        self.assertEqual(plans[0].params["metric"], "额定能量")


LIMIT_FACTS = [
    {
        "entity": "民航规定",
        "metric": "可随身携带额定能量上限",
        "value": 100,
        "unit": "Wh",
        "source_id": "S-007",
        "tier": "A",
    }
]


def watt_evidence(source_id: str, tier: str, value: float) -> Evidence:
    return Evidence(
        source_id=source_id,
        tier=tier,
        date="2025-01-01",
        text=f"额定能量 {value}Wh",
        meta={"entity": "云石 P20", "metric": "额定能量", "value": value, "unit": "Wh"},
    )


class PermissionTests(unittest.TestCase):
    def test_all_values_under_limit_is_supported(self) -> None:
        verdict, reason, sources = compare_permission(
            "可以带上飞机",
            [watt_evidence("S-008", "B", 74), watt_evidence("S-010", "B", 62)],
            LIMIT_FACTS,
        )
        self.assertEqual(verdict, "SUPPORTED")
        self.assertIn("不超过", reason)
        # 第三个返回值是承载结论的来源：两条候选值 + 限额来源
        self.assertEqual(set(sources), {"S-008", "S-010", "S-007"})
        self.assertIn("S-007", reason)

    def test_all_values_over_limit_is_contradicted(self) -> None:
        verdict, _reason, _sources = compare_permission(
            "可以带上飞机",
            [watt_evidence("S-008", "B", 160), watt_evidence("S-010", "B", 155)],
            LIMIT_FACTS,
        )
        self.assertEqual(verdict, "CONTRADICTED")

    def test_values_on_both_sides_need_a_human(self) -> None:
        verdict, reason, _sources = compare_permission(
            "可以带上飞机",
            [watt_evidence("S-008", "B", 74), watt_evidence("S-010", "B", 160)],
            LIMIT_FACTS,
        )
        self.assertEqual(verdict, "INSUFFICIENT")
        self.assertIn("两侧", reason)

    def test_unrelated_claim_returns_none(self) -> None:
        self.assertIsNone(
            compare_permission("青禾 X1 续航 60 天", [watt_evidence("S-008", "B", 74)], LIMIT_FACTS)
        )

    def test_missing_limit_fact_returns_none(self) -> None:
        self.assertIsNone(
            compare_permission("可以带上飞机", [watt_evidence("S-008", "B", 74)], [])
        )

    def test_no_subject_value_returns_none(self) -> None:
        plain = Evidence(source_id="S-009", tier="C", date="2025-06-30", text="带货文案")
        self.assertIsNone(compare_permission("可以带上飞机", [plain], LIMIT_FACTS))


class FailTool(Tool):
    """永远失败的检索工具，用于验证 TOOL_FAILED 终态。"""

    spec = ToolSpec(
        name="search_notes",
        purpose="测试用失败工具",
        params={"query": ParamSpec("string", True, "查询词"), "top_k": ParamSpec("int", False, "条数", default=5)},
        returns="无",
        timeout_s=5.0,
        max_retries=1,
        fallback=None,
    )

    def _run(self, params):
        return Outcome(STATUS_UPSTREAM_ERROR, error="模拟上游故障")


class FakeJudgeAdapter:
    """返回一条合法判定的假模型，用于验证模型判定通路。"""

    model_id = "fake-judge"

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, messages, **kwargs) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text=self.payload, model_id=self.model_id, attempts=1)


class AgentIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = LocalCorpus.from_dir(CORPUS_DIR)

    def agent(self, **kwargs) -> EvidenceAgent:
        return make_agent(self.corpus, **kwargs)

    def test_numeric_overstatement_is_contradicted(self) -> None:
        outcome = self.agent().run_content("C-1", ["青禾 X1 续航 60 天"]) 
        claim = outcome.claims[0]
        self.assertEqual(claim.state, JUDGED)
        self.assertEqual(claim.verdict, "CONTRADICTED")
        self.assertFalse(claim.in_review_queue)
        self.assertEqual(claim.attempts[0].plan.tool, "lookup_fact")
        self.assertIn("S-001", claim.evidence)

    def test_numeric_claim_matching_source_is_supported(self) -> None:
        outcome = self.agent().run_content("C-2", ["云石 P20 容量 20000mAh"])
        claim = outcome.claims[0]
        self.assertEqual(claim.state, JUDGED)
        self.assertEqual(claim.verdict, "SUPPORTED")

    def test_certification_conflict_escalates(self) -> None:
        outcome = self.agent().run_content(
            "C-3",
            ["获得口腔护理协会认证"],
            content="青禾 X1 电动牙刷使用体验分享，获得口腔护理协会认证。",
        )
        claim = outcome.claims[0]
        self.assertEqual(claim.state, ESCALATED)
        self.assertIn("STALE", claim.flags)
        self.assertTrue(claim.in_review_queue)
        self.assertIn("过期", claim.reason)

    def test_energy_conflict_escalates(self) -> None:
        outcome = self.agent().run_content("C-4", ["云石 P20 额定能量 74Wh"])
        claim = outcome.claims[0]
        self.assertEqual(claim.state, ESCALATED)
        # 厂商标称与负载下实测不是同一件事：这是"不可比"，不是"冲突"
        self.assertIn("NOT_COMPARABLE", claim.flags)

    def test_unsearchable_claim_escalates_after_retries(self) -> None:
        outcome = self.agent().run_content("C-5", ["莱茵 H9 附带紫外线杀菌灯"])
        claim = outcome.claims[0]
        self.assertEqual(claim.state, ESCALATED)
        self.assertGreaterEqual(claim.tool_calls, 2)  # 换过查询词
        # 2026-10-13：语料扩容后改用一条与语料零词面重叠的主张（紫外线杀菌灯），才能真的走到「用尽」；
        # 原来那条（滤芯寿命很长）现在会命中另一个产品的文档，变成「证据充分但规则无法判定」——
        # 那是检索层没有相关性／实体门禁的表现（13 号文档已知局限 1），由 GAP-006 记录。
        self.assertIn("用尽", claim.reason)

    def test_sufficient_but_not_rule_decidable_escalates_without_model(self) -> None:
        outcome = self.agent().run_content("C-6", ["青禾 X1 防水等级达到 IPX7"])
        claim = outcome.claims[0]
        self.assertEqual(claim.state, ESCALATED)
        self.assertIn("规则无法判定", claim.reason)

    def test_model_judge_is_used_when_rules_cannot_decide(self) -> None:
        adapter = FakeJudgeAdapter(
            json.dumps(
                {
                    "verdict": "SUPPORTED",
                    "flags": [],
                    "evidence": ["S-001"],
                    "reasoning": "规格页标注 IPX7",
                    "claim_type": "numeric",
                },
                ensure_ascii=False,
            )
        )
        outcome = self.agent(adapter=adapter).run_content(
            "C-7", ["青禾 X1 防水等级达到 IPX7"]
        )
        claim = outcome.claims[0]
        self.assertEqual(claim.state, JUDGED)
        self.assertEqual(claim.verdict, "SUPPORTED")
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(len(claim.model_calls), 1)

    def test_tool_failure_reports_tool_failed_not_escalated(self) -> None:
        registry = ToolRegistry([FailTool()])
        agent = EvidenceAgent(registry)
        claim = agent.run_claim("内容", "这款产品支持卫星通话功能")
        self.assertEqual(claim.state, TOOL_FAILED)
        self.assertTrue(claim.in_review_queue)
        self.assertIn("工具连续失败", claim.reason)

    def test_budget_is_per_claim(self) -> None:
        outcome = self.agent().run_content(
            "C-8", ["青禾 X1 续航 60 天", "云石 P20 容量 20000mAh"]
        )
        self.assertEqual([c.state for c in outcome.claims], [JUDGED, JUDGED])
        self.assertEqual(len(outcome.claims[0].attempts), 1)

    def test_content_summary_counts(self) -> None:
        outcome = self.agent().run_content(
            "C-9", ["青禾 X1 续航 60 天", "该产品获得口腔护理协会认证"]
        )
        summary = outcome.summary()
        self.assertEqual(summary["claims"], 2)
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["escalated"], 1)
        self.assertEqual(summary["tool_failed"], 0)
        self.assertGreaterEqual(summary["tool_calls"], 2)

    def test_trace_records_plan_assess_and_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with TraceWriter(Path(tmp)) as writer:
                self.agent(trace=writer).run_content("C-10", ["青禾 X1 续航 60 天"])
            events = [
                json.loads(line)
                for line in (Path(tmp) / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            ]
        kinds = [event["event"] for event in events]
        self.assertIn("agent_start", kinds)
        self.assertIn("plan", kinds)
        self.assertIn("assess", kinds)
        self.assertIn("claim_finished", kinds)
        self.assertIn("agent_finished", kinds)
        plan_event = next(event for event in events if event["event"] == "plan")
        self.assertEqual(plan_event["tool"], "lookup_fact")
        self.assertTrue(plan_event["selection_reason"])

    def test_permission_claim_is_verified_even_with_conflicting_values(self) -> None:
        """74Wh 与 62Wh 互相冲突，但都在 100Wh 以内，所以"能带上飞机"成立。"""

        outcome = self.agent().run_content(
            "C-11",
            ["可以带上飞机"],
            content="云石 P20 充电宝开箱：20000mAh 大容量，额定能量 74Wh，可以带上飞机。",
        )
        claim = outcome.claims[0]
        self.assertEqual(claim.state, JUDGED)
        self.assertEqual(claim.verdict, "SUPPORTED")
        self.assertEqual(claim.flags, [])
        self.assertEqual(claim.disposition.tier, "release")
        self.assertEqual(claim.evidence_override, "E3")
        self.assertEqual(claim.attempts[0].plan.strategy, "permission_fact")


if __name__ == "__main__":
    unittest.main()

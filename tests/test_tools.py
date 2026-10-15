"""Unit tests for the Week-1 tool layer: registry, corpus and builtin tools."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.tools import (  # noqa: E402
    STATUS_BAD_REQUEST,
    STATUS_BUDGET_EXCEEDED,
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    STATUS_UPSTREAM_ERROR,
    CallBudget,
    Evidence,
    LocalCorpus,
    Outcome,
    ParamSpec,
    Tool,
    ToolRegistry,
    ToolSpec,
    build_tools,
    score,
    terms,
)

from evidence_agent.grading import grade_of  # noqa: E402

CORPUS_DIR = ROOT / "data" / "corpus"


def _spec(name: str, **overrides) -> ToolSpec:
    base = dict(
        name=name,
        purpose="测试用工具",
        params={"q": ParamSpec("string", True, "查询词")},
        returns="测试结果",
        timeout_s=5.0,
        max_retries=2,
        fallback=None,
    )
    base.update(overrides)
    return ToolSpec(**base)


class FlakyTool(Tool):
    """前 N 次失败，之后成功。用于验证重试。"""

    def __init__(self, fail_times: int, status: str = STATUS_UPSTREAM_ERROR) -> None:
        self.spec = _spec("flaky")
        self.fail_times = fail_times
        self.status = status
        self.calls = 0

    def _run(self, params):
        self.calls += 1
        if self.calls <= self.fail_times:
            return Outcome(self.status, error="模拟失败")
        return Outcome(STATUS_OK, items=[Evidence("S-001", "A", "2025-01-01", "命中")])


class BoomTool(Tool):
    """直接抛异常，验证 registry 兜底。"""

    def __init__(self) -> None:
        self.spec = _spec("boom", max_retries=0)
        self.calls = 0

    def _run(self, params):
        self.calls += 1
        raise RuntimeError("炸了")


class FakeClock:
    """每次读取前进固定毫秒数，用来在测试里模拟超时。"""

    def __init__(self, step_ms: int = 0) -> None:
        self.step = step_ms / 1000.0
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def make_registry(*tools: Tool, clock=None, budget: CallBudget | None = None):
    return ToolRegistry(
        tools,
        sleep=lambda _seconds: None,
        clock=clock or FakeClock(0),
        budget=budget,
    )


class RegistryGuardTests(unittest.TestCase):
    def test_unknown_tool_is_unavailable_and_recorded(self) -> None:
        registry = make_registry()
        result = registry.call("nope", {"q": "x"})
        self.assertEqual(result.status, STATUS_UNAVAILABLE)
        self.assertFalse(result.ok)
        self.assertEqual(len(registry.records), 1)
        self.assertEqual(registry.records[0].tool, "nope")

    def test_missing_required_param_is_rejected_without_calling_tool(self) -> None:
        tool = FlakyTool(fail_times=0)
        registry = make_registry(tool)
        result = registry.call("flaky", {})
        self.assertEqual(result.status, STATUS_BAD_REQUEST)
        self.assertIn("q", result.error or "")
        self.assertEqual(tool.calls, 0)

    def test_unknown_param_is_rejected(self) -> None:
        tool = FlakyTool(fail_times=0)
        registry = make_registry(tool)
        result = registry.call("flaky", {"q": "x", "extra": 1})
        self.assertEqual(result.status, STATUS_BAD_REQUEST)
        self.assertEqual(tool.calls, 0)

    def test_wrong_type_is_rejected(self) -> None:
        tool = FlakyTool(fail_times=0)
        registry = make_registry(tool)
        result = registry.call("flaky", {"q": 123})
        self.assertEqual(result.status, STATUS_BAD_REQUEST)
        self.assertEqual(tool.calls, 0)


class RegistryRetryTests(unittest.TestCase):
    def test_retryable_failure_then_success(self) -> None:
        tool = FlakyTool(fail_times=2)
        registry = make_registry(tool)
        result = registry.call("flaky", {"q": "青禾"})
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(tool.calls, 3)
        self.assertEqual(result.source_ids(), ["S-001"])

    def test_retries_stop_at_limit(self) -> None:
        tool = FlakyTool(fail_times=99)
        registry = make_registry(tool)
        result = registry.call("flaky", {"q": "青禾"})
        self.assertEqual(result.status, STATUS_UPSTREAM_ERROR)
        self.assertEqual(result.attempts, 3)  # 1 次调用 + 2 次重试
        self.assertTrue(result.retryable)

    def test_empty_is_not_retried(self) -> None:
        class EmptyTool(Tool):
            def __init__(self) -> None:
                self.spec = _spec("empty")
                self.calls = 0

            def _run(self, params):
                self.calls += 1
                return Outcome(STATUS_EMPTY)

        tool = EmptyTool()
        registry = make_registry(tool)
        result = registry.call("empty", {"q": "青禾"})
        self.assertEqual(result.status, STATUS_EMPTY)
        self.assertEqual(tool.calls, 1)  # 换查询词是运行时的策略，不是工具层重试
        self.assertTrue(result.ok)
        self.assertTrue(result.empty)

    def test_unexpected_exception_becomes_upstream_error(self) -> None:
        tool = BoomTool()
        registry = make_registry(tool)
        result = registry.call("boom", {"q": "青禾"})
        self.assertEqual(result.status, STATUS_UPSTREAM_ERROR)
        self.assertIn("RuntimeError", result.error or "")
        self.assertEqual(tool.calls, 1)

    def test_over_timeout_is_converted_to_timeout(self) -> None:
        tool = FlakyTool(fail_times=0)
        registry = make_registry(tool, clock=FakeClock(step_ms=6000))
        result = registry.call("flaky", {"q": "青禾"})
        self.assertEqual(result.status, STATUS_TIMEOUT)
        self.assertEqual(result.attempts, 3)


class RegistryBudgetTests(unittest.TestCase):
    def test_call_budget_blocks_further_calls(self) -> None:
        tool = FlakyTool(fail_times=0)
        budget = CallBudget(max_calls=1, max_seconds=60)
        registry = make_registry(tool, budget=budget)
        first = registry.call("flaky", {"q": "青禾"})
        second = registry.call("flaky", {"q": "青禾"})
        self.assertEqual(first.status, STATUS_OK)
        self.assertEqual(second.status, STATUS_BUDGET_EXCEEDED)
        self.assertEqual(tool.calls, 1)
        self.assertTrue(budget.exhausted())

    def test_budget_exhausts_on_elapsed_time(self) -> None:
        tool = FlakyTool(fail_times=0)
        budget = CallBudget(max_calls=10, max_seconds=0.5)
        registry = make_registry(tool, clock=FakeClock(step_ms=1000), budget=budget)
        registry.call("flaky", {"q": "青禾"})
        blocked = registry.call("flaky", {"q": "青禾"})
        self.assertEqual(blocked.status, STATUS_BUDGET_EXCEEDED)

    def test_per_call_budget_overrides_default(self) -> None:
        tool = FlakyTool(fail_times=0)
        registry = make_registry(tool)
        tight = CallBudget(max_calls=0)
        result = registry.call("flaky", {"q": "青禾"}, budget=tight)
        self.assertEqual(result.status, STATUS_BUDGET_EXCEEDED)
        self.assertEqual(tool.calls, 0)

    def test_registry_has_no_implicit_global_budget(self) -> None:
        """预算是按主张的。全局默认预算会让结论依赖调用顺序。"""

        tool = FlakyTool(fail_times=0)
        registry = make_registry(tool)
        for _ in range(6):
            self.assertEqual(registry.call("flaky", {"q": "青禾"}).status, STATUS_OK)
        self.assertEqual(tool.calls, 6)
        self.assertIsNone(registry.budget)

    def test_two_claims_get_independent_budgets(self) -> None:
        tool = FlakyTool(fail_times=0)
        registry = make_registry(tool)
        first_claim = CallBudget(max_calls=1)
        second_claim = CallBudget(max_calls=1)

        ok = registry.call("flaky", {"q": "青禾"}, budget=first_claim)
        blocked = registry.call("flaky", {"q": "青禾"}, budget=first_claim)
        fresh = registry.call("flaky", {"q": "青禾"}, budget=second_claim)

        self.assertEqual(ok.status, STATUS_OK)
        self.assertEqual(blocked.status, STATUS_BUDGET_EXCEEDED)
        self.assertEqual(fresh.status, STATUS_OK)  # 上一条主张耗尽不影响这一条
        self.assertEqual(tool.calls, 2)


class CorpusSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = LocalCorpus.from_dir(CORPUS_DIR)

    def test_terms_split_chinese_into_bigrams(self) -> None:
        self.assertIn("续航", terms("续航"))
        self.assertIn("20000", terms("20000mAh"))

    def test_score_ranks_title_hits_higher(self) -> None:
        with_title = score("续航", "续航复测报告", "无关正文")
        without_title = score("续航", "无关标题", "正文提到续航")
        self.assertGreater(with_title, without_title)

    def test_search_finds_recency_report(self) -> None:
        hits = self.corpus.search("青禾 X1 续航 实测")
        self.assertTrue(hits)
        self.assertIn("S-002", [hit.source_id for hit in hits])

    def test_search_returns_empty_for_unrelated_query(self) -> None:
        self.assertEqual(self.corpus.search("量子计算机 超导 制冷机"), [])

    def test_search_respects_top_k(self) -> None:
        self.assertLessEqual(len(self.corpus.search("充电宝 容量", top_k=2)), 2)

    def test_alias_resolution(self) -> None:
        self.assertEqual(self.corpus.resolve_entity("青禾X1"), ["青禾 X1"])
        self.assertEqual(self.corpus.resolve_entity("云石 P20 充电宝"), ["云石 P20"])
        self.assertEqual(self.corpus.resolve_entity("不存在的牌子"), [])

    def test_lookup_fact_returns_conflicting_rows(self) -> None:
        rows = self.corpus.lookup_fact("P20", "额定能量")
        values = {row["value"] for row in rows}
        self.assertEqual(values, {74, 62})
        self.assertEqual(len(rows), 2)

    def test_lookup_fact_filters_by_period(self) -> None:
        rows = self.corpus.lookup_fact("青禾 X1", "续航", "2025")
        self.assertEqual([row["value"] for row in rows], [19.5, 21])

    def test_lookup_fact_enriches_publisher(self) -> None:
        rows = self.corpus.lookup_fact("青禾 X1", "续航")
        self.assertTrue(all(row.get("publisher") for row in rows))

    def test_get_doc_missing_returns_none(self) -> None:
        self.assertIsNone(self.corpus.get_doc("S-999"))

    def test_locate_falls_back_to_relevant_sentence(self) -> None:
        text, span = self.corpus.locate("S-010", "额定能量")
        self.assertIn("额定能量", text)
        self.assertIsNotNone(span)


class BuiltinToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = LocalCorpus.from_dir(CORPUS_DIR)
        cls.tools = {tool.spec.name: tool for tool in build_tools(cls.corpus)}

    def test_specs_match_prd(self) -> None:
        expected = {
            "search_notes": (8.0, 2, "lookup_fact"),
            "lookup_fact": (5.0, 2, None),
            "fetch_source": (10.0, 1, None),
        }
        for name, (timeout, retries, fallback) in expected.items():
            spec = self.tools[name].spec
            self.assertEqual(spec.timeout_s, timeout, name)
            self.assertEqual(spec.max_retries, retries, name)
            self.assertEqual(spec.fallback, fallback, name)
            self.assertTrue(spec.read_only, name)

    def test_search_notes_hits(self) -> None:
        registry = make_registry(self.tools["search_notes"])
        result = registry.call("search_notes", {"query": "云石 P20 额定能量"})
        self.assertEqual(result.status, STATUS_OK)
        self.assertIn("S-008", result.source_ids())

    def test_search_notes_empty_for_nonsense(self) -> None:
        registry = make_registry(self.tools["search_notes"])
        result = registry.call("search_notes", {"query": "量子计算机 超导 制冷机"})
        self.assertEqual(result.status, STATUS_EMPTY)

    def test_lookup_fact_reports_conflict(self) -> None:
        registry = make_registry(self.tools["lookup_fact"])
        result = registry.call(
            "lookup_fact", {"entity": "云石 P20", "metric": "额定能量"}
        )
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(sorted(result.source_ids()), ["S-008", "S-010"])

    def test_lookup_fact_unknown_entity_is_empty(self) -> None:
        registry = make_registry(self.tools["lookup_fact"])
        result = registry.call("lookup_fact", {"entity": "不存在"})
        self.assertEqual(result.status, STATUS_EMPTY)

    def test_fetch_source_returns_span(self) -> None:
        registry = make_registry(self.tools["fetch_source"])
        result = registry.call(
            "fetch_source", {"source_id": "S-001", "span_hint": "IPX7"}
        )
        self.assertEqual(result.status, STATUS_OK)
        self.assertIsNotNone(result.items[0].span)
        self.assertFalse(result.degraded)

    def test_fetch_source_degrades_without_span(self) -> None:
        registry = make_registry(self.tools["fetch_source"])
        result = registry.call(
            "fetch_source", {"source_id": "S-001", "span_hint": "完全不存在的片段xyz"}
        )
        self.assertTrue(result.degraded)
        self.assertEqual(result.degrade_reason, "UNLOCATED_SPAN")
        self.assertLessEqual(len(result.items[0].text), 200)

    def test_fetch_source_carries_source_dimensions(self) -> None:
        """取原文不能丢来源维度。

        丢了会退回按等级折算：同一份资料走 search_notes 是 0.70（厂商自述），
        走 fetch_source 是 0.95（未知）。同一份证据在两个路径上被排出不同优先级。
        """

        registry = make_registry(self.tools["fetch_source"])
        fetched = registry.call(
            "fetch_source", {"source_id": "S-001", "span_hint": "续航"}
        )
        item = fetched.items[0]
        self.assertTrue(item.meta)
        self.assertEqual(item.meta.get("publisher"), "青禾官网")

        searched = make_registry(self.tools["search_notes"]).call(
            "search_notes", {"query": "青禾 X1 续航 21 天"}
        )
        same_doc = next(i for i in searched.items if i.source_id == "S-001")
        self.assertEqual(grade_of(item).weight, grade_of(same_doc).weight)

    def test_search_result_carries_structured_facts(self) -> None:
        """检索路径要把资料在事实库里的结构化记录带出来，否则冲突判定看不见它。"""

        registry = make_registry(self.tools["search_notes"])
        result = registry.call("search_notes", {"query": "云石 P20 额定能量"})
        item = next(i for i in result.items if i.source_id == "S-010")
        facts = item.meta.get("facts") or []
        self.assertIn("额定能量", {fact.get("metric") for fact in facts})
        self.assertEqual([fact.get("value") for fact in facts], [62])

    def test_fetch_source_missing_id_is_empty(self) -> None:
        registry = make_registry(self.tools["fetch_source"])
        result = registry.call("fetch_source", {"source_id": "S-999"})
        self.assertEqual(result.status, STATUS_EMPTY)

    def test_registry_records_carry_selection_reason(self) -> None:
        registry = make_registry(*self.tools.values())
        registry.call(
            "lookup_fact",
            {"entity": "青禾 X1", "metric": "续航"},
            selection_reason="numeric 类型优先查事实库",
        )
        record = registry.records[-1]
        self.assertEqual(record.selection_reason, "numeric 类型优先查事实库")
        self.assertIn("budget", record.to_dict())


if __name__ == "__main__":
    unittest.main()

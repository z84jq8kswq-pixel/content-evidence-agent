"""Unit tests for the workbench API helpers (no HTTP server needed)."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
APP = ROOT / "app"
for path in (SRC, APP):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import server  # noqa: E402


def make_agent_run(root: Path, run_id: str, payload: dict, meta: dict | None = None) -> None:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    if meta is not None:
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )


def agent_payload() -> dict:
    return {
        "run_id": "unit-run",
        "adapter": "rule-only",
        "metrics": {
            "claims": 2,
            "judged_count": 1,
            "escalated_count": 1,
            "tool_failed_count": 0,
            "state_accuracy": 1.0,
            "escalation_rate": 0.5,
            "avg_tool_calls_per_claim": 1.5,
        },
        "v2_ceiling": {"ceiling": 0.0},
        "cases": [
            {
                "case_id": "GAP-001",
                "category": "数值夸大",
                "difficulty": "easy",
                "content": "云石 P20 和青禾 X1 的对比笔记：青禾 X1 续航 60 天，还获得过认证。",
                "given_sources": ["S-004"],
                "what_it_tests": "测试",
                "claims": [
                    {
                        "claim": "青禾 X1 续航 60 天",
                        "claim_type": "numeric",
                        "state": "JUDGED",
                        "verdict": "CONTRADICTED",
                        "flags": [],
                        "reason": "与最权威来源相差 186%",
                        "evidence": ["S-001"],
                        "tool_calls": 1,
                        "steps": [
                            {
                                "strategy": "baseline",
                                "tool": "lookup_fact",
                                "params": {"entity": "青禾 X1", "metric": "续航"},
                                "selection_reason": "numeric 类型优先查事实库",
                                "status": "OK",
                                "latency_ms": 0,
                                "degraded": False,
                                "digest": "1 条结果",
                                "sources": ["S-001"],
                            }
                        ],
                    },
                    {
                        "claim": "获得口腔护理协会认证",
                        "claim_type": "certification",
                        "state": "ESCALATED",
                        "verdict": "INSUFFICIENT",
                        "flags": ["STALE"],
                        "reason": "来源已被更新的记录覆盖（过期），不做判定",
                        "evidence": ["S-005", "S-006"],
                        "tool_calls": 2,
                        "steps": [
                            {
                                "strategy": "baseline",
                                "tool": "search_notes",
                                "params": {"query": "青禾 X1 认证"},
                                "selection_reason": "certification 类型但认不出指标，改用检索",
                                "status": "EMPTY",
                                "latency_ms": 0,
                                "degraded": False,
                                "digest": "零结果",
                                "sources": [],
                            },
                            {
                                "strategy": "switch",
                                "tool": "lookup_fact",
                                "params": {"entity": "青禾 X1", "metric": "认证"},
                                "selection_reason": "事实库查询命中两条",
                                "status": "OK",
                                "latency_ms": 0,
                                "degraded": False,
                                "digest": "2 条结果",
                                "sources": ["S-005", "S-006"],
                            },
                        ],
                    },
                ],
            }
        ],
    }


def legacy_payload() -> dict:
    """老评测的真实格式：顶层是 meta / summary / cases，没有 metrics。"""

    return {
        "meta": {"run_id": "20261003-200654-deepseek-v1", "adapter": "deepseek"},
        "summary": {"cases": 30},
        "cases": [{"case_id": "EV-001", "predicted_claims": []}],
    }


class DetectionTests(unittest.TestCase):
    def test_normalize_path_strips_copied_punctuation(self) -> None:
        """从聊天里复制链接时，结尾的中文标点会被一起带进来。"""

        self.assertEqual(server.normalize_path("/agent）。"), "/agent")
        self.assertEqual(server.normalize_path("/tools，"), "/tools")

    def test_normalize_path_strips_trailing_slash(self) -> None:
        self.assertEqual(server.normalize_path("/agent/"), "/agent")
        self.assertEqual(server.normalize_path("/"), "/")
        self.assertEqual(server.normalize_path(""), "/")

    def test_normalize_path_keeps_real_typos(self) -> None:
        self.assertEqual(server.normalize_path("/agnt"), "/agnt")

    def test_route_path_decodes_before_normalizing(self) -> None:
        """浏览器会把中文标点编码，必须先解码再去噪，否则规则匹配不上。"""

        self.assertEqual(server.route_path("/agent%EF%BC%89%E3%80%82"), "/agent")
        self.assertEqual(server.route_path("/tools/"), "/tools")
        self.assertEqual(server.route_path("/api/queue?run=gap-baseline"), "/api/queue")

    def test_old_format_results_are_not_agent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_agent_run(root, "old-run", legacy_payload())
            with self.assertRaises(ValueError):
                server.load_agent_results("old-run", root)

    def test_missing_run_raises_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                server.load_agent_results("nope", Path(tmp))

    def test_agent_comparison_between_two_agent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_agent_run(root, "gap-a", agent_payload(), {"run_id": "gap-a"})
            make_agent_run(root, "gap-b", agent_payload(), {"run_id": "gap-b"})

            comparison = server.agent_comparison_for("gap-a", "gap-b", root)
            self.assertEqual(comparison["family"], "agent")
            self.assertEqual(comparison["base"]["run"], "gap-a")
            self.assertTrue(comparison["rows"])

    def test_agent_comparison_rejects_legacy_runs(self) -> None:
        """拿老评测的编号问 v3 接口要给 400，而不是给一堆空指标。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_agent_run(root, "old-run", legacy_payload())
            make_agent_run(root, "gap-a", agent_payload(), {"run_id": "gap-a"})

            with self.assertRaises(ValueError):
                server.agent_comparison_for("old-run", "gap-a", root)

    def test_list_agent_runs_skips_old_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_agent_run(root, "gap-a", agent_payload(), {"created_at": "2026-10-08T00:00:00Z"})
            make_agent_run(root, "old-run", legacy_payload())
            (root / "runs" / "junk").mkdir()
            runs = server.list_agent_runs(root)
            self.assertEqual([row["id"] for row in runs], ["gap-a"])
            self.assertEqual(runs[0]["adapter"], "rule-only")
            self.assertEqual(runs[0]["claims"], 2)
            self.assertEqual(runs[0]["v2_ceiling"], 0.0)
            self.assertEqual(runs[0]["created_at"], "2026-10-08T00:00:00Z")

    def test_legacy_run_ids_exclude_agent_runs(self) -> None:
        """v3 运行不能混进老评测的下拉框，否则会出现空视图并诱导比错数字。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_agent_run(root, "gap-a", agent_payload())
            make_agent_run(root, "old-run", legacy_payload())
            (root / "runs" / "junk").mkdir()
            self.assertEqual(server.legacy_run_ids(root), ["old-run"])

    def test_is_legacy_run_tolerates_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(server.is_legacy_run("nope", Path(tmp)))


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # build_queue 会读语料把资料编号换成人能读的标题，所以临时根目录也要有语料
        shutil.copytree(ROOT / "data" / "corpus", self.root / "data" / "corpus")
        make_agent_run(self.root, "gap-a", agent_payload())
        self._original_reviews = server.REVIEWS_PATH
        server.REVIEWS_PATH = self.root / "reviews.jsonl"

    def tearDown(self) -> None:
        server.REVIEWS_PATH = self._original_reviews
        self.tmp.cleanup()

    def test_queue_orders_pending_first(self) -> None:
        queue = server.build_queue("gap-a", self.root)
        self.assertEqual(queue["claims"], 2)
        self.assertEqual(queue["counts"]["needs_human"], 1)
        self.assertEqual(queue["counts"]["judged"], 1)
        self.assertTrue(queue["items"][0]["needs_human"])
        self.assertEqual(queue["items"][0]["claim"], "获得口腔护理协会认证")

    def test_queue_item_carries_steps_and_content(self) -> None:
        queue = server.build_queue("gap-a", self.root)
        item = queue["items"][0]
        self.assertEqual(len(item["steps"]), 2)
        self.assertEqual(item["steps"][1]["selection_reason"], "事实库查询命中两条")
        self.assertIn("对比笔记", item["content"])
        self.assertEqual(item["key"], "GAP-001#1")
        self.assertEqual(item["case_id"], "GAP-001")
        self.assertEqual(item["claim_index"], 1)

    def test_queue_resolves_source_ids_to_readable_titles(self) -> None:
        queue = server.build_queue("gap-a", self.root)
        item = queue["items"][0]
        titles = [row["title"] for row in item["evidence_detail"]]
        self.assertIn("口腔护理协会 2025 年度认证名录", titles)
        self.assertTrue(all(row["id"].startswith("S-") for row in item["evidence_detail"]))

    def test_queue_marks_suspicious_fragments(self) -> None:
        queue = server.build_queue("gap-a", self.root)
        judged = [item for item in queue["items"] if not item["needs_human"]][0]
        kinds = {mark["kind"] for mark in judged["highlights"]}
        self.assertIn("number-bad", kinds)
        self.assertTrue(judged["content_highlights"])

    def test_missing_corpus_entry_falls_back_to_id(self) -> None:
        detail = server.source_detail(server.corpus_for(self.root), "S-999")
        self.assertEqual(detail["title"], "S-999")
        self.assertEqual(detail["tier"], "")

    def test_review_is_attached_by_key(self) -> None:
        server.REVIEWS_PATH.write_text(
            json.dumps(
                {
                    "run": "gap-a",
                    "case_id": "GAP-001",
                    "claim_index": 1,
                    "decision": "hold",
                    "reviewed_at": "2026-10-08T01:00:00Z",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        queue = server.build_queue("gap-a", self.root)
        reviewed = [item for item in queue["items"] if item["review"]]
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(reviewed[0]["review"]["decision"], "hold")
        self.assertEqual(queue["counts"]["reviewed"], 1)

    def test_reviews_from_other_runs_do_not_leak(self) -> None:
        server.REVIEWS_PATH.write_text(
            json.dumps({"run": "other", "case_id": "GAP-001", "claim_index": 0, "decision": "release"})
            + "\n",
            encoding="utf-8",
        )
        queue = server.build_queue("gap-a", self.root)
        self.assertEqual(queue["counts"]["reviewed"], 0)


class ToolRegistryViewTests(unittest.TestCase):
    def test_tool_view_lists_six_specs_per_tool(self) -> None:
        view = server.tool_registry_view(ROOT)
        names = [tool["name"] for tool in view["tools"]]
        self.assertEqual(names, ["search_notes", "lookup_fact", "fetch_source"])
        by_name = {tool["name"]: tool for tool in view["tools"]}
        search = by_name["search_notes"]
        self.assertEqual(search["timeout_s"], 8.0)
        self.assertEqual(search["max_retries"], 2)
        self.assertEqual(search["fallback"], "lookup_fact")
        self.assertTrue(search["read_only"])
        self.assertEqual([p["name"] for p in search["params"]], ["query", "top_k"])
        self.assertTrue(search["params"][0]["required"])
        # 语料规模随扩样变化，这里只断言"与真实语料一致且不为空"，
        # 不写死数字——写死会在每次扩充语料时变成假失败（已经发生过一次）。
        from evidence_agent.tools import LocalCorpus

        corpus = LocalCorpus.from_dir(ROOT / "data" / "corpus")
        self.assertEqual(view["corpus"]["sources"], len(corpus.docs))
        self.assertGreaterEqual(view["corpus"]["sources"], 24)
        self.assertEqual(view["corpus"]["version"], corpus.version)
        self.assertTrue(view["corpus"]["version"])


if __name__ == "__main__":
    unittest.main()

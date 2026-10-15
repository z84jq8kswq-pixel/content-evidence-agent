"""内容研判工作台的服务层与接口测试。

这一批测试锁的是**改造新增的行为**，尤其是几条不能退让的约束：

1. 输入校验：空内容、超长内容、非法运行编号一律拒绝，且不落盘；
2. 结果必须来自本次运行：跑两次不同内容，第二次的载荷里不能出现第一次的主张；
3. 产物落盘后能读回，且被识别为"内容研判"，不会混进评测运行列表；
4. 接口层：错误分类正确（400／409／200+结构化 error／500 不暴露堆栈）。
"""

from __future__ import annotations

import io
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
from evidence_agent.runtime import workbench  # noqa: E402
from evidence_agent.tools import LocalCorpus  # noqa: E402

#: 两条内容都是真实任务集里的原文，压的是数值判定（一致 / 夸大）。
CONTENT_A = "云石 P20 和青禾 X1 的对比笔记：云石 P20 充电宝容量 20000mAh；青禾 X1 电动牙刷续航 60 天。"
CONTENT_B = "沁澜 R3 净水器：通量 1200G，废水比 1:1。"


class WorkbenchTestCase(unittest.TestCase):
    """临时根目录 + 真实语料副本：测试不写项目的 runs/ 目录。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(ROOT / "data" / "corpus", self.root / "data" / "corpus")
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "config" / "default.json", self.root / "config" / "default.json")
        shutil.copy(ROOT / "data" / "gap_tasks.json", self.root / "data" / "gap_tasks.json")
        self.corpus = LocalCorpus.from_dir(self.root / "data" / "corpus")

    def tearDown(self) -> None:
        self.tmp.cleanup()


class ValidationTests(WorkbenchTestCase):
    def test_empty_content_is_rejected(self) -> None:
        for blank in ("", "   ", "\n\t"):
            with self.assertRaises(workbench.ContentValidationError):
                workbench.validate_content(blank)

    def test_overlong_content_is_rejected_with_the_limit_in_the_message(self) -> None:
        with self.assertRaises(workbench.ContentValidationError) as ctx:
            workbench.validate_content("测" * (workbench.MAX_CONTENT_CHARS + 1))
        self.assertIn(str(workbench.MAX_CONTENT_CHARS), str(ctx.exception))

    def test_content_is_stripped(self) -> None:
        self.assertEqual(workbench.validate_content("  内容 20000mAh  "), "内容 20000mAh")

    def test_illegal_run_ids_are_rejected(self) -> None:
        for bad in ("../evil", "a/b", "", "x" * 80, "a\\b", "run id"):
            with self.assertRaises(workbench.RunIdError, msg=bad):
                workbench.run_content(
                    self.root, CONTENT_A, corpus=self.corpus, run_id=bad, persist=False
                )

    def test_none_run_id_gets_a_generated_one(self) -> None:
        payload = workbench.run_content(
            self.root, CONTENT_A, corpus=self.corpus, persist=False
        )
        self.assertTrue(payload["run_id"].startswith("content-"))


class RunContentTests(WorkbenchTestCase):
    def test_run_produces_claims_with_dispositions(self) -> None:
        payload = workbench.run_content(
            self.root, CONTENT_A, corpus=self.corpus, persist=False
        )
        view = payload["cases"][0]["view"]
        self.assertGreaterEqual(view["summary"]["claims"], 2)
        for claim in view["claims"]:
            self.assertTrue(claim["disposition"], claim["claim"])
            self.assertIn(claim["verdict"], workbench.VERDICT_LABELS)
            self.assertTrue(claim["steps"], "每条主张都要有工具调用记录")
        self.assertEqual(payload["metrics"]["claims"], view["summary"]["claims"])

    def test_dispositions_are_passed_through_not_recomputed(self) -> None:
        """工作台不许重新判定：界面里的档位必须与运行时给出的逐个一致。

        R13（仅厂商自述不得触发对外动作）是 `runtime.disposition` 的规则，这里
        验证的是**这条规则的结果被原样带出来**，包括 `vendor_only` 这个闸门痕迹：
        本样例里「云石 P20 容量 20000mAh」依据是厂商标称，运行时把它标成
        `vendor_only=True`，工作台必须照抄，不能因为结论是 SUPPORTED 就抹掉。
        """

        content = "云石 P20 和青禾 X1 的对比笔记：云石 P20 充电宝容量 20000mAh；青禾 X1 电动牙刷续航 60 天。"
        payload = workbench.run_content(
            self.root, content, corpus=self.corpus, persist=False
        )
        claims = payload["cases"][0]["view"]["claims"]
        self.assertTrue(claims)

        from evidence_agent.runtime import make_agent

        outcome = make_agent(self.corpus).run_content(
            "CONTENT-001", [claim["claim"] for claim in claims], content=content
        )
        runtime_dispositions = [
            (claim.disposition.to_dict() if claim.disposition else None)
            for claim in outcome.claims
        ]
        self.assertEqual([claim["disposition"] for claim in claims], runtime_dispositions)

        vendor_only = [c for c in claims if c["disposition"]["vendor_only"]]
        self.assertTrue(vendor_only, "厂商标称承载的结论应留下 vendor_only 痕迹")
        for claim in vendor_only:
            self.assertEqual(claim["disposition"]["rule"], "R4")
            self.assertFalse(claim["needs_human"])

    def test_second_run_does_not_leak_the_first_result(self) -> None:
        first = workbench.run_content(
            self.root, CONTENT_A, corpus=self.corpus, persist=False
        )
        second = workbench.run_content(
            self.root, CONTENT_B, corpus=self.corpus, persist=False
        )
        first_texts = {c["claim"] for c in first["cases"][0]["view"]["claims"]}
        second_texts = {c["claim"] for c in second["cases"][0]["view"]["claims"]}
        self.assertTrue(first_texts and second_texts)
        self.assertFalse(first_texts & second_texts)
        self.assertEqual(second["cases"][0]["view"]["content"], CONTENT_B)

    def test_no_progress_is_fabricated(self) -> None:
        """后端是同步调用，不能声称自己分阶段执行过。"""

        payload = workbench.run_content(
            self.root, CONTENT_A, corpus=self.corpus, persist=False
        )
        self.assertEqual(payload["stages"]["mode"], "sync")
        self.assertIn("没有分阶段进度", payload["stages"]["label"])

    def test_persistence_round_trip(self) -> None:
        payload = workbench.run_content(
            self.root, CONTENT_A, corpus=self.corpus, run_id="content-unit-001"
        )
        run_dir = self.root / "runs" / "content-unit-001"
        produced = sorted(path.name for path in run_dir.iterdir())
        self.assertEqual(produced, ["report.md", "results.json", "run_meta.json", "trace.jsonl"])

        reloaded = workbench.load_content_payload(self.root, "content-unit-001")
        self.assertEqual(reloaded["run_id"], "content-unit-001")
        claims = reloaded["cases"][0]["view"]["claims"]
        self.assertEqual(len(claims), len(payload["cases"][0]["view"]["claims"]))
        self.assertEqual(reloaded["metrics"]["claims"], payload["metrics"]["claims"])

    def test_existing_run_is_not_overwritten(self) -> None:
        workbench.run_content(
            self.root, CONTENT_A, corpus=self.corpus, run_id="content-unit-002"
        )
        with self.assertRaises(ValueError):
            workbench.run_content(
                self.root, CONTENT_B, corpus=self.corpus, run_id="content-unit-002"
            )

    def test_report_md_states_that_actions_are_not_automatic(self) -> None:
        workbench.run_content(
            self.root, CONTENT_A, corpus=self.corpus, run_id="content-unit-003"
        )
        report = (self.root / "runs" / "content-unit-003" / "report.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("未经人工复核", report)
        self.assertIn("处置动作不自动执行", report)


class ListingTests(WorkbenchTestCase):
    def test_history_only_lists_content_runs(self) -> None:
        make_eval_run(self.root, "gap-fake")
        workbench.run_content(
            self.root, CONTENT_A, corpus=self.corpus, run_id="content-unit-004"
        )
        rows = workbench.list_content_runs(self.root)
        self.assertEqual([row["run_id"] for row in rows], ["content-unit-004"])
        self.assertEqual(rows[0]["claims"], 2)

    def test_content_run_does_not_enter_the_evaluation_views(self) -> None:
        """内容研判不能被评测页当成评测运行：没有标注、没有任务集，比分会误导人。"""

        workbench.run_content(
            self.root, CONTENT_A, corpus=self.corpus, run_id="content-unit-005"
        )
        self.assertEqual(server.list_agent_runs(self.root), [])
        self.assertEqual(server.legacy_run_ids(self.root), [])
        with self.assertRaises(ValueError):
            server.load_agent_results("content-unit-005", self.root)
        meta = server.agent_run_meta("content-unit-005", self.root)
        self.assertEqual(server.classify_run(meta, 1, "abcd1234", 1), "content")
        self.assertEqual(server.RUN_KINDS["content"], "单条内容研判")

    def test_samples_come_from_the_real_task_set(self) -> None:
        samples = workbench.sample_contents(self.root)
        self.assertTrue(samples)
        raw = json.loads((self.root / "data" / "gap_tasks.json").read_text(encoding="utf-8"))
        contents = {case["content"] for case in raw["cases"]}
        for sample in samples:
            self.assertIn(sample["content"], contents)
            self.assertEqual(sample["origin"], "data/gap_tasks.json")


class WorkbenchStatusTests(WorkbenchTestCase):
    def tearDown(self) -> None:
        import os

        os.environ.pop("EVIDENCE_AGENT_ADAPTER", None)
        super().tearDown()

    def test_status_reports_real_corpus_numbers(self) -> None:
        status = server.workbench_status(self.root)
        self.assertEqual(status["corpus"]["sources"], len(self.corpus.docs))
        self.assertEqual(status["corpus"]["facts"], len(self.corpus.facts))
        self.assertEqual(status["adapter"], "rule-only")
        self.assertIn("max_content_chars", status["limits"])

    def test_status_honours_a_configured_adapter(self) -> None:
        import os

        os.environ["EVIDENCE_AGENT_ADAPTER"] = "deepseek"
        status = server.workbench_status(self.root)
        self.assertEqual(status["adapter"], "deepseek")
        self.assertIn("model_key_ready", status)

    def test_unknown_adapter_falls_back_to_rule_only(self) -> None:
        import os

        os.environ["EVIDENCE_AGENT_ADAPTER"] = "gpt-9"
        self.assertEqual(server.configured_adapter(), "rule-only")
        self.assertEqual(server.workbench_status(self.root)["adapter"], "rule-only")


class ApiRequestTests(WorkbenchTestCase):
    def test_run_content_request_returns_a_real_run(self) -> None:
        body, status = server.run_content_request(self.root, {"content": CONTENT_A})
        self.assertEqual(status, 200)
        self.assertNotIn("error", body)
        self.assertTrue(body["cases"][0]["view"]["claims"])
        run_id = body["run_id"]
        self.assertTrue((self.root / "runs" / run_id / "results.json").exists())

    def test_non_string_content_is_rejected(self) -> None:
        body, status = server.run_content_request(self.root, {"content": 123})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["kind"], "invalid_content")

    def test_unsupported_adapter_is_rejected(self) -> None:
        body, status = server.run_content_request(
            self.root, {"content": CONTENT_A, "adapter": "claude"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["kind"], "invalid_adapter")
        self.assertEqual(body["error"]["allowed"], list(workbench.ADAPTERS))

    def test_bad_run_id_is_rejected_without_writing(self) -> None:
        body, status = server.run_content_request(
            self.root, {"content": CONTENT_A, "run_id": "../../escape"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["kind"], "invalid_run_id")
        self.assertFalse((self.root / "runs").exists())

    def test_duplicate_run_id_reports_a_conflict(self) -> None:
        first, status = server.run_content_request(
            self.root, {"content": CONTENT_A, "run_id": "content-unit-006"}
        )
        self.assertEqual(status, 200)
        second, status = server.run_content_request(
            self.root, {"content": CONTENT_B, "run_id": "content-unit-006"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(second["error"]["kind"], "run_conflict")
        self.assertEqual(first["run_id"], "content-unit-006")

    def test_model_without_key_is_a_runtime_condition_not_a_server_error(self) -> None:
        body, status = server.run_content_request(
            self.root, {"content": CONTENT_A, "adapter": "deepseek"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["error"]["kind"], "model_unavailable")
        self.assertIn("hint", body["error"])

    def test_unexpected_failure_does_not_expose_the_stack(self) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("内部实现细节不应出现在响应里")

        original = workbench.run_content
        workbench.run_content = boom
        try:
            body, status = server.run_content_request(self.root, {"content": CONTENT_A})
        finally:
            workbench.run_content = original
        self.assertEqual(status, 500)
        self.assertNotIn("内部实现细节", json.dumps(body, ensure_ascii=False))
        self.assertEqual(body["error"]["kind"], "run_failed")


class BodyReadingTests(unittest.TestCase):
    """请求体读取与大小限制：用一个最小的假 handler，不需要起服务。"""

    class FakeHandler:
        def __init__(self, body: bytes, content_length: str | None = None) -> None:
            length = str(len(body)) if content_length is None else content_length
            self.headers = {"Content-Length": length}
            self.rfile = io.BytesIO(body)

    def test_valid_json_object_is_parsed(self) -> None:
        payload, error = server.read_json_body(
            self.FakeHandler(json.dumps({"content": "内容"}).encode("utf-8"))
        )
        self.assertEqual(error, "")
        self.assertEqual(payload["content"], "内容")

    def test_empty_body_is_rejected(self) -> None:
        payload, error = server.read_json_body(self.FakeHandler(b""))
        self.assertEqual(payload, {})
        self.assertTrue(error)

    def test_oversized_body_is_rejected_before_parsing(self) -> None:
        body = b'{"content": "' + b"a" * server.MAX_BODY_BYTES + b'"}'
        payload, error = server.read_json_body(self.FakeHandler(body))
        self.assertEqual(payload, {})
        self.assertIn("过大", error)

    def test_non_object_json_is_rejected(self) -> None:
        payload, error = server.read_json_body(self.FakeHandler(b"[1, 2, 3]"))
        self.assertEqual(payload, {})
        self.assertTrue(error)

    def test_invalid_content_length_is_rejected(self) -> None:
        payload, error = server.read_json_body(self.FakeHandler(b"{}", content_length="abc"))
        self.assertEqual(payload, {})
        self.assertTrue(error)


class RouteTests(unittest.TestCase):
    def test_workbench_page_and_api_paths(self) -> None:
        self.assertEqual(server.route_path("/workbench"), "/workbench")
        self.assertEqual(server.route_path("/workbench/"), "/workbench")
        self.assertEqual(server.route_path("/api/run-content"), "/api/run-content")
        self.assertEqual(server.route_path("/api/content-run?run=x"), "/api/content-run")

    def test_workbench_page_is_served_from_static(self) -> None:
        static = Path(server.STATIC_DIR)
        self.assertTrue((static / "workbench.html").is_file())
        self.assertTrue((static / "workbench.js").is_file())
        self.assertTrue((static / "workbench.css").is_file())


def make_eval_run(root: Path, run_id: str) -> None:
    """造一个最小的评测运行产物，用来验证两种运行不会互相污染。"""

    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "adapter": "rule-only",
                "extractor": "rule",
                "metrics": {"claims": 1},
                "cases": [{"case_id": "GAP-001", "claims": []}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "run_meta.json").write_text(
        json.dumps({"run_id": run_id, "adapter": "rule-only"}, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()

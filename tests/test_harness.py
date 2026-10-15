"""Unit tests for matching, rules, parsing and metrics."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.adapters.stub import StubAdapter  # noqa: E402
from evidence_agent.adapters.base import AdapterConfigError  # noqa: E402
from evidence_agent.adapters.deepseek import DeepSeekAdapter  # noqa: E402
from evidence_agent.matching import match_claims, similarity  # noqa: E402
from evidence_agent.metrics import aggregate  # noqa: E402
from evidence_agent.models import (  # noqa: E402
    CaseResult,
    PredictedClaim,
    should_review,
)
from evidence_agent.pipeline import (  # noqa: E402
    V2Pipeline,
    extract_claims,
    parse_json_object,
    parse_single_claim,
)
from evidence_agent.dataset import load_cases  # noqa: E402
from evidence_agent.envfile import load_env_file  # noqa: E402


class MatchingTests(unittest.TestCase):
    def test_similarity_ignores_punctuation(self) -> None:
        self.assertGreater(
            similarity("澜声 Buds Pro 综合续航 40 小时", "澜声Buds Pro综合续航40小时"),
            0.9,
        )

    def test_matching_is_one_to_one(self) -> None:
        pairs, unmatched_e, unmatched_p = match_claims(
            ["墨影 X1 续航 20 小时", "全系支持无线充电"],
            ["墨影 X1 续航 20 小时", "无线充电"],
            threshold=0.2,
        )
        self.assertEqual(len(pairs), 2)
        self.assertEqual(unmatched_e, [])
        self.assertEqual(unmatched_p, [])

    def test_unrelated_claims_do_not_match(self) -> None:
        pairs, unmatched_e, unmatched_p = match_claims(
            ["墨影 X1 续航 20 小时"], ["洗地机滚刷可拆洗"], threshold=0.3
        )
        self.assertEqual(pairs, [])
        self.assertEqual(len(unmatched_e), 1)
        self.assertEqual(len(unmatched_p), 1)


class RuleTests(unittest.TestCase):
    def test_partial_always_queues(self) -> None:
        self.assertTrue(should_review("PARTIAL", [], "causal"))

    def test_stale_flag_queues(self) -> None:
        self.assertTrue(should_review("INSUFFICIENT", ["STALE"], "scope"))

    def test_non_core_insufficient_does_not_queue(self) -> None:
        self.assertFalse(should_review("INSUFFICIENT", [], "causal"))

    def test_core_insufficient_queues(self) -> None:
        self.assertTrue(should_review("INSUFFICIENT", [], "numeric"))

    def test_supported_without_flags_does_not_queue(self) -> None:
        self.assertFalse(should_review("SUPPORTED", [], "numeric"))


class ParsingTests(unittest.TestCase):
    def test_parses_plain_json(self) -> None:
        self.assertEqual(parse_json_object('{"claims": []}'), {"claims": []})

    def test_parses_fenced_json(self) -> None:
        text = '这里是结果：\n```json\n{"claims": [{"text": "a"}]}\n```\n'
        self.assertEqual(parse_json_object(text)["claims"][0]["text"], "a")

    def test_raises_on_garbage(self) -> None:
        with self.assertRaises(ValueError):
            parse_json_object("模型今天不想输出 JSON")

    def test_accepts_top_level_array(self) -> None:
        payload = parse_json_object('[{"text": "续航 40 小时", "verdict": "SUPPORTED"}]')
        self.assertEqual(payload["claims"][0]["text"], "续航 40 小时")

    def test_top_level_array_is_recorded_as_violation(self) -> None:
        claims, violations = extract_claims(
            [{"text": "续航 40 小时", "verdict": "SUPPORTED"}]
        )
        self.assertEqual(len(claims), 1)
        self.assertTrue(any("数组" in item for item in violations))

    def test_normalizes_chinese_keys_and_values(self) -> None:
        claims, violations = extract_claims(
            {
                "claims": [
                    {"主张": "续航 40 小时", "判定": "支持", "标志": ["条件省略"]}
                ]
            }
        )
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].text, "续航 40 小时")
        self.assertEqual(claims[0].verdict, "SUPPORTED")
        self.assertEqual(claims[0].flags, ("OMITTED_CONDITION",))
        self.assertEqual(violations, [])

    def test_alternate_container_key_is_a_violation(self) -> None:
        claims, violations = extract_claims(
            {"claim_list": [{"text": "续航 40 小时", "verdict": "SUPPORTED"}]}
        )
        self.assertEqual(len(claims), 1)
        self.assertTrue(any("claim_list" in item for item in violations))

    def test_parse_single_claim_handles_array_wrapper(self) -> None:
        claim, violations = parse_single_claim(
            '[{"verdict": "PARTIAL", "flags": ["OMITTED_CONDITION"]}]'
        )
        self.assertEqual(claim.verdict, "PARTIAL")
        self.assertEqual(claim.flags, ("OMITTED_CONDITION",))
        self.assertTrue(any("数组" in item for item in violations))

    def test_missing_claims_key_reports_actual_fields(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            extract_claims({"result": "no claims here", "note": "hi"})
        self.assertIn("result", str(ctx.exception))


class MetricsTests(unittest.TestCase):
    def _case(self):
        _, cases = load_cases(ROOT / "data" / "eval_tasks.json")
        return {c.id: c for c in cases}

    def test_certain_answer_on_insufficient_is_counted(self) -> None:
        case = self._case()["EV-013"]
        result = CaseResult(
            case_id=case.id,
            category=case.category,
            difficulty=case.difficulty,
            predicted_claims=[
                PredictedClaim(
                    text=case.expected_claims[0].text,
                    verdict="SUPPORTED",
                    evidence=("S1",),
                    claim_type="health_safety",
                )
            ],
        )
        summary = aggregate([case], [result])
        self.assertEqual(summary["unsupported_certainty_rate"], 1.0)
        self.assertEqual(summary["unsupported_certain_count"], 1)
        self.assertGreaterEqual(summary["p0_errors"], 1)

    def test_missing_prediction_counts_as_failure(self) -> None:
        case = self._case()["EV-013"]
        result = CaseResult(
            case_id=case.id,
            category=case.category,
            difficulty=case.difficulty,
            predicted_claims=[],
            parse_error="抽取阶段没有产出任何主张",
        )
        summary = aggregate([case], [result])
        # 没抽出主张是召回问题，不应算进"给出确定结论"这个指标
        self.assertIsNone(summary["unsupported_certainty_rate"])
        self.assertEqual(summary["insufficient_missed"], 1)
        self.assertEqual(summary["insufficient_answered"], 0)

    def test_insufficient_answer_is_not_counted_as_certain(self) -> None:
        case = self._case()["EV-013"]
        result = CaseResult(
            case_id=case.id,
            category=case.category,
            difficulty=case.difficulty,
            predicted_claims=[
                PredictedClaim(
                    text=case.expected_claims[0].text,
                    verdict="INSUFFICIENT",
                    flags=("LOW_SOURCE_QUALITY",),
                    claim_type="health_safety",
                )
            ],
        )
        summary = aggregate([case], [result])
        self.assertEqual(summary["unsupported_certainty_rate"], 0.0)
        self.assertEqual(summary["verdict_accuracy"], 1.0)

    def test_missing_verdict_field_is_not_an_answer(self) -> None:
        """模型没给 verdict：退回 INSUFFICIENT，但不算答对，也不进核心指标分母。

        否则 schema 崩坏反而会拿到最安全的答案，指标看起来最好。
        """

        case = self._case()["EV-013"]
        claim = PredictedClaim.from_dict(
            {"text": case.expected_claims[0].text, "claim_type": "health_safety"}
        )
        self.assertTrue(claim.verdict_inferred)

        result = CaseResult(
            case_id=case.id,
            category=case.category,
            difficulty=case.difficulty,
            predicted_claims=[claim],
        )
        summary = aggregate([case], [result])
        self.assertEqual(summary["verdict_missing_count"], 1)
        self.assertEqual(summary["insufficient_unanswered"], 1)
        self.assertEqual(summary["insufficient_answered"], 0)
        self.assertIsNone(summary["unsupported_certainty_rate"])
        self.assertEqual(summary["verdict_accuracy"], 0.0)

    def test_queue_mismatch_is_attributed_to_its_cause(self) -> None:
        """复核队列一致率不是独立指标：不一致时要能看出成因。"""

        case = self._case()["EV-013"]
        result = CaseResult(
            case_id=case.id,
            category=case.category,
            difficulty=case.difficulty,
            predicted_claims=[
                PredictedClaim(
                    text=case.expected_claims[0].text,
                    verdict="SUPPORTED",
                    claim_type="causal",
                )
            ],
        )
        summary = aggregate([case], [result])
        self.assertEqual(summary["queue_mismatch_breakdown"], {"判定不同": 1})


class DeepSeekAdapterTests(unittest.TestCase):
    """Adapter behaviour and local secret loading, without hitting the real API."""

    def test_env_file_loads_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.local"
            path.write_text(
                '# 注释行\nEVIDENCE_AGENT_TEST_A="value-a"\nexport EVIDENCE_AGENT_TEST_B=value-b\n',
                encoding="utf-8",
            )
            os.environ.pop("EVIDENCE_AGENT_TEST_A", None)
            os.environ.pop("EVIDENCE_AGENT_TEST_B", None)

            loaded = load_env_file(path)

            self.assertIn("EVIDENCE_AGENT_TEST_A", loaded)
            self.assertEqual(os.environ["EVIDENCE_AGENT_TEST_A"], "value-a")
            self.assertEqual(os.environ["EVIDENCE_AGENT_TEST_B"], "value-b")

    def test_env_file_does_not_override_existing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.local"
            path.write_text("EVIDENCE_AGENT_TEST_A=from-file\n", encoding="utf-8")
            os.environ["EVIDENCE_AGENT_TEST_A"] = "from-shell"

            load_env_file(path)

            self.assertEqual(os.environ["EVIDENCE_AGENT_TEST_A"], "from-shell")
        os.environ.pop("EVIDENCE_AGENT_TEST_A", None)

    def _adapter(self, **kwargs):
        os.environ["EVIDENCE_AGENT_TEST_KEY"] = "test-key"
        return DeepSeekAdapter(
            "deepseek-flash",
            api_key_env="EVIDENCE_AGENT_TEST_KEY",
            max_retries=1,
            backoff_s=0.0,
            sleep=lambda seconds: None,
            **kwargs,
        )

    def test_empty_content_is_retried_and_diagnosed(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "x" * 40},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1200},
            }
        ).encode("utf-8")

        calls: list = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self):
                return body

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            return FakeResponse()

        adapter = self._adapter()
        with patch("urllib.request.urlopen", fake_urlopen):
            response = adapter.complete([{"role": "user", "content": "hi"}])

        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "EMPTY_RESPONSE")
        self.assertIn("finish_reason=length", response.error)
        self.assertIn("reasoning_content 长度=40", response.error)
        self.assertIsNotNone(response.raw_body)
        self.assertIn("finish_reason", response.raw_body)
        self.assertEqual(len(calls), 2, "空内容应重试一次")

    def test_raising_max_tokens_shows_up_in_the_error(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {"message": {"content": ""}, "finish_reason": "length"}
                ]
            }
        ).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self):
                return body

        adapter = self._adapter(max_tokens=3000)
        with patch("urllib.request.urlopen", lambda request, timeout=None: FakeResponse()):
            response = adapter.complete([{"role": "user", "content": "hi"}])

        self.assertIn("max_tokens=3000", response.error)

    def test_missing_api_key_raises_config_error(self) -> None:
        os.environ.pop("EVIDENCE_AGENT_MISSING_KEY", None)
        with self.assertRaises(AdapterConfigError):
            DeepSeekAdapter(
                "deepseek-flash", api_key_env="EVIDENCE_AGENT_MISSING_KEY"
            )


class StubPipelineTests(unittest.TestCase):
    def test_v2_pipeline_produces_predictions_offline(self) -> None:
        _, cases = load_cases(ROOT / "data" / "eval_tasks.json")
        case = next(c for c in cases if c.id == "EV-007")
        adapter = StubAdapter(model_id="stub-offline")
        result = V2Pipeline().run(case, adapter)
        self.assertIsNone(result.error)
        self.assertTrue(result.predicted_claims)
        self.assertEqual(len(result.calls), 1 + len(result.predicted_claims))

    def test_stub_is_naive_and_overconfident(self) -> None:
        _, cases = load_cases(ROOT / "data" / "eval_tasks.json")
        by_id = {c.id: c for c in cases}
        selected = [by_id["EV-013"], by_id["EV-007"], by_id["EV-001"]]
        adapter = StubAdapter(model_id="stub-offline")
        pipeline = V2Pipeline()
        results = [pipeline.run(case, adapter) for case in selected]
        summary = aggregate(selected, results)
        rate = summary["unsupported_certainty_rate"]
        self.assertIsNotNone(rate)
        self.assertGreater(rate, 0.0, "朴素基线不应在证据不足案例上表现完美")


if __name__ == "__main__":
    unittest.main()

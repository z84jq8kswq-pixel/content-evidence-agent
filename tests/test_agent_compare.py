"""Unit tests for the v3 (agent) run comparison."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.agent_compare import (  # noqa: E402
    claim_changes,
    compare_agent_runs,
    metric_rows,
)


def claim(text, verdict="SUPPORTED", state="JUDGED", flags=(), tier="release"):
    return {
        "claim": text,
        "state": state,
        "verdict": verdict,
        "flags": list(flags),
        "disposition": {"tier": tier},
    }


def payload(claims, case_id="GAP-001", run_id="run"):
    return {
        "run_id": run_id,
        "metrics": {
            "claim_recall": 1.0,
            "claim_precision": 0.5,
            "routing_accuracy": 1.0,
            "missing_claims": [],
            "escalation_rate": 0.5,
        },
        "cases": [{"case_id": case_id, "claims": claims}],
    }


class MetricRowTests(unittest.TestCase):
    def test_higher_lower_and_neutral_directions(self) -> None:
        rows = {
            row["key"]: row
            for row in metric_rows(
                {
                    "claim_recall": 1.0,
                    "claim_precision": 0.8,
                    "escalation_rate": 0.5,
                    "missing_claims": [],
                },
                {
                    "claim_recall": 1.0,
                    "claim_precision": 0.5,
                    "escalation_rate": 0.25,
                    "missing_claims": ["某条主张"],
                },
            )
        }
        self.assertEqual(rows["claim_recall"]["verdict"], "持平")
        self.assertEqual(rows["claim_precision"]["verdict"], "变差")
        # 转人工率是取舍不是好坏，不判改善／变差
        self.assertEqual(rows["escalation_rate"]["verdict"], "口径中立")
        # 列表型指标按条数比
        self.assertEqual(rows["missing_claims"]["base"], 0)
        self.assertEqual(rows["missing_claims"]["new"], 1)
        # 没有数据的指标不能瞎判方向
        self.assertEqual(rows["cost_yuan"]["verdict"], "n/a")

    def test_cost_and_calls_come_from_run_meta(self) -> None:
        rows = {
            row["key"]: row
            for row in metric_rows(
                {},
                {},
                {"cost_yuan": 0.0, "model_calls": 0},
                {"cost_yuan": 0.011, "model_calls": 13},
            )
        }
        self.assertEqual(rows["model_calls"]["new"], 13)
        self.assertEqual(rows["cost_yuan"]["verdict"], "变差")


class ClaimChangeTests(unittest.TestCase):
    def test_verdict_and_tier_change_is_reported(self) -> None:
        base = payload([claim("云石 P20 有电池过热隐患", verdict="INSUFFICIENT", state="ESCALATED", tier="manual")])
        new = payload([claim("云石 P20 有电池过热隐患", verdict="SUPPORTED", state="JUDGED", tier="release")])

        changes = claim_changes(base, new)
        self.assertEqual(len(changes), 1)
        note = changes[0]["note"]
        for token in ("状态", "判定", "档位"):
            self.assertIn(token, note)
        self.assertEqual(changes[0]["base_tier"], "manual")
        self.assertEqual(changes[0]["new_tier"], "release")

    def test_unchanged_claim_is_not_reported(self) -> None:
        base = payload([claim("云石 P20 容量 20000mAh")])
        self.assertEqual(claim_changes(base, payload([claim("云石 P20 容量 20000mAh")])), [])

    def test_one_sided_claims_are_reported(self) -> None:
        base = payload([claim("甲产品续航 60 天"), claim("乙产品容量 20000mAh")])
        new = payload([claim("甲产品续航 60 天"), claim("丙产品快充 65W")])

        notes = {c["claim"]: c["note"] for c in claim_changes(base, new)}
        self.assertIn("乙产品容量 20000mAh", notes)
        self.assertIn("对照那次没有拆出这条主张", notes["乙产品容量 20000mAh"])
        self.assertIn("丙产品快充 65W", notes)
        self.assertIn("基准那次没有拆出这条主张", notes["丙产品快充 65W"])


class CompareAgentRunsTests(unittest.TestCase):
    def test_counts_and_identical_config_warning(self) -> None:
        base = payload([claim("云石 P20 容量 20000mAh")])
        meta = {"run_id": "r", "adapter": "rule-only", "extractor": "rule"}

        result = compare_agent_runs(base, meta, payload([claim("云石 P20 容量 20000mAh")]), meta)
        self.assertEqual(result["family"], "agent")
        self.assertEqual(result["counts"], {"cases": 1, "changed_claims": 0})
        self.assertTrue(any("适配器与抽取器都一样" in w for w in result["warnings"]))

    def test_case_set_mismatch_warns_and_counts_zero(self) -> None:
        result = compare_agent_runs(
            payload([claim("甲产品续航 60 天")], case_id="GAP-001"),
            {},
            payload([claim("甲产品续航 60 天")], case_id="GAP-002"),
            {},
        )
        self.assertTrue(any("不是同一批" in w for w in result["warnings"]))
        self.assertEqual(result["counts"]["cases"], 0)

    def test_real_runs_show_the_gap004_change(self) -> None:
        """真实产物上的锁：GAP-004 从@Q@规则判不了@Q@变成模型判定。"""

        def load(run_id):
            base = ROOT / "runs" / run_id
            payload_data = json.loads((base / "results.json").read_text(encoding="utf-8"))
            meta_data = json.loads((base / "run_meta.json").read_text(encoding="utf-8"))
            return payload_data, meta_data

        base_payload, base_meta = load("gap-no-oracle")
        new_payload, new_meta = load("gap-model-judge")
        result = compare_agent_runs(base_payload, base_meta, new_payload, new_meta)

        changed = [c for c in result["changes"] if c["case_id"] == "GAP-004"]
        self.assertEqual(len(changed), 1)
        self.assertIn("LOW_SOURCE_QUALITY", changed[0]["new_flags"])
        rows = {row["key"]: row for row in result["rows"]}
        self.assertEqual(rows["cost_yuan"]["new"], new_meta["cost_yuan"])


if __name__ == "__main__":
    unittest.main()

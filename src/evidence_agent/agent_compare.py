"""v3（自主检索版）运行的对比：指标 delta + 主张级变化。

和 v1／v2 的 "compare.py" 是两套东西，别混：

- v1／v2 的结果里每条主张都能对上标注（"expected_claims"），所以那边是"跟标注比"；
- v3 的结果里没有标注（缺口任务只用来打分），所以这里只能"两次运行互相比"——
  同一条主张在两次运行里的状态／判定／标志位／档位有没有变。

主张靠文本相似度配对（和评测里同一套口径），所以草稿措辞变化不会变成"全变了"。
"""

from __future__ import annotations

from typing import Any, Mapping

from .matching import match_claims

#: (指标键, 显示名, 方向)。方向只标"越高越好"或"越低越好"；
#: 口径中立的（计数、转人工率、调用次数）标 neutral——它们不是好坏，是取舍。
AGENT_METRICS: tuple[tuple[str, str, str], ...] = (
    ("claim_recall", "主张召回率", "higher"),
    ("claim_precision", "主张精确率", "higher"),
    ("routing_accuracy", "分诊一致率", "higher"),
    ("state_accuracy", "状态一致率（三态）", "higher"),
    ("verdict_accuracy", "判定准确率", "higher"),
    ("tool_selection_accuracy", "工具选择准确率", "higher"),
    ("reason_match_rate", "理由命中率", "higher"),
    ("tier_accuracy", "档位准确率", "higher"),
    ("evidence_hit_rate", "关键证据进结论率", "higher"),
    ("flag_recall", "标志位召回", "higher"),
    ("flag_precision", "标志位查准", "higher"),
    ("model_calls", "模型调用次数", "lower"),
    ("cost_yuan", "估算成本(元)", "lower"),
    ("missing_flag_count", "漏标标志位", "lower"),
    ("spurious_flag_count", "误报标志位", "lower"),
    ("missing_claims", "漏拆主张", "neutral"),
    ("spurious_claim_count", "多拆主张", "neutral"),
    ("escalation_rate", "转人工率", "neutral"),
    ("avg_tool_calls_per_claim", "平均工具调用次数", "neutral"),
    ("retry_rate", "重试率", "neutral"),
)


def _metric_value(summary: Mapping[str, Any], key: str) -> Any:
    value = summary.get(key)
    if isinstance(value, list):
        return len(value)
    return value


def metric_rows(
    base_metrics: Mapping[str, Any],
    new_metrics: Mapping[str, Any],
    base_meta: Mapping[str, Any] | None = None,
    new_meta: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """逐项列出指标变化。口径中立的只显示数值，不判"改善／变差"。"""

    base_meta = base_meta or {}
    new_meta = new_meta or {}
    rows: list[dict[str, Any]] = []
    for key, name, direction in AGENT_METRICS:
        if key in ("model_calls", "cost_yuan"):
            base_value = base_meta.get(key)
            new_value = new_meta.get(key)
        else:
            base_value = _metric_value(base_metrics, key)
            new_value = _metric_value(new_metrics, key)
        if isinstance(base_value, (int, float)) and isinstance(new_value, (int, float)):
            delta = round(new_value - base_value, 4)
            if direction == "neutral":
                verdict = "口径中立"
            elif abs(delta) < 1e-9:
                verdict = "持平"
            elif (delta > 0) == (direction == "higher"):
                verdict = "改善"
            else:
                verdict = "变差"
        else:
            delta, verdict = None, "n/a"
        rows.append(
            {
                "key": key,
                "name": name,
                "base": base_value,
                "new": new_value,
                "delta": delta,
                "direction": direction,
                "verdict": verdict,
            }
        )
    return rows


def claim_changes(
    base_payload: Mapping[str, Any],
    new_payload: Mapping[str, Any],
    threshold: float = 0.3,
) -> list[dict[str, Any]]:
    """主张级变化：同一条主张在两次运行里的状态／判定／标志位／档位有没有变。"""

    new_cases = {str(case.get("case_id", "")): case for case in new_payload.get("cases", [])}
    changes: list[dict[str, Any]] = []

    for base_case in base_payload.get("cases", []):
        case_id = str(base_case.get("case_id", ""))
        new_case = new_cases.get(case_id)
        if new_case is None:
            continue
        base_claims = list(base_case.get("claims") or [])
        new_claims = list(new_case.get("claims") or [])
        pairs, unmatched_base, unmatched_new = match_claims(
            [str(c.get("claim", "")) for c in base_claims],
            [str(c.get("claim", "")) for c in new_claims],
            threshold=threshold,
        )
        for base_index, new_index, _similarity in pairs:
            before = base_claims[base_index]
            after = new_claims[new_index]
            note = _change_note(before, after)
            if not note:
                continue
            changes.append(_row(case_id, str(after.get("claim", "")), before, after, note))
        for base_index in unmatched_base:
            before = base_claims[base_index]
            changes.append(
                _row(case_id, str(before.get("claim", "")), before, None, "对照那次没有拆出这条主张")
            )
        for new_index in unmatched_new:
            after = new_claims[new_index]
            changes.append(
                _row(case_id, str(after.get("claim", "")), None, after, "基准那次没有拆出这条主张")
            )
    return changes


def _row(
    case_id: str,
    claim: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    note: str,
) -> dict[str, Any]:
    before = before or {}
    after = after or {}
    return {
        "case_id": case_id,
        "claim": claim,
        "base_state": str(before.get("state", "MISSING" if after else "")),
        "new_state": str(after.get("state", "MISSING" if before else "")),
        "base_verdict": str(before.get("verdict", "")),
        "new_verdict": str(after.get("verdict", "")),
        "base_flags": list(before.get("flags") or []),
        "new_flags": list(after.get("flags") or []),
        "base_tier": _tier_of(before),
        "new_tier": _tier_of(after),
        "note": note,
    }


def _tier_of(claim: Mapping[str, Any]) -> str:
    disposition = claim.get("disposition") or {}
    return str(disposition.get("tier", ""))


def _change_note(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """一句话说明这条主张哪里变了；没变返回空串。"""

    notes: list[str] = []
    if str(before.get("state", "")) != str(after.get("state", "")):
        notes.append("状态 {0} → {1}".format(before.get("state"), after.get("state")))
    if str(before.get("verdict", "")) != str(after.get("verdict", "")):
        notes.append(
            "判定 {0} → {1}".format(before.get("verdict"), after.get("verdict"))
        )
    if sorted(before.get("flags") or []) != sorted(after.get("flags") or []):
        notes.append(
            "标志 {0} → {1}".format(
                "/".join(before.get("flags") or []) or "无",
                "/".join(after.get("flags") or []) or "无",
            )
        )
    if _tier_of(before) != _tier_of(after):
        notes.append(
            "档位 {0} → {1}".format(_tier_of(before) or "—", _tier_of(after) or "—")
        )
    return "；".join(notes)


def compare_agent_runs(
    base_payload: Mapping[str, Any],
    base_meta: Mapping[str, Any],
    new_payload: Mapping[str, Any],
    new_meta: Mapping[str, Any],
    threshold: float = 0.3,
) -> dict[str, Any]:
    """两次 v3 运行的完整对比：指标 delta + 主张级变化 + 可比性提示。"""

    base_meta = dict(base_meta or {})
    new_meta = dict(new_meta or {})
    if not base_meta.get("run_id"):
        base_meta["run_id"] = str(base_payload.get("run_id", ""))
    if not new_meta.get("run_id"):
        new_meta["run_id"] = str(new_payload.get("run_id", ""))

    changes = claim_changes(base_payload, new_payload, threshold=threshold)
    warnings: list[str] = []

    base_cases = [str(c.get("case_id", "")) for c in base_payload.get("cases", [])]
    new_cases = [str(c.get("case_id", "")) for c in new_payload.get("cases", [])]
    if set(base_cases) != set(new_cases):
        warnings.append(
            "两次运行的内容条不是同一批（基准多 {0}，对照多 {1}），指标不能直接比".format(
                "/".join(sorted(set(base_cases) - set(new_cases)) or ["无"]),
                "/".join(sorted(set(new_cases) - set(base_cases)) or ["无"]),
            )
        )
    if base_meta.get("gap_tasks_version") != new_meta.get("gap_tasks_version"):
        warnings.append("两次运行用的任务集版本不同，指标不能直接比")
    if base_meta.get("adapter") == new_meta.get("adapter") and base_meta.get(
        "extractor"
    ) == new_meta.get("extractor"):
        warnings.append("两次运行的适配器与抽取器都一样，差异只可能来自模型随机性")

    return {
        "family": "agent",
        "base": {
            "run": base_meta.get("run_id", ""),
            "adapter": base_meta.get("adapter", ""),
            "extractor": base_meta.get("extractor", ""),
            "created_at": base_meta.get("created_at", ""),
        },
        "new": {
            "run": new_meta.get("run_id", ""),
            "adapter": new_meta.get("adapter", ""),
            "extractor": new_meta.get("extractor", ""),
            "created_at": new_meta.get("created_at", ""),
        },
        "warnings": warnings,
        "rows": metric_rows(
            base_payload.get("metrics") or {},
            new_payload.get("metrics") or {},
            base_meta,
            new_meta,
        ),
        "changes": changes,
        "counts": {
            "cases": len(set(base_cases) & set(new_cases)),
            "changed_claims": len(changes),
        },
    }

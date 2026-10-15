"""Agent pipelines under comparison.

v1: one model call does extraction and judgment together.
v2: extraction, then one judgment call per claim.

Both share the same output contract so the harness scores them identically.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Sequence

from .adapters.base import ModelAdapter
from .models import CallRecord, CaseResult, PredictedClaim, TaskCase
from .prompts import extract_messages, judge_messages, v1_messages

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

# The model does not always follow the exact contract. These maps let the
# harness recover a usable answer while still recording that the output broke
# the schema, so format compliance stays measurable as its own metric.
CLAIM_LIST_KEYS = (
    "claims",
    "claim_list",
    "claimList",
    "results",
    "items",
    "data",
    "list",
    "主张",
    "主张列表",
    "核查结果",
)

TEXT_KEYS = ("text", "claim", "说法", "主张", "内容")
VERDICT_KEYS = ("verdict", "判定", "结论", "判断")

KEY_ALIASES = {
    "说法": "text",
    "主张": "text",
    "内容": "text",
    "判定": "verdict",
    "结论": "verdict",
    "判断": "verdict",
    "标志": "flags",
    "标志位": "flags",
    "标记": "flags",
    "证据": "evidence",
    "来源": "evidence",
    "引用": "evidence",
    "依据": "reasoning",
    "理由": "reasoning",
    "说明": "reasoning",
    "类型": "claim_type",
    "主张类型": "claim_type",
}

VERDICT_ALIASES = {
    "支持": "SUPPORTED",
    "有支持": "SUPPORTED",
    "部分支持": "PARTIAL",
    "部分": "PARTIAL",
    "冲突": "CONTRADICTED",
    "矛盾": "CONTRADICTED",
    "不支持": "CONTRADICTED",
    "证据不足": "INSUFFICIENT",
    "无法判断": "INSUFFICIENT",
    "不确定": "INSUFFICIENT",
    "待核查": "INSUFFICIENT",
}

FLAG_ALIASES = {
    "资料过期": "STALE",
    "过期": "STALE",
    "时效": "STALE",
    "资料冲突": "SOURCE_CONFLICT",
    "来源冲突": "SOURCE_CONFLICT",
    "互相矛盾": "SOURCE_CONFLICT",
    "范围错配": "SCOPE_MISMATCH",
    "范围不符": "SCOPE_MISMATCH",
    "型号不符": "SCOPE_MISMATCH",
    "条件省略": "OMITTED_CONDITION",
    "遗漏条件": "OMITTED_CONDITION",
    "单位不一致": "UNIT_MISMATCH",
    "口径不一致": "UNIT_MISMATCH",
    "营销来源": "LOW_SOURCE_QUALITY",
    "来源质量低": "LOW_SOURCE_QUALITY",
}

CLAIM_TYPE_ALIASES = {
    "数值": "numeric",
    "数值型": "numeric",
    "范围": "scope",
    "范围型": "scope",
    "排他": "exclusive",
    "排他型": "exclusive",
    "唯一": "exclusive",
    "认证": "certification",
    "认证型": "certification",
    "健康安全": "health_safety",
    "健康": "health_safety",
    "因果": "causal",
    "因果型": "causal",
    "时间": "timeline",
    "时间型": "timeline",
}


def _slice_json(text: str) -> str | None:
    starts = [index for index in (text.find("{"), text.find("[")) if index != -1]
    if not starts:
        return None
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        return None
    return text[start : end + 1]


def parse_json_any(text: str):
    """Parse the first JSON value in a model reply, object or array."""

    if not text or not text.strip():
        raise ValueError("模型返回为空")
    candidate = text.strip()
    fence = FENCE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()

    attempts = [candidate]
    sliced = _slice_json(candidate)
    if sliced and sliced != candidate:
        attempts.append(sliced)

    for attempt in attempts:
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    raise ValueError("返回内容不是合法 JSON")


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from a model reply, tolerating code fences."""

    payload = parse_json_any(text)
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"claims": payload}
    raise ValueError(f"返回 JSON 顶层类型不支持：{type(payload).__name__}")


def normalize_claim_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Map alias keys and Chinese enum values onto the canonical schema."""

    out = dict(raw)
    for alias, canonical in KEY_ALIASES.items():
        if canonical not in out and alias in out:
            out[canonical] = out[alias]

    verdict = out.get("verdict")
    if isinstance(verdict, str):
        text = verdict.strip()
        out["verdict"] = VERDICT_ALIASES.get(text, text.upper())

    flags = out.get("flags")
    if isinstance(flags, str):
        flags = [flags]
    if isinstance(flags, list):
        out["flags"] = [
            FLAG_ALIASES.get(str(item).strip(), str(item).strip()) for item in flags
        ]

    claim_type = out.get("claim_type")
    if isinstance(claim_type, str):
        key = claim_type.strip()
        out["claim_type"] = CLAIM_TYPE_ALIASES.get(key, key.lower())

    evidence = out.get("evidence")
    if isinstance(evidence, str):
        out["evidence"] = [evidence]

    return out


def extract_claims(
    payload: Any, *, require_verdict: bool = False
) -> tuple[list[PredictedClaim], list[str]]:
    """Pull a claim list out of whatever shape the model returned.

    Returns (claims, violations). Violations are recorded rather than raised so
    a schema problem is reported as a format failure instead of being lost.
    """

    violations: list[str] = []
    if isinstance(payload, list):
        items = payload
        violations.append("顶层直接输出数组，未使用 claims 字段")
    elif isinstance(payload, dict):
        items = None
        for key in CLAIM_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                if key != "claims":
                    violations.append(f"使用了非标准字段 {key}")
                break
        if items is None:
            if any(key in payload for key in TEXT_KEYS):
                items = [payload]
                violations.append("顶层是单条主张对象，未使用 claims 数组")
            else:
                keys = sorted(str(k) for k in payload)[:8]
                raise ValueError(f"返回 JSON 中找不到主张数组，实际字段：{keys}")
    else:
        raise ValueError(f"返回 JSON 顶层类型不支持：{type(payload).__name__}")

    claims: list[PredictedClaim] = []
    for item in items:
        if not isinstance(item, dict):
            violations.append("主张数组中出现非对象元素，已跳过")
            continue
        if not any(key in item for key in TEXT_KEYS):
            violations.append("存在缺少 text 字段的主张")
        if require_verdict and not any(key in item for key in VERDICT_KEYS):
            violations.append("存在缺少 verdict 字段的主张")
        claim = PredictedClaim.from_dict(normalize_claim_dict(item))
        if claim.text:
            claims.append(claim)
    return claims, violations


def parse_single_claim(text: str) -> tuple[PredictedClaim, list[str]]:
    """Parse one judged claim, tolerating a single-element array wrapper."""

    payload = parse_json_any(text)
    violations: list[str] = []
    if isinstance(payload, list):
        if not payload:
            raise ValueError("判定结果为空数组")
        payload = payload[0]
        violations.append("判定阶段顶层输出数组")
    if not isinstance(payload, dict):
        raise ValueError(f"判定结果不是对象：{type(payload).__name__}")
    if not any(key in payload for key in VERDICT_KEYS):
        violations.append("判定结果缺少 verdict 字段")
    claim = PredictedClaim.from_dict(normalize_claim_dict(payload))
    return claim, violations


def parse_claims(payload: dict[str, Any]) -> list[PredictedClaim]:
    """Backwards-compatible wrapper returning only the parsed claims."""

    claims, _ = extract_claims(payload)
    return claims


class Pipeline(ABC):
    name = "base"

    def __init__(
        self,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        step_settings: dict[str, dict] | None = None,
    ):
        self.temperature = temperature
        self.max_tokens = max_tokens
        # Per-node model settings, e.g. {"extract": {"thinking": "disabled"}}.
        # Extraction is mechanical; judgment needs reasoning. Giving them the
        # same budget wastes tokens and time on the easy step.
        self.step_settings = step_settings or {}

    @abstractmethod
    def run(self, case: TaskCase, adapter: ModelAdapter) -> CaseResult:
        raise NotImplementedError

    def _call(
        self,
        result: CaseResult,
        adapter: ModelAdapter,
        step: str,
        messages: Sequence[dict[str, str]],
        overrides: dict | None = None,
    ):
        overrides = overrides or {}
        response = adapter.complete(
            messages,
            temperature=overrides.get("temperature", self.temperature),
            max_tokens=overrides.get("max_tokens", self.max_tokens),
            thinking=overrides.get("thinking"),
            reasoning_effort=overrides.get("reasoning_effort"),
        )
        result.calls.append(
            CallRecord(step=step, response=response, request_messages=list(messages))
        )
        return response


class V1Pipeline(Pipeline):
    """Single-shot: one call returns claims with verdicts."""

    name = "v1"

    def run(self, case: TaskCase, adapter: ModelAdapter) -> CaseResult:
        started = time.perf_counter()
        result = CaseResult(case_id=case.id, category=case.category, difficulty=case.difficulty)
        response = self._call(
            result,
            adapter,
            "judge_all",
            v1_messages(case.content, case.sources),
            self.step_settings.get("all"),
        )
        if not response.ok:
            result.error = f"{response.error_code}: {response.error}"
        else:
            try:
                payload = parse_json_any(response.text)
                claims, violations = extract_claims(payload, require_verdict=True)
                result.predicted_claims = claims
                if violations:
                    result.format_violation = "；".join(violations)
            except (ValueError, json.JSONDecodeError) as exc:
                result.parse_error = str(exc)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result


class V2Pipeline(Pipeline):
    """Two-stage: extract claims, then judge each claim separately."""

    name = "v2"

    def __init__(self, *, max_claims: int = 5, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_claims = max_claims

    def run(self, case: TaskCase, adapter: ModelAdapter) -> CaseResult:
        started = time.perf_counter()
        result = CaseResult(case_id=case.id, category=case.category, difficulty=case.difficulty)

        extract_response = self._call(
            result,
            adapter,
            "extract_claims",
            extract_messages(case.content, case.sources),
            self.step_settings.get("extract"),
        )
        if not extract_response.ok:
            result.error = f"{extract_response.error_code}: {extract_response.error}"
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result

        try:
            payload = parse_json_any(extract_response.text)
            draft_claims, violations = extract_claims(payload, require_verdict=False)
            if violations:
                result.format_violation = "；".join(violations)
        except (ValueError, json.JSONDecodeError) as exc:
            result.parse_error = f"抽取阶段：{exc}"
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result

        if not draft_claims:
            result.parse_error = "抽取阶段没有产出任何主张"
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result

        # Stage 2 simulates the human confirmation step: the claim list is
        # locked before evidence judgment starts. Auto-accept in the harness.
        for draft in draft_claims[: self.max_claims]:
            judge_response = self._call(
                result,
                adapter,
                f"judge_claim:{draft.text[:12]}",
                judge_messages(case.content, draft.text, draft.claim_type, case.sources),
                self.step_settings.get("judge"),
            )
            if not judge_response.ok:
                result.predicted_claims.append(
                    PredictedClaim(
                        text=draft.text,
                        verdict="INSUFFICIENT",
                        claim_type=draft.claim_type,
                        reasoning=f"判定调用失败：{judge_response.error_code}",
                    )
                )
                continue
            try:
                claim, judge_violations = parse_single_claim(judge_response.text)
            except (ValueError, json.JSONDecodeError):
                result.predicted_claims.append(
                    PredictedClaim(
                        text=draft.text,
                        verdict="INSUFFICIENT",
                        claim_type=draft.claim_type,
                        reasoning="判定结果无法解析，退回证据不足",
                    )
                )
                continue
            if judge_violations:
                joined = "；".join(judge_violations)
                result.format_violation = (
                    f"{result.format_violation}；{joined}"
                    if result.format_violation
                    else joined
                )
            if not claim.text:
                claim.text = draft.text
            if claim.claim_type == "other" and draft.claim_type != "other":
                claim.claim_type = draft.claim_type
            result.predicted_claims.append(claim)

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result


PIPELINES: dict[str, type[Pipeline]] = {
    "v1": V1Pipeline,
    "v2": V2Pipeline,
}

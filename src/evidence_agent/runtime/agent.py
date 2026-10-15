"""自主检索型内容核查 Agent 的运行时。

状态机（PRD 5.1）：

    INIT → 对每条主张 [PLAN → ACT → OBSERVE → ASSESS]
             ├ 证据充分 → JUDGE（规则优先，规则判不了再叫模型）
             ├ 不充分且有策略 → 回到 PLAN
             └ 无策略／超预算／有阻断标志 → ESCALATE（转人工）
         → AGGREGATE（分诊结果 + Trace）

三种终止状态：JUDGED（有结论）、ESCALATED（交给人）、TOOL_FAILED（工具不可用，
不得给出结论）。后两种都要人处理，但原因不同，报告里必须分开。

预算是按单条主张计的：每条主张新建一个 CallBudget，互不影响。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..grading import independent_sources
from ..models import CallRecord, PredictedClaim, Source
from ..pipeline import parse_single_claim
from ..prompts import judge_messages
from ..tools.base import ToolResult
from ..tools.registry import CallBudget, ToolRegistry
from .assessor import (
    Assessment,
    assess,
    compare_numeric,
    compare_permission,
    has_strong_source,
)
from .disposition import Disposition, classify, distribution
from .planner import Plan, Planner, infer_claim_type, infer_metric
from .states import ESCALATED, JUDGED, TOOL_FAILED

__all__ = [
    "ESCALATED",
    "JUDGED",
    "TOOL_FAILED",
    "Attempt",
    "ClaimOutcome",
    "ContentOutcome",
    "EvidenceAgent",
    "default_budget",
    "make_agent",
]


@dataclass
class Attempt:
    plan: Plan
    result: ToolResult
    assessment: Assessment


@dataclass
class ClaimOutcome:
    claim_text: str
    claim_type: str
    state: str
    verdict: str
    reason: str
    flags: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    model_calls: list[CallRecord] = field(default_factory=list)
    disposition: Disposition | None = None
    #: 覆盖证据强度。只在"阈值判定吸收了来源冲突"时由 _decide 写入。
    evidence_override: str = ""
    #: 承载这个结论的来源编号。评测用它检查"关键证据是否真的在结论依据里"，
    #: 只检查"检索到过"会让证据命中率虚高。
    decided_by: list[str] = field(default_factory=list)

    @property
    def tool_calls(self) -> int:
        return len(self.attempts)

    @property
    def prompt_tokens(self) -> int:
        return sum(call.response.prompt_tokens for call in self.model_calls)

    @property
    def completion_tokens(self) -> int:
        return sum(call.response.completion_tokens for call in self.model_calls)

    @property
    def in_review_queue(self) -> bool:
        """要不要人过手。

        没有档位时退回到老口径（没结论就要人看）；有档位时按档位判——
        降级分发、打标、通知创作者都是自动动作，不进人工队列。
        """

        if self.disposition is not None:
            return self.disposition.needs_human
        return self.state != JUDGED

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim_text,
            "claim_type": self.claim_type,
            "state": self.state,
            "verdict": self.verdict,
            "flags": self.flags,
            "evidence": self.evidence,
            "reason": self.reason,
            "tool_calls": self.tool_calls,
            # 模型调用的代价要留痕：v1／v2 的 harness 一直有 token 统计，
            # 这里以前只有工具调用次数，接真实模型后就看不到成本了。
            "model_calls": [
                {
                    "step": call.step,
                    "ok": call.response.ok,
                    "latency_ms": call.response.latency_ms,
                    "prompt_tokens": call.response.prompt_tokens,
                    "completion_tokens": call.response.completion_tokens,
                    "error_code": call.response.error_code,
                }
                for call in self.model_calls
            ],
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "disposition": self.disposition.to_dict() if self.disposition else None,
            "evidence_override": self.evidence_override or None,
            "decided_by": self.decided_by,
            "steps": [
                {
                    "strategy": attempt.plan.strategy,
                    "tool": attempt.plan.tool,
                    "params": attempt.plan.params,
                    "selection_reason": attempt.plan.reason,
                    "status": attempt.result.status,
                    "latency_ms": attempt.result.latency_ms,
                    "degraded": attempt.result.degraded,
                    "digest": attempt.result.digest(),
                    "sources": attempt.result.source_ids(),
                }
                for attempt in self.attempts
            ],
        }


@dataclass
class ContentOutcome:
    content_id: str
    claims: list[ClaimOutcome] = field(default_factory=list)
    duration_ms: int = 0

    def summary(self) -> dict[str, Any]:
        judged = [c for c in self.claims if c.state == JUDGED]
        escalated = [c for c in self.claims if c.state == ESCALATED]
        failed = [c for c in self.claims if c.state == TOOL_FAILED]
        return {
            "content_id": self.content_id,
            "claims": len(self.claims),
            "judged": len(judged),
            "escalated": len(escalated),
            "tool_failed": len(failed),
            "tool_calls": sum(c.tool_calls for c in self.claims),
            "duration_ms": self.duration_ms,
            "tiers": distribution(
                [c.disposition for c in self.claims if c.disposition is not None]
            ),
        }


def _unique_source_ids(items) -> list[str]:
    """按来源编号去重，保持首次出现的顺序。"""

    out: list[str] = []
    for item in items:
        if item.source_id not in out:
            out.append(item.source_id)
    return out


def default_budget() -> CallBudget:
    return CallBudget(max_calls=4, max_seconds=30.0)


class EvidenceAgent:
    """把工具层、策略层与判定规则串成一次完整的分诊。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        planner: Planner | None = None,
        adapter: Any | None = None,
        #: 策略阶梯有三档（baseline → 换词／换工具 → 取原文定位），
        #: 轮数少于 3 时第三档永远跑不到；预算仍是每主张最多 4 次调用、30 秒。
        max_rounds: int = 3,
        budget_factory: Callable[[], CallBudget] = default_budget,
        step_settings: dict[str, dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        trace: Any | None = None,
        reference_facts: Sequence[Any] = (),
    ) -> None:
        self.registry = registry
        self.planner = planner or Planner()
        self.adapter = adapter
        self.max_rounds = max_rounds
        self.budget_factory = budget_factory
        self.step_settings = step_settings or {}
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.trace = trace
        #: 参考事实库（含法规阈值）。许可型主张要靠它比对限额。
        self.reference_facts = list(reference_facts)

    # --- 对外入口 -----------------------------------------------------

    def run_content(
        self,
        content_id: str,
        claims: Sequence[str],
        *,
        content: str = "",
        trace: Any | None = None,
    ) -> ContentOutcome:
        started = time.perf_counter()
        active_trace = trace or self.trace
        outcome = ContentOutcome(content_id=content_id)
        if active_trace:
            active_trace.log("agent_start", content_id=content_id, claims=len(claims))

        for text in claims:
            claim_type = infer_claim_type(text)
            claim_outcome = self.run_claim(content, text, claim_type, trace=active_trace)
            outcome.claims.append(claim_outcome)
            if active_trace:
                active_trace.log("claim_finished", content_id=content_id, **claim_outcome.to_dict())

        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        if active_trace:
            active_trace.log("agent_finished", **outcome.summary())
        return outcome

    def run_claim(
        self,
        content: str,
        claim_text: str,
        claim_type: str | None = None,
        *,
        trace: Any | None = None,
    ) -> ClaimOutcome:
        claim_type = claim_type or infer_claim_type(claim_text)
        # 主张涉及的指标。检索路径带回来的结构化事实要靠它才认得出来，否则同一份
        # 资料走检索会被当成"没有可比的数值"（见 assessor._records_of）。
        claim_metric = infer_metric(claim_text) or ""
        # 实体同样要传：否则别的产品的同指标记录会参与冲突判定（扩样时撞到过）
        claim_entity = self.planner.entity_of(claim_text, content) or ""
        budget = self.budget_factory()
        attempts: list[Attempt] = []
        evidence: list[Any] = []
        executed: set[tuple[str, str]] = set()
        assessment = Assessment(sufficient=False, reason="尚未开始检索")

        while len(attempts) < self.max_rounds:
            plans = [
                plan
                for plan in self.planner.strategies(
                    claim_text, claim_type, evidence, context=content
                )
                if plan.key() not in executed and self.registry.get(plan.tool) is not None
            ]
            if not plans:
                break
            plan = plans[0]
            executed.add(plan.key())
            if budget.exhausted():
                break

            if trace:
                trace.log(
                    "plan",
                    claim=claim_text,
                    strategy=plan.strategy,
                    tool=plan.tool,
                    params=plan.params,
                    selection_reason=plan.reason,
                )

            result = self.registry.call(
                plan.tool, plan.params, budget=budget, selection_reason=plan.reason
            )
            if result.items:
                evidence.extend(result.items)
            assessment = assess(
                evidence, claim_metric=claim_metric, claim_entity=claim_entity
            )
            attempts.append(Attempt(plan=plan, result=result, assessment=assessment))

            if trace:
                trace.log(
                    "assess",
                    claim=claim_text,
                    tool=plan.tool,
                    status=result.status,
                    sufficient=assessment.sufficient,
                    flags=assessment.flags,
                    reason=assessment.reason,
                )
            if assessment.sufficient or assessment.blocking:
                break

        outcome = self._decide(
            content, claim_text, claim_type, attempts, assessment, budget
        )
        outcome.disposition = classify(
            state=outcome.state,
            verdict=outcome.verdict,
            claim_type=claim_type,
            claim_text=claim_text,
            assessment=assessment,
            content=content,
            evidence_override=outcome.evidence_override or None,
        )
        return outcome

    # --- 判定 ---------------------------------------------------------

    def _decide(
        self,
        content: str,
        claim_text: str,
        claim_type: str,
        attempts: list[Attempt],
        assessment: Assessment,
        budget: CallBudget,
    ) -> ClaimOutcome:
        # 同一份资料可能贡献多条事实（容量与额定能量都来自 S-008），
        # 展示时要去重，否则界面上同一份材料会列两遍
        evidence_ids: list[str] = []
        for item in assessment.items:
            if item.source_id not in evidence_ids:
                evidence_ids.append(item.source_id)

        # 许可型主张先跑阈值比对。它**故意放在阻断标志之前**：阈值判定能吸收
        # 小幅来源冲突——74Wh 与 62Wh 虽然互相矛盾，但都在 100Wh 以内，结论
        # 不受影响。前提仍然是有 A/B 级来源，证据质量的门槛没有放松。
        permission = compare_permission(
            claim_text, assessment.items, self.reference_facts
        )
        if (
            permission is not None
            and permission[0] in ("SUPPORTED", "CONTRADICTED")
            and has_strong_source(assessment.items)
        ):
            verdict, reason, permission_sources = permission
            # 独立来源按发布者去重，与处置档位的 E 档走同一条规则：按资料编号数会把
            # 同一家机构的两份材料算成两个来源，直接抬高可对外动作的档位。
            independent = independent_sources(assessment.items)
            return ClaimOutcome(
                claim_text=claim_text,
                claim_type=claim_type,
                state=JUDGED,
                verdict=verdict,
                reason=reason,
                # 分歧已被阈值判定吸收，继续挂着会误导读者
                flags=[
                    f
                    for f in assessment.flags
                    if f not in ("SOURCE_CONFLICT", "NOT_COMPARABLE")
                ],
                evidence=evidence_ids,
                attempts=attempts,
                decided_by=list(permission_sources),
                evidence_override="E3" if len(independent) >= 2 else "E2",
            )

        if assessment.sufficient:
            numeric = compare_numeric(claim_text, assessment.items)
            if numeric:
                verdict, reason, numeric_sources = numeric
                return ClaimOutcome(
                    claim_text=claim_text,
                    claim_type=claim_type,
                    state=JUDGED,
                    verdict=verdict,
                    reason=reason,
                    flags=assessment.flags,
                    evidence=evidence_ids,
                    attempts=attempts,
                    decided_by=list(numeric_sources),
                )
            return self._judge_with_model(
                content, claim_text, claim_type, attempts, assessment, budget
            )

        if budget.exhausted() and not assessment.blocking:
            return self._escalate(
                claim_text, claim_type, attempts, assessment, "工具预算耗尽，转人工"
            )
        if assessment.blocking:
            return self._escalate(
                claim_text, claim_type, attempts, assessment, assessment.reason
            )
        if attempts and all(not attempt.result.ok for attempt in attempts):
            return ClaimOutcome(
                claim_text=claim_text,
                claim_type=claim_type,
                state=TOOL_FAILED,
                verdict="INSUFFICIENT",
                reason="工具连续失败且无法降级，本次核查不可用",
                flags=assessment.flags,
                evidence=evidence_ids,
                attempts=attempts,
            )
        return self._escalate(
            claim_text,
            claim_type,
            attempts,
            assessment,
            "检索策略已用尽仍无法判定，转人工",
        )

    def _escalate(
        self,
        claim_text: str,
        claim_type: str,
        attempts: list[Attempt],
        assessment: Assessment,
        reason: str,
    ) -> ClaimOutcome:
        return ClaimOutcome(
            claim_text=claim_text,
            claim_type=claim_type,
            state=ESCALATED,
            verdict="INSUFFICIENT",
            reason=reason,
            flags=assessment.flags,
            # 同一份材料可能由多次调用各带回一条（事实行／检索片段／原文段落），
            # 展示时要按来源去重，否则界面上同一份资料会列好几遍
            evidence=_unique_source_ids(assessment.items),
            attempts=attempts,
            decided_by=list(assessment.decisive_sources),
        )

    def _judge_with_model(
        self,
        content: str,
        claim_text: str,
        claim_type: str,
        attempts: list[Attempt],
        assessment: Assessment,
        budget: CallBudget,
    ) -> ClaimOutcome:
        """规则判不了的主张才叫模型——模型只在证据包上做判断。"""

        if self.adapter is None:
            return self._escalate(
                claim_text,
                claim_type,
                attempts,
                assessment,
                "证据充分但规则无法判定，需要模型或人工",
            )

        sources = [
            Source(id=item.source_id, tier=item.tier, date=item.date, text=item.text)
            for item in assessment.items
        ]
        messages = judge_messages(content, claim_text, claim_type, sources)
        overrides = self.step_settings.get("judge", {})
        response = self.adapter.complete(
            messages,
            temperature=overrides.get("temperature", self.temperature),
            max_tokens=overrides.get("max_tokens", self.max_tokens),
            thinking=overrides.get("thinking"),
            reasoning_effort=overrides.get("reasoning_effort"),
        )
        call = CallRecord(
            step=f"judge:{claim_text[:12]}", response=response, request_messages=messages
        )
        if not response.ok:
            outcome = self._escalate(
                claim_text,
                claim_type,
                attempts,
                assessment,
                f"判定调用失败（{response.error_code}），转人工",
            )
            outcome.model_calls.append(call)
            return outcome

        claim, _violations = parse_single_claim(response.text)
        outcome = ClaimOutcome(
            claim_text=claim_text,
            claim_type=claim_type or claim.claim_type,
            state=JUDGED,
            verdict=claim.verdict,
            reason=claim.reasoning or "由模型在证据包上判定",
            flags=list(claim.flags) or assessment.flags,
            evidence=list(claim.evidence) or _unique_source_ids(assessment.items),
            attempts=attempts,
            decided_by=list(claim.evidence) or list(assessment.decisive_sources),
        )
        outcome.model_calls.append(call)
        return outcome


def make_agent(
    corpus: Any,
    *,
    adapter: Any | None = None,
    trace: Any | None = None,
    **kwargs: Any,
) -> EvidenceAgent:
    """按语料装配一个可直接使用的 Agent。"""

    from ..tools.builtin import build_tools

    registry = ToolRegistry(build_tools(corpus))
    planner = Planner(resolve_entity=corpus.resolve_entity)
    kwargs.setdefault("reference_facts", corpus.enriched_facts())
    return EvidenceAgent(registry, planner=planner, adapter=adapter, trace=trace, **kwargs)

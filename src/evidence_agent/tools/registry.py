"""工具注册中心：参数校验、超时、重试、预算与调用记录。

这是 Tool Registry 的产品实体——每个工具的参数、超时、重试、降级与成本都
登记在案，调用过程全部留痕，供 Trace 与人工复核使用。

预算规则来自 docs/10-prd-agent-v3.md 第 5.4 节：单条主张最多 4 次工具调用、
最多 2 轮重试、工具耗时合计不超过 30 秒，超限即转人工。

注意：预算是**按单条主张**计的，所以 registry 默认不持有预算。若把一份预算当
成全局默认值，不同主张会互相吃掉额度，结果会依赖调用顺序——这类错误不会报错，
只会让结论悄悄变形。运行时必须为每条主张新建一个 CallBudget 并显式传入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .base import (
    RETRYABLE_STATUSES,
    STATUS_BAD_REQUEST,
    STATUS_BUDGET_EXCEEDED,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    STATUS_UPSTREAM_ERROR,
    Tool,
    ToolResult,
    ToolSpec,
    validate_params,
)


@dataclass
class CallBudget:
    """单条主张的工具预算。超限不再是失败，而是"该交给人了"。"""

    max_calls: int = 4
    max_seconds: float = 30.0
    calls: int = 0
    seconds: float = 0.0

    def exhausted(self) -> bool:
        return self.calls >= self.max_calls or self.seconds >= self.max_seconds

    def charge(self, latency_ms: int) -> None:
        self.calls += 1
        self.seconds += max(0, latency_ms) / 1000.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "seconds": round(self.seconds, 3),
            "max_calls": self.max_calls,
            "max_seconds": self.max_seconds,
            "exhausted": self.exhausted(),
        }


@dataclass
class ToolCallRecord:
    """一次工具调用的完整留痕，写入 trace.jsonl。"""

    tool: str
    params: dict[str, Any]
    status: str
    attempts: int
    latency_ms: int
    error: str | None = None
    degraded: bool = False
    degrade_reason: str | None = None
    source_ids: list[str] = field(default_factory=list)
    digest: str = ""
    selection_reason: str = ""
    budget: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "params": self.params,
            "status": self.status,
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "source_ids": self.source_ids,
            "digest": self.digest,
            "selection_reason": self.selection_reason,
            "budget": self.budget,
        }


class ToolRegistry:
    """注册、查找与调用工具，并保证重试与预算不被绕过。"""

    def __init__(
        self,
        tools: Iterable[Tool] = (),
        *,
        sleep=time.sleep,
        clock=time.perf_counter,
        budget: CallBudget | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._records: list[ToolCallRecord] = []
        self._sleep = sleep
        self._clock = clock
        # None 表示不设全局预算；预算是按主张的，由运行时逐条传入。
        self._budget = budget
        for tool in tools:
            self.register(tool)

    # --- 注册与查询 ---------------------------------------------------

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"工具重复注册：{name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def spec(self, name: str) -> ToolSpec | None:
        tool = self._tools.get(name)
        return tool.spec if tool else None

    @property
    def records(self) -> list[ToolCallRecord]:
        return list(self._records)

    @property
    def budget(self) -> CallBudget | None:
        return self._budget

    # --- 调用 ---------------------------------------------------------

    def call(
        self,
        name: str,
        params: dict[str, Any] | None = None,
        *,
        budget: CallBudget | None = None,
        selection_reason: str = "",
    ) -> ToolResult:
        active = budget if budget is not None else self._budget

        tool = self._tools.get(name)
        if tool is None:
            return self._record(
                ToolResult(
                    tool=name,
                    status=STATUS_UNAVAILABLE,
                    params=dict(params or {}),
                    error=f"工具未注册：{name}",
                ),
                active,
                selection_reason,
            )

        cleaned, error = validate_params(tool.spec, params)
        if error:
            return self._record(
                ToolResult(
                    tool=name,
                    status=STATUS_BAD_REQUEST,
                    params=dict(params or {}),
                    error=error,
                ),
                active,
                selection_reason,
            )

        if active is not None and active.exhausted():
            return self._record(
                ToolResult(
                    tool=name,
                    status=STATUS_BUDGET_EXCEEDED,
                    params=cleaned,
                    error="工具预算已耗尽，应转人工",
                ),
                active,
                selection_reason,
            )

        attempts = 0
        total_ms = 0
        result = ToolResult(tool=name, status=STATUS_UPSTREAM_ERROR, params=cleaned)

        while attempts <= tool.spec.max_retries:
            attempts += 1
            started = self._clock()
            try:
                outcome = tool._run(cleaned)
            except Exception as exc:  # 工具不该抛，抛了也不许掀翻整批任务
                outcome = _unexpected(exc)
            latency = int((self._clock() - started) * 1000)
            total_ms += latency

            over_time = (
                outcome.status in (STATUS_OK, "EMPTY")
                and latency > tool.spec.timeout_s * 1000
            )
            status = STATUS_TIMEOUT if over_time else outcome.status
            result = ToolResult(
                tool=name,
                status=status,
                params=cleaned,
                items=outcome.items,
                latency_ms=total_ms,
                attempts=attempts,
                error=(
                    f"超过超时上限 {tool.spec.timeout_s}s" if over_time else outcome.error
                ),
                degraded=outcome.degraded,
                degrade_reason=outcome.degrade_reason,
            )

            if status not in RETRYABLE_STATUSES or attempts > tool.spec.max_retries:
                break
            self._sleep(0.5 * (2 ** (attempts - 1)))

        if active is not None:
            active.charge(total_ms)
        return self._record(result, active, selection_reason)

    def _record(
        self,
        result: ToolResult,
        budget: CallBudget | None,
        selection_reason: str,
    ) -> ToolResult:
        self._records.append(
            ToolCallRecord(
                tool=result.tool,
                params=result.params,
                status=result.status,
                attempts=result.attempts,
                latency_ms=result.latency_ms,
                error=result.error,
                degraded=result.degraded,
                degrade_reason=result.degrade_reason,
                source_ids=result.source_ids(),
                digest=result.digest(),
                selection_reason=selection_reason,
                budget=budget.snapshot() if budget is not None else {},
            )
        )
        return result


def _unexpected(exc: Exception):
    from .base import Outcome

    return Outcome(
        status=STATUS_UPSTREAM_ERROR,
        error=f"{type(exc).__name__}: {exc}",
    )

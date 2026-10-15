"""工具层的公共契约：规格、结果与证据片段。

工具层要回答六个问题，缺一个研发就得回来问产品：参数长什么样、返回什么、
超时多少、重试几次、失败降级到哪、单次成本多少。这六项就是 ToolSpec。

两条设计约束（来自 docs/10-prd-agent-v3.md 第 5.3／5.4 节）：

1. 工具不向调用方抛异常，一律返回 ToolResult——与 adapters 的约定一致，
   单次工具失败不该让整批任务停下来。
2. EMPTY（查得到但零结果）不是错误，它是运行时的重试触发条件。工具层不做
   空结果重试，因为"换查询词"是策略，属于运行时，不属于工具。

已知局限：本地工具同步执行，超时是事后计时判定的，无法真正中断。接真实
网络工具时必须换成可中断实现，否则超时只是记账。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

STATUS_OK = "OK"
STATUS_EMPTY = "EMPTY"
STATUS_BAD_REQUEST = "BAD_REQUEST"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_UPSTREAM_ERROR = "UPSTREAM_ERROR"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

#: 会被工具层自动重试的状态。EMPTY 不在其中：换查询词是运行时的策略，
#: 原样重试同一个查询只会浪费时间与预算。
RETRYABLE_STATUSES: tuple[str, ...] = (STATUS_TIMEOUT, STATUS_UPSTREAM_ERROR)


@dataclass(frozen=True)
class ParamSpec:
    """单个参数的类型与必填约束。"""

    type: str  # "string" | "int"
    required: bool
    desc: str
    default: Any = None


@dataclass(frozen=True)
class ToolSpec:
    """工具的名片。六个字段缺一不可。"""

    name: str
    purpose: str
    params: Mapping[str, ParamSpec]
    returns: str
    timeout_s: float
    max_retries: int
    fallback: str | None
    cost_per_call: float = 0.0
    read_only: bool = True

    def param_names(self) -> list[str]:
        return list(self.params)


@dataclass(frozen=True)
class Evidence:
    """一条可用于支撑判断的证据片段。字段与 models.Source 对齐。"""

    source_id: str
    tier: str
    date: str
    text: str
    score: float = 0.0
    span: tuple[int, int] | None = None
    # 结构化工具（如 lookup_fact）把实体／指标／数值挂在这里，
    # 供运行时做冲突与过时判定；检索类工具留空。
    meta: Mapping[str, Any] | None = None


@dataclass
class Outcome:
    """工具内部执行结果，由 registry 包装成 ToolResult。"""

    status: str
    items: list[Evidence] = field(default_factory=list)
    error: str | None = None
    degraded: bool = False
    degrade_reason: str | None = None


@dataclass
class ToolResult:
    """对外统一的工具调用结果。"""

    tool: str
    status: str
    params: dict[str, Any] = field(default_factory=dict)
    items: list[Evidence] = field(default_factory=list)
    latency_ms: int = 0
    attempts: int = 1
    error: str | None = None
    degraded: bool = False
    degrade_reason: str | None = None

    @property
    def ok(self) -> bool:
        """OK 与 EMPTY 都算调用成功——区别在于有没有查到东西。"""

        return self.status in (STATUS_OK, STATUS_EMPTY)

    @property
    def empty(self) -> bool:
        return self.status == STATUS_EMPTY

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES

    def source_ids(self) -> list[str]:
        out: list[str] = []
        for item in self.items:
            if item.source_id not in out:
                out.append(item.source_id)
        return out

    def digest(self, limit: int = 160) -> str:
        if self.status == STATUS_OK:
            first = self.items[0].text
            body = first if len(self.items) == 1 else f"{first}（共 {len(self.items)} 条）"
        elif self.status == STATUS_EMPTY:
            body = "零结果"
        else:
            body = self.error or self.status
        return body[:limit]


class Tool(ABC):
    """工具基类。子类只实现 _run，重试与超时由 registry 负责。"""

    spec: ToolSpec

    @abstractmethod
    def _run(self, params: dict[str, Any]) -> Outcome:
        """执行一次调用。不得抛异常，异常由 registry 兜底为 UPSTREAM_ERROR。"""


def validate_params(
    spec: ToolSpec, params: Mapping[str, Any] | None
) -> tuple[dict[str, Any], str | None]:
    """校验并补全参数，返回 (清洗后的参数, 错误信息)。"""

    raw = dict(params or {})
    unknown = [key for key in raw if key not in spec.params]
    if unknown:
        return {}, f"未知参数：{'、'.join(sorted(unknown))}"

    cleaned: dict[str, Any] = {}
    for name, param in spec.params.items():
        if name not in raw:
            if param.required:
                return {}, f"缺少必填参数：{name}"
            cleaned[name] = param.default
            continue
        value = raw[name]
        if param.type == "string":
            if not isinstance(value, str):
                return {}, f"参数 {name} 应为字符串"
            value = value.strip()
            if param.required and not value:
                return {}, f"参数 {name} 不能为空"
        elif param.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return {}, f"参数 {name} 应为整数"
            if value <= 0:
                return {}, f"参数 {name} 必须为正整数"
        cleaned[name] = value

    return cleaned, None

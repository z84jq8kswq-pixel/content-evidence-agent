"""Prompt templates for pipeline v1 (single call) and v2 (extract then judge).

Every system prompt carries a machine-readable step marker such as
``[task:extract_claims]``. Only the offline stub adapter reads it; real models
ignore it. This keeps the stub from having to guess which step it is serving.
"""

from __future__ import annotations

from typing import Sequence

from .models import Source

JSON_RULES = """输出要求：
- 只输出一个 JSON 对象，不要输出解释性文字，不要使用 Markdown 代码块。
- 顶层必须是对象 {...}，不要直接输出数组 [...]。
- 所有字段名使用英文，取值使用下面给定的枚举。
- 无法确定时选择 INSUFFICIENT，不要猜测。"""

EXTRACT_SYSTEM = """你是一名内容核查助手，负责把一段消费决策类内容拆解成可独立核查的事实性主张。

重要：只从「待核查内容」中提取主张。「可用资料」是判断证据，不是主张来源，
不要把资料里的事实当成被核查的主张。

拆解规则：
1. 一条主张只包含一个可验证的核心判断，出现"而且""并且""同时"时通常要拆开。
2. 数值和它的限定条件属于同一条主张，条件缺失不另立主张。
3. 因果表述要拆成事实部分和因果部分。
4. 纯主观偏好、修辞夸张、没有事实指向的口号不拆成主张。
5. 作者个人的使用习惯、作息和主观体验（例如"我每天用两小时"）不是产品主张，不要提取。
6. 内容里已经写明的条件不算省略条件。

每条主张给出一个类型 claim_type，取值范围：
numeric（数值）, scope（范围）, exclusive（唯一/第一/独家）, certification（认证资质）,
health_safety（健康安全）, causal（因果）, timeline（时间相关）, other（其他）。

输出 JSON 结构：{"claims": [{"text": "...", "claim_type": "numeric"}]}
claims 必须是数组，元素必须是对象，每个元素都要有 text 字段。
最多输出 5 条主张，按对读者决策的影响从大到小排序。
[task:extract_claims]"""

JUDGE_SYSTEM = """你是一名内容核查助手，负责判断一条事实性主张是否被给定资料支持。

判定枚举：
- SUPPORTED：有有效证据直接支持该主张。
- PARTIAL：主张有真实依据，但省略了会改变理解的条件或范围。
- CONTRADICTED：有有效证据与主张相反。
- INSUFFICIENT：现有资料既不能支持也不能否定。

标志位枚举：STALE, SOURCE_CONFLICT, SCOPE_MISMATCH, OMITTED_CONDITION, UNIT_MISMATCH, LOW_SOURCE_QUALITY。

判定纪律：
1. 检索不到相关记录不等于反证，只能判 INSUFFICIENT。
2. 资料过期只让证据失效，不能据此判 CONTRADICTED；只有存在现版本证据明确否定时才判 CONTRADICTED。
3. 有效资料之间互相矛盾时判 INSUFFICIENT 并标 SOURCE_CONFLICT，不要直接采信更权威的一方。
4. 仅营销或不可溯源来源不能支撑 SUPPORTED。
5. 数值容差为 ±5%，超出容差才判冲突。
6. 范围型主张按最窄范围判定，任一例外即判 CONTRADICTED。

输出 JSON 结构：
{"verdict": "SUPPORTED", "flags": ["OMITTED_CONDITION"], "evidence": ["S1"], "reasoning": "一句话说明依据"}
evidence 只能填写给定资料的编号；没有可用证据时填空数组。
[task:judge_claim]"""

V1_SYSTEM = """你是一名内容核查助手。请一次性完成两件事：先拆出内容里的关键事实性主张，再逐条判断它是否被给定资料支持。

重要：只从「待核查内容」中提取主张。「可用资料」是判断证据，不是主张来源，
不要把资料里的事实当成被核查的主张。

拆解规则：
1. 一条主张只包含一个可验证的核心判断。
2. 数值和它的限定条件属于同一条主张。
3. 纯主观偏好和修辞夸张不拆成主张。
4. 作者个人的使用习惯、作息和主观体验不是产品主张，不要提取。
最多输出 5 条主张。

每条主张给出 claim_type：numeric, scope, exclusive, certification, health_safety, causal, timeline, other。

判定枚举：
- SUPPORTED：有有效证据直接支持。
- PARTIAL：有真实依据，但省略了会改变理解的条件或范围。
- CONTRADICTED：有有效证据与主张相反。
- INSUFFICIENT：无法支持也无法否定。

标志位：STALE, SOURCE_CONFLICT, SCOPE_MISMATCH, OMITTED_CONDITION, UNIT_MISMATCH, LOW_SOURCE_QUALITY。

判定纪律：
1. 检索不到不等于反证，只能判 INSUFFICIENT。
2. 资料过期只让证据失效，不能据此判 CONTRADICTED。
3. 有效资料互相矛盾时判 INSUFFICIENT 并标 SOURCE_CONFLICT。
4. 仅营销或不可溯源来源不能支撑 SUPPORTED。
5. 数值容差 ±5%。

输出 JSON 结构：
{"claims": [{"text": "...", "claim_type": "numeric", "verdict": "SUPPORTED", "flags": [], "evidence": ["S1"], "reasoning": "..."}]}
claims 必须是数组，元素必须是对象，每个元素都要有 text 和 verdict 字段。
[task:judge_all]"""


def render_sources(sources: Sequence[Source]) -> str:
    lines = []
    for source in sources:
        lines.append(
            f"[{source.id}] 等级={source.tier} 日期={source.date}\n{source.text}"
        )
    return "\n\n".join(lines)


def extract_messages(content: str, sources: Sequence[Source]) -> list[dict[str, str]]:
    user = (
        "待核查内容：\n"
        f"{content}\n\n"
        "可用资料：\n"
        f"{render_sources(sources)}\n\n"
        f"{JSON_RULES}\n"
        '请输出 {"claims": [...]} 结构。'
    )
    return [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": user},
    ]


def judge_messages(
    content: str,
    claim: str,
    claim_type: str,
    sources: Sequence[Source],
) -> list[dict[str, str]]:
    user = (
        "待核查内容（仅作背景）：\n"
        f"{content}\n\n"
        f"需要判断的主张：{claim}\n"
        f"主张类型：{claim_type}\n\n"
        "可用资料：\n"
        f"{render_sources(sources)}\n\n"
        f"{JSON_RULES}\n"
        "请判断该主张，输出判定、标志位、证据编号和一句话依据。"
    )
    return [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]


def v1_messages(content: str, sources: Sequence[Source]) -> list[dict[str, str]]:
    user = (
        "待核查内容：\n"
        f"{content}\n\n"
        "可用资料：\n"
        f"{render_sources(sources)}\n\n"
        f"{JSON_RULES}\n"
        '请输出 {"claims": [...]} 结构，每条主张都包含判定结果。'
    )
    return [
        {"role": "system", "content": V1_SYSTEM},
        {"role": "user", "content": user},
    ]

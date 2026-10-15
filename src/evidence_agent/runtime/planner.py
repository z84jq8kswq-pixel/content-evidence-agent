"""策略层：决定每条主张用什么工具、传什么参数，以及第一次没查到之后怎么办。

设计原则来自 PRD 5.2：**规则负责兜底，模型负责搜索。**
工具选择先用 claim_type 的映射表定基线（5.5 节），失败后按固定顺序换策略：
换查询词 → 换工具 → 取原文定位。顺序写死，所以可复现、可评测、可解释。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..tools.base import Evidence
from .assessor import PERMISSION_RULES

#: claim_type -> (首选工具, 备选工具)。与 PRD 5.5 的映射表一致。
CLAIM_TOOL_MAP: dict[str, tuple[str, str | None]] = {
    "numeric": ("lookup_fact", "search_notes"),
    "certification": ("lookup_fact", "search_notes"),
    "health_safety": ("lookup_fact", "search_notes"),
    "exclusive": ("search_notes", "fetch_source"),
    "timeline": ("search_notes", "fetch_source"),
    "scope": ("search_notes", "fetch_source"),
    "causal": ("search_notes", None),
    "other": ("search_notes", None),
}

TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("certification", ("认证", "资质", "名录", "检测报告", "获奖")),
    ("health_safety", ("安全", "危害", "有毒", "过敏", "副作用", "健康")),
    ("exclusive", ("唯一", "全系", "首款", "史上", "所有型号")),
    ("timeline", ("最新", "今年", "刚刚", "发布", "更新")),
    ("scope", ("仅限", "适用于", "条件下", "范围")),
    ("causal", ("导致", "因为", "改善", "治疗")),
)

METRIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("额定能量", ("额定能量", "wh")),
    ("容量", ("容量", "mah", "毫安")),
    # 功率的口语与规格页写法都要认。只写"快充／充电功率"时，真实规格页上的
    # "输出参数 45W MAX""最大输出 65W"这类说法会认不出指标，数值比对直接失效、
    # 只能转人工——这是接真实语料时实测暴露的缺口。
    ("快充功率", ("快充", "充电功率", "输出功率", "输入功率", "最大输出", "输出")),
    ("防水等级", ("防水", "ipx")),
    ("续航", ("续航", "用多久", "用几天", "充电一次", "不充电")),
    # 降噪开启后的续航是独立的一个指标：和标称续航是两个数，不能混着比
    ("降噪续航", ("降噪",)),
    ("认证", ("认证", "资质", "名录")),
    ("价格", ("价格", "售价")),
    # 下面几条是扩样时新加的形状（净水器、成分类），指标名按物质精确命名，
    # 不用一个笼统的"浓度"把不同物质混在一起（那是 13 号文档里"认证"粒度太粗的同类错误）
    ("滤芯寿命", ("滤芯", "寿命")),
    ("通量", ("通量",)),
    ("废水比", ("废水比",)),
    ("烟酰胺浓度", ("烟酰胺",)),
    ("含铁量", ("含铁量", "铁含量")),
)

NUMBER_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*([A-Za-z]+|小时|个月|分钟|天|年|次|%)", re.IGNORECASE
)


def infer_claim_type(text: str) -> str:
    lowered = (text or "").lower()
    for claim_type, words in TYPE_KEYWORDS:
        if any(word in lowered for word in words):
            return claim_type
    if NUMBER_UNIT.search(text or "") or re.search(r"\d", text or ""):
        return "numeric"
    return "other"


def infer_metric(text: str) -> str | None:
    lowered = (text or "").lower()
    for metric, words in METRIC_KEYWORDS:
        if any(word in lowered for word in words):
            return metric
    return None


#: 比例型写法：废水比 1:1、功效比 5:1。归一成 第一项/第二项，单位记 RATIO_UNIT。
RATIO = re.compile(r"(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)")
RATIO_UNIT = ":1"


def claim_numbers(text: str) -> list[tuple[float, str]]:
    """抽出主张里的（数值，单位）。用于与事实库做数值比对。

    比例型指标（废水比 1:1）没有 数字+单位 的写法，单独抽，归一成
    第一项/第二项（第二项为 0 时跳过），单位记 :1。
    """

    out: list[tuple[float, str]] = []
    for raw_value, unit in NUMBER_UNIT.findall(text or ""):
        out.append((float(raw_value), unit))
    for first, second in RATIO.findall(text or ""):
        denominator = float(second)
        if denominator:
            out.append((float(first) / denominator, RATIO_UNIT))
    return out


def rewrite_query(text: str, entity: str | None, metric: str | None) -> str:
    """把整句主张压成关键词，用于第二次检索。"""

    parts: list[str] = []
    if entity:
        parts.append(entity)
    if metric:
        parts.append(metric)
    parts.extend(f"{value:g}{unit}" for value, unit in claim_numbers(text))
    return " ".join(parts) if parts else (text or "").strip()


@dataclass(frozen=True)
class Plan:
    tool: str
    params: dict[str, Any]
    reason: str
    strategy: str

    def key(self) -> tuple[str, str]:
        return (self.strategy, self.tool)


class Planner:
    """按 claim_type 给出有序策略列表。工具注册情况由运行时过滤。"""

    def __init__(
        self,
        resolve_entity: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._resolve_entity = resolve_entity or (lambda _text: [])

    def entity_of(self, claim_text: str, context: str = "") -> str | None:
        """先看主张本身，再看正文。

        主张句常常省略主体（"获得口腔护理协会认证"），主体在正文里。只在主张
        里找实体，会让事实库查询白白空跑一次。
        """

        for text in (claim_text, context):
            found = self._resolve_entity(text or "")
            if found:
                return found[0]
        return None

    def strategies(
        self,
        claim_text: str,
        claim_type: str,
        evidence: Iterable[Evidence] = (),
        context: str = "",
    ) -> list[Plan]:
        baseline, fallback = CLAIM_TOOL_MAP.get(claim_type, ("search_notes", None))
        entity = self.entity_of(claim_text, context)
        metric = infer_metric(claim_text)
        plans: list[Plan] = []

        # 许可型主张问的是"有没有越过规定上限"，得先拿到主体的那个数值，
        # 否则无从比对。所以它先于普通的事实库查询。
        permission = next(
            (
                rule
                for rule in PERMISSION_RULES
                if any(word in (claim_text or "") for word in rule.keywords)
            ),
            None,
        )
        if permission and entity:
            plans.append(
                Plan(
                    "lookup_fact",
                    {"entity": entity, "metric": permission.subject_metric},
                    f"许可型主张：先取 {permission.subject_metric} 再比对限额",
                    "permission_fact",
                )
            )

        # 事实库查询需要"实体 + 指标"两个坐标。只有实体没有指标时，查询会把该
        # 实体的全部记录拉回来，一旦其中任意两项互相冲突就会误触人工队列——
        # 那种冲突和当前主张无关。所以缺指标时退回检索。
        if baseline == "lookup_fact" and entity and metric:
            plans.append(
                Plan(
                    "lookup_fact",
                    {"entity": entity, "metric": metric},
                    f"{claim_type} 类型优先查事实库",
                    "baseline",
                )
            )
            plans.append(
                Plan(
                    "search_notes",
                    {"query": rewrite_query(claim_text, entity, metric)},
                    "事实库不足，改用检索",
                    "switch",
                )
            )
        else:
            if baseline == "lookup_fact":
                reason = (
                    f"{claim_type} 类型但认不出实体，改用检索"
                    if not entity
                    else f"{claim_type} 类型但认不出指标，改用检索"
                )
            else:
                reason = f"{claim_type} 类型先做检索"
            plans.append(
                Plan("search_notes", {"query": claim_text}, reason, "baseline")
            )
            plans.append(
                Plan(
                    "search_notes",
                    {"query": rewrite_query(claim_text, entity, metric)},
                    "改写查询词重试",
                    "rewrite",
                )
            )

        items = list(evidence)
        if items:
            best = max(items, key=lambda item: (item.score, item.date))
            plans.append(
                Plan(
                    "fetch_source",
                    {"source_id": best.source_id, "span_hint": metric or ""},
                    "取原文定位到具体段落",
                    "fetch_span",
                )
            )

        # 去掉参数不全的计划，避免把注定失败的调用算进预算
        usable: list[Plan] = []
        for plan in plans:
            if plan.tool == "lookup_fact" and not plan.params.get("entity"):
                continue
            if plan.tool == "fetch_source" and not plan.params.get("source_id"):
                continue
            usable.append(plan)
        return usable

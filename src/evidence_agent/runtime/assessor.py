"""证据充分性判定：什么时候可以下结论，什么时候必须交回给人。

这一节决定 Agent 能不能自己做主，所以全部写成规则，模型没有否决权（PRD 5.2）。
三类情况直接转人工：来源冲突、来源被更新覆盖（过期）、只有低等级来源。

未实现的规则（明确记录，不含糊）：
- "来源日期早于说法生效期"需要解析主张自身的时间范围，本期没有做；
- 语义级冲突（两条都在说同一件事但结论相反）只能靠结构化字段识别，
  纯文本证据之间的冲突会被漏掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from ..tools.base import Evidence
from ..grading import grade_of

#: 出现这些标志位就不再下结论，直接转人工。
BLOCKING_FLAGS: tuple[str, ...] = (
    "SOURCE_CONFLICT",
    "STALE",
    "SCOPE_MISMATCH",
    "NOT_COMPARABLE",
)

#: 容差按指标类型配。测量类天然离散，标称类不该有偏差。
TOLERANCE_BY_METRIC: dict[str, float] = {
    "续航": 0.20,
    "亮度": 0.20,
    "耗时": 0.20,
    "降噪": 0.20,
    "降噪续航": 0.20,
    "滤芯寿命": 0.20,
    "容量": 0.05,
    "额定能量": 0.05,
    "快充功率": 0.05,
    "价格": 0.05,
    "通量": 0.05,
    "烟酰胺浓度": 0.05,
    "含铁量": 0.05,
    "废水比": 0.10,
}

#: 默认容差。原来的 10% 是全域硬编码，现在只是兜底值。
RELATIVE_CONFLICT_TOLERANCE = 0.10

#: 会正常随时间变化的指标。不同时间点数值不同是"变化"，
#: 既不是来源冲突，也不是资料过期。
VOLATILE_METRICS: frozenset[str] = frozenset(
    {"价格", "售价", "销量", "库存", "排名", "粉丝数"}
)


def tolerance_for(metric: str) -> float:
    return TOLERANCE_BY_METRIC.get(metric, RELATIVE_CONFLICT_TOLERANCE)


def comparability(rows: Sequence[Mapping[str, Any]]) -> tuple[bool, str]:
    """先判这两条记录说的是不是同一件事。

    条件不同（一个标称、一个在特定负载下实测）或取样方式不同（送检样 vs 市售品），
    两个数根本不该放在一起比——那是**不可比**，不是矛盾。

    参数是结构化记录行（事实行的字段，或证据自带的 meta），不是 Evidence 本身：
    检索路径拿到的资料，它的事实挂在 meta["facts"] 里。
    """

    def distinct(field: str) -> list[str]:
        return sorted(
            {
                str(row.get(field, "")).strip()
                for row in rows
                if str(row.get(field, "")).strip()
            }
        )

    conditions = distinct("conditions")
    if len(conditions) > 1:
        return False, "测试条件不同（" + " / ".join(conditions) + "）"
    samplings = distinct("sampling")
    if len(samplings) > 1:
        return False, "取样方式不同（" + " / ".join(samplings) + "）"
    return True, ""

#: 两条记录相隔超过这个天数，视为新来源覆盖旧来源，而不是并行冲突。
SUPERSEDE_GAP_DAYS = 365

TIER_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


def _tier_rank(tier: str) -> int:
    return TIER_RANK.get((tier or "").upper(), 9)


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class Assessment:
    sufficient: bool
    items: list[Evidence] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    reason: str = ""
    conflicting: list[tuple[str, str]] = field(default_factory=list)
    superseded: list[tuple[str, str]] = field(default_factory=list)
    not_comparable: list[tuple[str, str]] = field(default_factory=list)
    #: 承载结论的来源：冲突／过期／不可比时点名参与那组比较的来源，否则是最权威的那条。
    #: 供评测检查"关键证据是否真的在结论依据里"——只检查"检索到过"会虚高。
    decisive_sources: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        return any(flag in BLOCKING_FLAGS for flag in self.flags)

    def best(self) -> Evidence | None:
        """最权威的一条证据。

        用 `grade_of(...).rank_key`（身份 → 取样 → 资质 → 日期）比较，
        **不用展示权重**：权重是三档展示值，用它比大小会让权威性依赖数值巧合。
        排序键最后一段就是日期（字符串字典序），同身份同取样同资质时自然取更新的那条。
        """

        if not self.items:
            return None
        return min(self.items, key=lambda item: grade_of(item).rank_key)


def _dedupe(items: Iterable[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, str]] = set()
    out: list[Evidence] = []
    for item in items:
        key = (item.source_id, item.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _records_of(
    item: Evidence, claim_metric: str = "", claim_entity: str = ""
) -> list[Mapping[str, Any]]:
    """一条证据携带的、可参与冲突判定的结构化记录。

    两类来源：

    1. 证据自己的 meta——lookup_fact 直接把事实行的字段挂在上面；
    2. meta["facts"]——检索路径（search_notes／fetch_source）拿到的资料，
       原文里没有可比的数值，能比的是这份资料在事实库里的结构化记录。

    第二类只在**主张的指标已知且对得上**时参与。否则同一份资料里与当前主张无关的
    指标（一条充电宝资料同时有容量、额定能量、快充功率）会把无关的冲突顶上来，
    把本该判定的主张拖进人工队列。
    """

    meta = item.meta or {}
    rows: list[Mapping[str, Any]] = []
    if str(meta.get("entity", "")).strip() and str(meta.get("metric", "")).strip():
        rows.append(meta)
    wanted = str(claim_metric or "").strip()
    entity = str(claim_entity or "").strip()
    facts = meta.get("facts")
    if wanted and isinstance(facts, Sequence) and not isinstance(facts, str):
        rows.extend(
            fact
            for fact in facts
            if isinstance(fact, Mapping)
            and str(fact.get("metric", "")).strip() == wanted
            # 实体也要对得上：别的产品的同指标记录不能拿来判这条主张。
            # 这是扩样时真实撞到的：加了净水器语料后，莱茵 H9 的「滤芯寿命」@
            # 主张检索到了沁澜 R3 的两条记录，被误判成「来源过期」。
            # 实体必须认得出来、而且对得上：认不出主体的主张（「这款」、陌生品牌）
            # 不能用检索顺手带回来的结构化事实去判冲突／过期——那是别的产品的东西。
            and entity
            and str(fact.get("entity", "")).strip() == entity
        )
    return rows


def _is_about_entity(item: Evidence, claim_entity: str) -> bool:
    """这条证据是不是在说这个主体。

    主体认得出来时，只有提到这个主体的证据才算数。检索池里经常混进别的产品的权威文档，
    如果把它们算进「证据充分」，一条只有 D 级用户帖支撑的主张也会被判成支持——扩样时
    真实发生过。这是「检索没有相关性门槛」的实体版本：以主体为界，可判、可测。
    """

    if not claim_entity:
        return True
    meta = item.meta or {}
    facts = meta.get("facts")
    if isinstance(facts, Sequence) and not isinstance(facts, str):
        for fact in facts:
            if isinstance(fact, Mapping) and str(fact.get("entity", "")).strip() == claim_entity:
                return True
    row_entity = str(meta.get("entity", "")).strip()
    if row_entity:
        return row_entity == claim_entity
    return claim_entity in (item.text or "")


def _group_credible(
    items: Sequence[Evidence], claim_metric: str = "", claim_entity: str = ""
) -> dict[tuple[str, str], list[tuple[Evidence, Mapping[str, Any]]]]:
    """只让 A／B 级来源参与冲突与过时判定。

    低等级来源之间存在分歧不算"来源冲突"——那是噪音，不是矛盾。若把 C／D
    也拉进来，任何一条夸大文案都会把结论拖进人工队列。
    """

    groups: dict[tuple[str, str], list[tuple[Evidence, Mapping[str, Any]]]] = {}
    for item in items:
        if not grade_of(item).strong:
            continue
        for row in _records_of(item, claim_metric, claim_entity):
            entity = str(row.get("entity", "")).strip()
            metric = str(row.get("metric", "")).strip()
            if entity and metric:
                groups.setdefault((entity, metric), []).append((item, row))
    return groups


def assess(
    items: Iterable[Evidence], *, claim_metric: str = "", claim_entity: str = ""
) -> Assessment:
    """判定证据是否足以支撑一个结论。

    claim_metric 是主张涉及的指标、claim_entity 是主体（运行时的 infer_metric／
    entity_of 结果）。检索路径带回来的结构化事实参不参与冲突判定由这两个决定，
    见 _records_of——指标不对、实体不对，都不能拿来判这条主张。
    """

    unique = _dedupe(items)
    if not unique:
        return Assessment(sufficient=False, reason="没有任何证据")

    # 证据包只留「在说这条主张主体」的材料：混进来的别的产品的文档会让强度判定虚高
    relevant = [item for item in unique if _is_about_entity(item, claim_entity)]
    if relevant:
        unique = relevant

    conflicting: list[tuple[str, str]] = []
    superseded: list[tuple[str, str]] = []
    not_comparable: list[tuple[str, str]] = []
    decisive: list[str] = []

    for key, rows in _group_credible(unique, claim_metric, claim_entity).items():
        metric = key[1]
        comparable, why = comparability([row for _item, row in rows])
        if not comparable:
            not_comparable.append((key, why))
            decisive.extend(item.source_id for item, _row in rows)
            continue

        values: list[float] = []
        texts: set[str] = set()
        for _item, row in rows:
            number = _number(row.get("value"))
            if number is None:
                texts.add(str(row.get("value", "")).strip())
            else:
                values.append(number)

        differs = False
        if len(values) > 1:
            low, high = min(values), max(values)
            base = abs(high) or 1.0
            differs = (high - low) / base > tolerance_for(metric)
        if not differs and len(texts) > 1:
            differs = True
        if not differs:
            continue

        dates = sorted(
            d
            for d in (_parse_date(row.get("date") or item.date) for item, row in rows)
            if d
        )
        gap = (dates[-1] - dates[0]).days if len(dates) > 1 else 0
        if metric in VOLATILE_METRICS:
            not_comparable.append(
                (key, f"{metric}会随时间变化，而主张没有指明时间范围")
            )
            decisive.extend(item.source_id for item, _row in rows)
            continue
        if gap > SUPERSEDE_GAP_DAYS:
            superseded.append(key)
        else:
            conflicting.append(key)
        decisive.extend(item.source_id for item, _row in rows)

    flags: list[str] = []
    if conflicting:
        flags.append("SOURCE_CONFLICT")
    if superseded:
        flags.append("STALE")
    if not_comparable:
        flags.append("NOT_COMPARABLE")

    # 最权威的一条：用权威性排序键，不用展示权重（权重是三档展示值）。
    # 它的身份会写进 reason，所以"为什么是这条来源"必须与排序口径一致。
    best = min(unique, key=lambda item: grade_of(item).rank_key)
    best_grade = grade_of(best)
    has_strong = best_grade.strong
    if not has_strong:
        flags.append("LOW_SOURCE_QUALITY")

    if conflicting:
        reason = "同一指标存在互相冲突的来源，不做判定"
    elif superseded:
        reason = "来源已被更新的记录覆盖（过期），不做判定"
    elif not_comparable:
        reason = not_comparable[0][1] + "，两个数不该直接比，不做判定"
    elif not has_strong:
        reason = "只有低等级来源，证据不足"
    else:
        reason = f"存在可用的高等级来源（{best.source_id}，{best_grade.identity}）"

    if not decisive and has_strong:
        decisive.append(best.source_id)

    sufficient = has_strong and not any(flag in BLOCKING_FLAGS for flag in flags)
    return Assessment(
        sufficient=sufficient,
        items=unique,
        flags=flags,
        reason=reason,
        conflicting=conflicting,
        superseded=superseded,
        not_comparable=not_comparable,
        decisive_sources=tuple(dict.fromkeys(decisive)),
    )


def compare_numeric(
    claim_text: str, items: Sequence[Evidence]
) -> tuple[str, str, tuple[str, ...]] | None:
    """能用数值直接比对的主张，由规则给结论；比不了就返回 None 交给模型。

    第三个元素是**承载这个结论的来源编号**：评测要拿它检查"关键证据是否真的在
    结论依据里"，而不是只检查"检索到过"。
    """

    from .planner import claim_numbers, infer_metric

    metric = infer_metric(claim_text)
    for value, unit in claim_numbers(claim_text):
        candidates = []
        for item in items:
            meta = item.meta or {}
            if str(meta.get("unit", "")).lower() != unit.lower():
                continue
            # 同单位不等于同指标：续航 40 小时和降噪续航 28 小时都是"小时"，
            # 拿错那条比会得出相反的结论。主张的指标认得出来时，只比同指标的记录。
            row_metric = str(meta.get("metric", "")).strip()
            if metric and row_metric and row_metric != metric:
                continue
            number = _number(meta.get("value"))
            if number is not None:
                candidates.append((number, item))
        if not candidates:
            continue

        # 取最权威的一条：排序键越小越权威（含日期段，越新越小）。
        target, source = min(candidates, key=lambda pair: grade_of(pair[1]).rank_key)
        if target == 0:
            continue
        deviation = abs(value - target) / abs(target)
        if deviation <= RELATIVE_CONFLICT_TOLERANCE:
            return (
                "SUPPORTED",
                f"主张数值 {value:g}{unit} 与最权威来源一致"
                f"（{target:g}{unit}，{source.source_id}）",
                (source.source_id,),
            )
        return (
            "CONTRADICTED",
            f"主张数值 {value:g}{unit} 与最权威来源相差 {deviation:.0%}"
            f"（{target:g}{unit}，{source.source_id}）",
            (source.source_id,),
        )
    return None


def has_strong_source(items: Sequence[Evidence]) -> bool:
    """有没有够格的来源。用于"证据质量"门槛，与"是否存在冲突"分开。"""

    return any(grade_of(item).strong for item in items)


@dataclass(frozen=True)
class PermissionRule:
    """许可型主张的比对规则。

    这类主张问的不是"某个数值是多少"，而是"某个数值有没有越过规定上限"，
    所以需要把法规阈值也存成事实，才能自动比对。
    """

    keywords: tuple[str, ...]
    subject_metric: str
    limit_entity: str
    limit_metric: str


PERMISSION_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(
        keywords=("带上飞机", "带上机", "上飞机", "登机", "随身携带"),
        subject_metric="额定能量",
        limit_entity="民航规定",
        limit_metric="可随身携带额定能量上限",
    ),
)


def compare_permission(
    claim_text: str,
    items: Sequence[Evidence],
    facts: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, str, tuple[str, ...]] | None:
    """许可型主张的阈值比对。

    和其他规则不同的一点：**它能吸收小幅来源冲突**。只要所有候选值都落在限额
    同一侧，结论就不受影响——74Wh 和 62Wh 虽然互相冲突，但都在 100Wh 以内，
    "可以带上飞机"因此成立。阈值判定对分歧是鲁棒的，这是它值得单独成规则的原因。
    """

    text = claim_text or ""
    for rule in PERMISSION_RULES:
        if not any(word in text for word in rule.keywords):
            continue

        candidates: list[tuple[float, Evidence]] = []
        unit = ""
        for item in items:
            meta = item.meta or {}
            if str(meta.get("metric", "")) != rule.subject_metric:
                continue
            value = _number(meta.get("value"))
            if value is None:
                continue
            unit = unit or str(meta.get("unit", ""))
            candidates.append((value, item))
        if not candidates:
            continue

        limit_row = next(
            (
                row
                for row in facts
                if str(row.get("entity", "")) == rule.limit_entity
                and str(row.get("metric", "")) == rule.limit_metric
            ),
            None,
        )
        limit = _number(limit_row.get("value")) if limit_row else None
        if limit is None:
            continue

        values = sorted({value for value, _ in candidates})
        sources = sorted({item.source_id for _, item in candidates})
        shown = "/".join(f"{value:g}{unit}" for value in values)
        limit_source = str(limit_row.get("source_id", ""))
        carrying = tuple(sources) + ((limit_source,) if limit_source else ())
        if all(value <= limit for value in values):
            return (
                "SUPPORTED",
                f"来源给出的 {rule.subject_metric} 为 {shown}，均不超过 "
                f"{limit:g}{unit} 的限额（{limit_source}），该说法成立",
                carrying,
            )
        if all(value > limit for value in values):
            return (
                "CONTRADICTED",
                f"来源给出的 {rule.subject_metric} 为 {shown}，均超过 "
                f"{limit:g}{unit} 的限额（{limit_source}），该说法不成立",
                carrying,
            )
        return (
            "INSUFFICIENT",
            f"来源之间的 {rule.subject_metric} 分别落在限额两侧（{shown}），需人工确认",
            carrying,
        )
    return None

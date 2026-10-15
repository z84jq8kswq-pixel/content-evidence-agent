"""来源分级：把"一个字母"拆成可判断的维度。

原来的 A/B/C/D 把两件事压成了一个值：

1. 这条证据**形式上**来自谁（来源身份）
2. 这条证据**本身**站不站得住（取样方式、资质）

"第三方检测"这个标签下混着完全不同的东西：有认可资质、市场随机购买样品的检测；
也有厂商送检的"特供样"——送检版和市售版不是同一批，这是行业公认的问题；
还有收了钱的商业评测。把它们归进同一个字母，等于假装它们等价。

**三个职责必须分开，不要合成一个分数**（这是 2026-10-15 重构的核心）：

| 层 | 回答的问题 | 实现 | 谁在用 |
|---|---|---|---|
| 资格门槛 | 这条来源能不能单独支撑一个结论 | `_eligibility()` | 充分性判定、`strong`、对外动作闸门 |
| 权威排序 | 多条来源打架时听谁的 | `rank_key` / `rank_key_for_row()` | `assessor.best()`、`compare_numeric()`、`highlight` |
| 展示权重 | 报告与界面上显示什么强度值 | `_display_weight()` | 报告、工作台、标记 |

历史上取样方式与资质既参与门槛、又被加进权重，形成重复计分与假精度：
同一个事实算了两遍，且"跨维度可加"会让数值巧合决定权威性排序
（例如列名 0.85 + 随机取样 0.10 理论上可以贴近监管库的 1.00）。
现在资质与取样方式**只**决定门槛，权重只做展示，权威性由有序元组保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Sequence

#: 来源类型 -> 来源身份。语料里的 kind 字段决定身份，不用重复标注。
IDENTITY_BY_KIND: dict[str, str] = {
    "监管公开库": "监管机构",
    "独立检测": "独立第三方",
    "行业组织名录": "独立第三方",
    "品牌官方声明": "厂商自述",
    "平台规则": "平台机构",
    "站内用户内容": "用户自述",
    "站内营销内容": "用户自述",
}

#: 来源身份的权威性等级，**数字越小越权威**。
#: 语义来自 `文档库/02-判定规则/来源分级.md` 与处置档位的举证责任：
#: 监管公开信息 > 独立第三方 > 平台机构 / 厂商自述 > 未知 > 用户自述。
#: 这是门槛之外的另一套职责——门槛回答"够不够格"，等级回答"谁更硬"。
IDENTITY_RANK: dict[str, int] = {
    "监管机构": 0,
    "独立第三方": 1,
    "平台机构": 2,
    "厂商自述": 3,
    "未知": 4,
    "用户自述": 5,
}

#: 旧等级（A–D 与任务集的 E1–E4）折算成的身份等级。
#: 只有旧数据或测试夹具缺少维度信息时才会用到：让它们与带维度的证据仍可比较，
#: 而不是被排到所有带维度证据的后面。
TIER_IDENTITY_RANK: dict[str, int] = {
    "A": 0,
    "E1": 0,
    "B": 1,
    "E2": 1,
    "C": 2,
    "E3": 2,
    "D": 3,
    "E4": 3,
}
UNKNOWN_IDENTITY_RANK = 4

#: 取样方式的排序优先级（**数字越小越优先**），只在权威性排序里用。
#: 它不参与门槛之外的其他判断：门槛由 `_eligibility` 直接给出结论。
#: 市场随机购买 > 未说明 > 厂商送检：送检样与市售品不是同一批。
SAMPLING_RANK_BY_NAME: dict[str, int] = {
    "市场随机购买": 0,
    "厂商送检": 2,
}
DEFAULT_SAMPLING_RANK = 1
MISSING_SAMPLING_RANK = 1
#: 旧数据缺字段时的兜底：排在合法取样之后，比"厂商送检"靠前。
UNKNOWN_SAMPLING_RANK = 3

#: 日期的折算基准与"缺失日期"的排序值。日期换算成"距基准的天数取负"，
#: 于是越新越小，与排序键前三段（越小越权威）方向一致；缺失日期排最后。
#: 用一个有限的大整数而不是 float('inf')：排序键会进 `to_dict()` 被序列化成
#: JSON，Infinity 不是合法 JSON。
DATE_BASELINE = date(2000, 1, 1)
NO_DATE_KEY = 10**8

#: 展示权重：三档，由门槛结论直接推出，**不参与资格判定、也不决定权威排序**。
#: 分成"强来源 / 厂商标称 / 不合格"三档的原因：资质与取样已经在门槛里判过，
#: 再加权就是重复计分，而报告里出现"0.85 + 0.10"这种算式会误导读者。
WEIGHT_STRONG = 0.90
WEIGHT_VENDOR = 0.70
WEIGHT_INELIGIBLE = 0.40

#: 门槛未通过的来源，展示权重压到这个上限之下。保留是为了让
#: "权重 >= STRONG_THRESHOLD" 与门槛结论不矛盾（旧调用方的兼容口径）。
INELIGIBLE_CEILING = 0.55

#: 低于这个权重就不算"能支撑结论的来源"。是否够格由 `_eligibility` 的门槛直接
#: 给出；权重只是把门槛结论翻译成一个可显示的数字，供排序辅助、高亮与报告使用。
STRONG_THRESHOLD = 0.60

#: 只有旧 tier、没有维度信息时的兜底权重。语料里的行都应该带 kind，
#: 兜底只是为了不让老数据算不出来。
TIER_FALLBACK_WEIGHT: dict[str, float] = {
    "A": 0.95,
    "B": 0.85,
    "C": 0.40,
    "D": 0.25,
    # 评测任务集（data/eval_tasks.json）用的是 E1–E4，语义见 docs/02-labeling-rules.md：
    # E1 一手可核验、E2 权威第三方、E3 用户实测、E4 营销／不可溯源，与 A–D 一一对应。
    # 不映射的话这些来源会全部落到"未知"（0.35，非强来源），而且不报错。
    "E1": 0.95,
    "E2": 0.85,
    "E3": 0.40,
    "E4": 0.25,
}


@dataclass(frozen=True)
class SourceGrade:
    identity: str
    sampling: str
    accredited: bool
    tier: str
    #: 展示权重：三档，由门槛结论推出。**不参与资格判定，也不决定权威排序。**
    weight: float
    #: 能不能单独支撑一个结论。由门槛（`_eligibility`）给出，不由权重比大小推出。
    strong: bool = False
    reasons: list[str] = field(default_factory=list)
    #: 权威性排序键：身份等级 → 取样优先级 → 资质 → 日期（越新越小）。
    #: 元组越小越权威。用它代替"比 weight 大小"，权威性就不再依赖数值巧合。
    rank: tuple[int, int, int, int] = ()

    @property
    def rank_key(self) -> tuple[int, int, int, int]:
        """权威性排序键。没显式给出时按身份与字段现算（兼容手工构造的实例）。"""

        if self.rank:
            return self.rank
        return (
            IDENTITY_RANK.get(self.identity, UNKNOWN_IDENTITY_RANK),
            sampling_rank(self.sampling, self.identity),
            0 if self.accredited else 1,
            score_sort_key(""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "sampling": self.sampling,
            "accredited": self.accredited,
            "tier": self.tier,
            "weight": round(self.weight, 2),
            "strong": self.strong,
            "reasons": self.reasons,
            "rank": list(self.rank_key),
        }


def identity_of(kind: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    return IDENTITY_BY_KIND.get(kind, "未知")


def identity_rank_of(
    *, kind: str = "", identity: str = "", tier: str = ""
) -> int:
    """身份等级。认不出身份时退回按旧等级折算，认不出等级则按"未知"处理。"""

    resolved = identity_of(kind, identity)
    if resolved != "未知":
        return IDENTITY_RANK.get(resolved, UNKNOWN_IDENTITY_RANK)
    return TIER_IDENTITY_RANK.get((tier or "").upper(), UNKNOWN_IDENTITY_RANK)


def sampling_rank(sampling: str, identity: str = "") -> int:
    """取样方式的排序优先级（数字越小越优先）。

    只在权威性排序里用。它**不影响**门槛结论：过不过门槛由 `_eligibility`
    直接给出，这里只回答"两条都够格的来源，哪条更硬"。
    """

    value = (sampling or "").strip()
    if not value:
        return MISSING_SAMPLING_RANK
    return SAMPLING_RANK_BY_NAME.get(value, UNKNOWN_SAMPLING_RANK)


def score_sort_key(date: str) -> int:
    """日期的可比数字：**越新越小**，与排序键其余三段方向一致（越小越权威）。

    为什么不用日期字符串：字符串字典序是"越大越新"，与"越小越权威"方向相反，
    放进同一个元组里会让"取更新的那条"变成"取最旧的那条"——这是本次重构真实
    踩到的缺陷（有回归测试锁定）。缺失或无法解析的日期排最后（`NO_DATE_KEY`）。

    返回的是"距 2000-01-01 的天数取负"，因此新日期得到更小的值。
    """

    text = str(date or "").strip()
    if not text:
        return NO_DATE_KEY
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return (parsed - DATE_BASELINE).days * -1
    return NO_DATE_KEY


def rank_key(
    *,
    kind: str = "",
    identity: str = "",
    sampling: str = "",
    accredited: bool = False,
    tier: str = "",
    date: str = "",
) -> tuple[int, int, int, int]:
    """权威性排序键：身份 → 取样 → 资质 → 日期。**元组越小越权威。**

    设计要求（来自 2026-10-15 的重构要求）：

    1. 监管来源高于独立第三方这类结论必须**由排序规则保证**，
       不能依赖某个来源恰好拿到 1.00 分；
    2. 不同身份之间不存在"跨维度累计分数"导致的错误翻转——身份是元组第一位，
       低等级身份无论取样多好、资质多全，都不会超过高等级身份；
    3. 同级来源才比较取样、资质与日期；
    4. 四段方向一致：前三位"越小越优先"，日期用 `score_sort_key` 折算成
       "越新越小"，缺失日期排最后。缺字段、同级、异常输入都有稳定行为。
    """

    return (
        identity_rank_of(kind=kind, identity=identity, tier=tier),
        sampling_rank(sampling, identity),
        0 if accredited else 1,
        score_sort_key(date),
    )


def rank_key_for_row(row: Mapping[str, Any]) -> tuple[int, int, int, int]:
    """从一条结构化记录（事实行或证据 meta）取排序键。

    与 `grade_of` 走同一套分支：有维度信息按维度算，没有就按旧等级折算。
    检索、判定、高亮三处都必须走这一个函数，否则同一份证据在不同地方会被
    排出不同的优先级——这是之前真实发生过的 bug。
    """

    tier = str(row.get("tier", "")).upper()
    if not (row.get("kind") or row.get("identity")):
        return rank_key(
            identity=str(row.get("identity", "")),
            tier=tier,
            date=str(row.get("date", "")),
        )
    return rank_key(
        kind=str(row.get("kind", "")),
        identity=str(row.get("identity", "")),
        sampling=str(row.get("sampling", "")),
        accredited=bool(row.get("accredited", False)),
        tier=tier,
        date=str(row.get("date", "")),
    )


def _eligibility(identity: str, sampling: str, accredited: bool) -> tuple[bool, str]:
    """门槛：这条来源能不能单独支撑一个结论。

    这是分级的主判据，**权重不参与**这个判断（展示权重反过来由它的结论推出）。
    加权求和做不到这件事：在求和口径下"取样方式不明但有认可资质"与
    "厂商送检"都会落在同一条线附近，只要阈值定得低一点，两者都能越过，
    "送检样与市售样不是同一批"这条行业共识就等于没写。

    表与依据见 `文档库/02-判定规则/来源分级.md` 第二节。
    """

    if identity == "监管机构":
        return True, "监管机构公开信息"
    if identity == "平台机构":
        return True, "平台规则文本"
    if identity == "厂商自述":
        if sampling == "厂商送检":
            return False, "厂商送检材料：送检样与市售样不是同一批"
        return True, "厂商标称：可作为『厂商是否这样标称』的依据"
    if identity == "独立第三方":
        if sampling == "厂商送检":
            return False, "送检样与市售品不是同一批，不能单独支撑结论"
        if sampling == "市场随机购买":
            return True, "市场随机购买样本"
        if accredited:
            return True, "有认可资质"
        return True, "独立第三方（取样方式未说明）"
    if identity == "用户自述":
        return False, "用户内容不能单独支撑结论"
    return False, "来源身份未知，不能单独支撑结论"


def _display_weight(eligible: bool, identity: str) -> float:
    """展示权重：只由门槛结论与身份推三档，不叠加取样与资质的加减分。

    为什么是三档而不是加权求和：资质与取样方式已经在 `_eligibility` 里做过硬
    判断，再加权就是把同一个事实算两遍；而一旦允许累加，报告里就会出现
    "0.85 + 0.10" 这类**看起来像在算资格**的算式，误导读者以为资格是算出来的。
    """

    if not eligible:
        return WEIGHT_INELIGIBLE
    if identity == "厂商自述":
        # 厂商标称能证明"厂商是这么标称的"，是核查"是否夸大宣传"的基准，
        # 但它不能证明"事实如此"，所以与监管／独立第三方分档。
        return WEIGHT_VENDOR
    return WEIGHT_STRONG


def grade_source(
    *,
    kind: str = "",
    identity: str = "",
    sampling: str = "",
    accredited: bool = False,
    tier: str = "",
    date: str = "",
) -> SourceGrade:
    """给一条来源算出三层结论：门槛（strong）、展示权重（weight）、排序键（rank）。

    三层互不代偿：
    - `strong` 只看门槛；
    - `weight` 只由门槛结论与身份推出，取三档；
    - `rank` 用有序元组表达权威性，不经过任何分数。

    注意 `date` 会进入排序键的最后一段，**调用方要把证据的日期传进来**；
    留空会让"同权威取更新的那条"退化成"取先遇到的那条"（实测踩到过）。
    """

    resolved = identity_of(kind, identity)
    eligible, gate = _eligibility(resolved, sampling, accredited)
    weight = _display_weight(eligible, resolved)

    reasons: list[str] = [f"来源身份：{resolved}", gate]
    if sampling:
        reasons.append(f"取样方式：{sampling}")
    if accredited:
        reasons.append("有认可资质（只作为门槛判据，不额外加分）")
    reasons.append(
        f"展示权重 {weight:.2f}（{'过门槛' if eligible else '未过门槛'}，三档制，不参与资格判定）"
    )

    return SourceGrade(
        identity=resolved,
        sampling=sampling or "不明",
        accredited=accredited,
        tier=tier,
        weight=weight,
        strong=eligible,
        reasons=reasons,
        rank=rank_key(
            kind=kind,
            identity=resolved,
            sampling=sampling,
            accredited=accredited,
            tier=tier,
            date=date,
        ),
    )


def grade_of(item: Any) -> SourceGrade:
    """从一条证据上读出分级。维度信息挂在 meta 上。

    日期优先取证据对象自己的 `date`（`Evidence` 的字段），因为它才是这条证据的
    时间；meta 里的 date 是随维度信息一起带过来的副本，可能没有。**排序键的
    最后一段就是日期**，取错了会让"同权威取更新的那条"退化成"取先遇到的那条"。
    """

    meta: Mapping[str, Any] = getattr(item, "meta", None) or {}
    tier = str(getattr(item, "tier", "") or meta.get("tier", "")).upper()
    date = str(getattr(item, "date", "") or meta.get("date", "") or "")
    if not (meta.get("kind") or meta.get("identity")):
        # 没有维度信息（老数据或测试夹具）：退回按等级折算，而不是当成未知
        weight = TIER_FALLBACK_WEIGHT.get(tier, 0.35)
        return SourceGrade(
            identity="未知",
            sampling="",
            accredited=False,
            tier=tier,
            weight=weight,
            strong=weight >= STRONG_THRESHOLD,
            reasons=[f"没有来源维度信息，按等级 {tier or '无'} 折算 {weight:.2f}"],
            rank=rank_key(identity="未知", tier=tier, date=date),
        )
    return grade_source(
        kind=str(meta.get("kind", "")),
        identity=str(meta.get("identity", "")),
        sampling=str(meta.get("sampling", "")),
        accredited=bool(meta.get("accredited", False)),
        tier=tier,
        date=date,
    )


def weight_for_row(row: Mapping[str, Any]) -> float:
    """给一条结构化事实算展示权重。

    检索、判定、高亮三处都必须走这一个函数，否则同一份证据在不同地方会被
    排出不同的优先级——这是之前真实发生过的 bug。

    注意：**权威性比较不要用这个函数**，用 `rank_key_for_row()`。
    """

    if row.get("kind") or row.get("identity"):
        return grade_source(
            kind=str(row.get("kind", "")),
            identity=str(row.get("identity", "")),
            sampling=str(row.get("sampling", "")),
            accredited=bool(row.get("accredited", False)),
            tier=str(row.get("tier", "")),
            date=str(row.get("date", "")),
        ).weight
    return TIER_FALLBACK_WEIGHT.get(str(row.get("tier", "")).upper(), 0.35)


def independent_sources(items: Sequence[Any]) -> set[str]:
    """互相独立的来源。

    按**发布者**去重，不是按资料编号：同一家机构的两次检测、同一篇文章的两个
    转载版本，都只是一个来源。这是"独立来源数"能不能当判据的前提。
    """

    owners: set[str] = set()
    for item in items:
        if not grade_of(item).strong:
            continue
        meta: Mapping[str, Any] = getattr(item, "meta", None) or {}
        owner = str(meta.get("publisher", "")).strip()
        if not owner:
            # 发布者不明时不能声称"独立"：退回资料编号会把同一家机构的两份材料
            # 算成两个来源，而 E3（不少于 2 个独立来源）正是允许对外动作的门槛。
            # 宁可少算，不可多算。
            continue
        owners.add(owner)
    return owners

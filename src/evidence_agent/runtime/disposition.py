"""处置档位：从"有结论"到"该怎么办"的映射。

为什么需要这一层：`JUDGED` 只表示"有结论"，不表示"可放行"。一条被判
CONTRADICTED 的主张，可能只是无伤大雅的体验夸大，也可能涉及电池安全——
结论一样，处置完全不同。所以中间必须再过一次映射。

三个维度（详见 docs/16-backlog.md F 节）：

- **证据强度 E**：能不能动。依据是举证责任——平台做了对外动作就要能拿出证据，
  拿不出的处置在申诉和监管面前必输。所以 E1 及以下是硬门槛。
- **违规构成 V**：能怎么动。依据是平台规则文本。**必须"被证伪"才可能构成违规**，
  真实的主张不构成虚假宣传。
- **危害性质 H**：动多重。依据是处置成本与可逆性——降级分发可逆、用户无感；
  下架不可逆、会流失创作者。能用低档解决就不升档。

两条设计约束：

1. 这是**门槛制 + 梯度**，不是加权打分。若做成评分制，"证据不足但危害极大"会被
   平均成一个中等分然后放行——那是最危险的错误。
2. 顺序是 E → V → H，不能反。先看危害就会在没证据时动手。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..grading import grade_of, independent_sources
from .assessor import Assessment
from .states import TOOL_FAILED

# 证据强度
E_STRONG = "E3"
E_MEDIUM = "E2"
E_WEAK = "E1"
E_NONE = "E0"

# 危害性质
H_HIGH = "H3"
H_MEDIUM = "H2"
H_LOW = "H1"
H_NONE = "H0"

# 违规构成
V_VIOLATION = "V2"
V_SUSPECT = "V1"
V_NONE = "V0"

# 处置档位（按风险从低到高）
RELEASE = "release"           # 不改动，仅入库为判例
DOWNRANK = "downrank"         # 不进推荐池／搜索降权，用户无感
LABEL = "label"               # 在内容上打标提示读者
NOTIFY_CREATOR = "notify_creator"
VIOLATION = "violation"       # 转违规流程（下架／账号级），须证据链与可申诉
MANUAL = "manual"             # 转人工：证据不足，禁止一切对外动作

TIER_LABELS: dict[str, str] = {
    RELEASE: "放行",
    DOWNRANK: "降级分发",
    LABEL: "打标提示读者",
    NOTIFY_CREATOR: "通知创作者整改",
    VIOLATION: "转违规处置",
    MANUAL: "转人工",
}

#: 风险等级，只用于展示排序，不参与判定
TIER_RISK: dict[str, int] = {
    RELEASE: 0,
    MANUAL: 1,
    DOWNRANK: 2,
    LABEL: 3,
    NOTIFY_CREATOR: 4,
    VIOLATION: 5,
}

#: 需要人过手的档位
TIER_NEEDS_HUMAN: dict[str, bool] = {
    RELEASE: False,
    MANUAL: True,
    DOWNRANK: False,
    LABEL: False,
    NOTIFY_CREATOR: False,
    VIOLATION: True,
}

#: 对外动作：会改变分发、公开呈现或账号状态的处置。这三档在"依据只有厂商
#: 自述"时一律不触发——平台一旦动手就要拿得出可以摆到申诉与监管面前的依据，
#: 而厂商自述只能证明"厂商是这么说的"，不能证明"事实如此"。
#: 通知创作者整改不在其中：它是发给创作者的提示，不改变内容的分发与公开展示。
OUTWARD_TIERS: frozenset[str] = frozenset({DOWNRANK, LABEL, VIOLATION})

#: 涉及这些词的主张，判错后可能伤到人身或金钱
HARM_HIGH_WORDS: tuple[str, ...] = (
    "安全",
    "过热",
    "爆炸",
    "起火",
    "漏电",
    "有毒",
    "致癌",
    "过敏",
    "副作用",
    "儿童",
    "婴儿",
    "孕妇",
    "哺乳",
    "药品",
    "处方",
    "治疗",
    "疗效",
    "降压",
    "降糖",
    "医疗器械",
    "食品",
    "添加剂",
    "成分",
    "认证",
    "资质",
    "批准文号",
    "额定能量",
    "登机",
    "带上机",
    "飞机",
    "航空",
    "承重",
)

#: 影响消费决策，但通常不直接伤到人身
HARM_MEDIUM_WORDS: tuple[str, ...] = (
    "续航",
    "容量",
    "功率",
    "参数",
    "性能",
    "材质",
    "价格",
    "原价",
    "折扣",
    "保修",
    "售后",
    "适用",
    "型号",
    "充电",
    "防水",
)

#: 主观体验类，属于表达问题而不是事实问题
HARM_LOW_WORDS: tuple[str, ...] = (
    "好用",
    "推荐",
    "喜欢",
    "感觉",
    "体验",
    "值得",
    "舒服",
)

#: 带这些信号的内容，虚假表述更可能构成"虚假营销／虚构测评"
MARKETING_WORDS: tuple[str, ...] = (
    "推广",
    "广告",
    "带货",
    "合作",
    "赞助",
    "品牌方",
    "旗舰店",
    "购买链接",
    "优惠券",
    "种草",
    "测评",
    "素人",
)

#: claim_type 直接决定危害等级的映射
CLAIM_TYPE_HARM: dict[str, str] = {
    "health_safety": H_HIGH,
    "certification": H_HIGH,
}


@dataclass(frozen=True)
class Disposition:
    tier: str
    evidence: str
    harm: str
    violation: str
    rule: str
    reason: str
    notify_creator: bool = False
    priority: str = "normal"  # normal | high
    #: 承载结论的来源是否全部是厂商自述（限定了能否触发对外动作）
    vendor_only: bool = False

    @property
    def label(self) -> str:
        return TIER_LABELS.get(self.tier, self.tier)

    @property
    def needs_human(self) -> bool:
        return TIER_NEEDS_HUMAN.get(self.tier, True)

    @property
    def risk(self) -> int:
        return TIER_RISK.get(self.tier, 9)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "label": self.label,
            "evidence": self.evidence,
            "harm": self.harm,
            "violation": self.violation,
            "rule": self.rule,
            "reason": self.reason,
            "notify_creator": self.notify_creator,
            "priority": self.priority,
            "needs_human": self.needs_human,
            "vendor_only": self.vendor_only,
        }


def _tier_rank(tier: str) -> int:
    return {"A": 0, "B": 1, "C": 2, "D": 3}.get((tier or "").upper(), 9)


def evidence_strength(state: str, assessment: Assessment | None) -> str:
    """能不能动。E1 及以下禁止一切对外动作。"""

    if state == TOOL_FAILED or assessment is None:
        return E_NONE
    if not assessment.sufficient:
        return E_WEAK
    # 按发布者去重，不是按资料编号：同一家机构的两次检测只是一个来源
    return E_STRONG if len(independent_sources(assessment.items)) >= 2 else E_MEDIUM


def vendor_only_evidence(assessment: Assessment | None) -> bool:
    """承载结论的来源是否全部是厂商自述。

    看的是**承载结论的那几条**（assessment.decisive_sources），不是检索到的
    整堆材料：抽屉里放着别人的检测报告、结论却只引了厂商自己的页，这种情形
    仍然算"仅凭厂商自述"。来源身份读不出来（资料不在证据包里）时返回 False——
    门槛只在"确认全是厂商自述"时才拦，不靠猜。
    """

    if assessment is None:
        return False
    by_id = {item.source_id: item for item in assessment.items}
    decisive = [
        by_id[source_id]
        for source_id in (assessment.decisive_sources or ())
        if source_id in by_id
    ]
    items = decisive or list(assessment.items)
    if not items:
        return False
    return all(grade_of(item).identity == "厂商自述" for item in items)


def harm_level(claim_type: str, claim_text: str) -> str:
    """动了以后放多重。claim_type 优先，其次看词。"""

    if claim_type in CLAIM_TYPE_HARM:
        return CLAIM_TYPE_HARM[claim_type]
    text = claim_text or ""
    for word in HARM_HIGH_WORDS:
        if word in text:
            return H_HIGH
    # 先看有没有影响消费决策的要素，再看是不是纯体验表述。顺序反了会把
    # 容量 20000mAh、推荐 这类主张判成 H1（体验类），动作跟着变轻：
    # H1 落到 R10（只通知创作者），而 H2 该走 R8（降级分发 + 通知）。
    for word in HARM_MEDIUM_WORDS:
        if word in text:
            return H_MEDIUM
    for word in HARM_LOW_WORDS:
        if word in text:
            return H_LOW
    # 默认按影响消费决策处理：本场景下的主张大多落在这一档
    return H_MEDIUM


def violation_level(verdict: str, claim_text: str, content: str = "") -> str:
    """能怎么动。

    前提：**必须"被证伪"才可能构成违规**。真实的主张不构成虚假宣传，查不出
    结论的主张也不能被指控——否则就是拿不确定当罪名。
    """

    if verdict in ("SUPPORTED", "INSUFFICIENT"):
        return V_NONE
    marketing = any(
        word in text
        for text in (content or "", claim_text or "")
        for word in MARKETING_WORDS
    )
    if verdict == "CONTRADICTED":
        return V_VIOLATION if marketing else V_SUSPECT
    if verdict == "PARTIAL":
        return V_SUSPECT
    return V_NONE


def classify(
    *,
    state: str,
    verdict: str,
    claim_type: str,
    claim_text: str,
    assessment: Assessment | None,
    content: str = "",
    evidence_override: str | None = None,
) -> Disposition:
    """按 E → V → H 的顺序给出处置档位。规则不匹配时保守转人工。"""

    # evidence_override 是给"阈值判定吸收了来源冲突"这类情况用的：判定说结论
    # 成立，档位就不该再按"证据不足"算。调用方必须说明为什么覆盖。
    e = evidence_override or evidence_strength(state, assessment)
    h = harm_level(claim_type, claim_text)
    v = violation_level(verdict, claim_text, content)
    vendor_only = vendor_only_evidence(assessment)

    def make(tier: str, rule: str, reason: str, **kwargs: Any) -> Disposition:
        if tier in OUTWARD_TIERS and vendor_only:
            # 结论可以照常给出（写在报告里），但动作止步于"交给人"。
            return Disposition(
                tier=MANUAL,
                evidence=e,
                harm=h,
                violation=v,
                rule="R13",
                reason="依据只有厂商自述：可以记录结论，但不能触发对外动作，先交人工确认",
                vendor_only=True,
                priority=kwargs.get("priority", "normal"),
            )
        return Disposition(
            tier=tier,
            evidence=e,
            harm=h,
            violation=v,
            rule=rule,
            reason=reason,
            vendor_only=vendor_only,
            **kwargs,
        )

    if e == E_NONE:
        return make(MANUAL, "R1", "工具不可用，本次核查不成立")
    if e == E_WEAK:
        if h == H_HIGH:
            return make(
                MANUAL,
                "R2",
                "证据不足且涉及高风险主题，必须人工判断",
                priority="high",
            )
        return make(MANUAL, "R3", "证据不足，禁止一切对外动作")
    if verdict == "SUPPORTED":
        return make(RELEASE, "R4", "主张与最权威来源一致，不需要处置")
    if verdict == "INSUFFICIENT" and e in (E_STRONG, E_MEDIUM):
        # 证据够了但系统没能给出结论（规则判不了、又没配模型）。这是能力缺口，
        # 不是判断结果——不能对外做任何动作，必须交人。
        return make(
            MANUAL,
            "R12",
            "证据充足但系统未能给出结论（缺模型判定），交人工判断",
        )
    if v == V_VIOLATION:
        return make(
            VIOLATION,
            "R5",
            "被证伪且带营销特征，可能构成虚假营销",
            notify_creator=True,
        )
    if v == V_SUSPECT and h == H_HIGH:
        if e == E_STRONG:
            return make(
                LABEL,
                "R6",
                "被证伪且涉及高风险主题，但未构成明确违规：打标提示读者",
            )
        return make(MANUAL, "R7", "涉及高风险主题但证据只有单一来源，先人工确认")
    if v == V_SUSPECT and h == H_MEDIUM:
        if e == E_STRONG:
            return make(
                DOWNRANK,
                "R8",
                "被证伪且影响消费决策：降级分发并通知创作者整改",
                notify_creator=True,
            )
        return make(MANUAL, "R9", "证据只有单一来源，不足以支撑分发动作")
    if v == V_SUSPECT:
        return make(
            NOTIFY_CREATOR,
            "R10",
            "被证伪但属体验类表述，提示创作者修改即可",
            notify_creator=True,
        )
    return make(MANUAL, "R11", "没有匹配到处置规则，保守转人工")


def distribution(dispositions: Sequence[Disposition]) -> dict[str, int]:
    """按档位统计，用于报告与评测。"""

    out: dict[str, int] = {}
    for item in dispositions:
        out[item.tier] = out.get(item.tier, 0) + 1
    return out

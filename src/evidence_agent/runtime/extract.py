"""把内容拆成待核查主张：规则抽取，不读标注。

评测缺口任务时不能拿标注主张当输入——那等于把答案抄进题面，所有指标都会虚高。
所以运行前必须先拆。这里提供一个确定的规则基线，**它的错误率本身就是评测对象**。
接真实模型时用 harness 的 --extract model，走 prompts.extract_messages。

规则（来自 docs/02-labeling-rules.md 的拆解规则，不来自标注答案）：

1. 先按句切分（。！？；换行、冒号），再按逗号切分子句；
2. **一条子句一条主张**——标注就是这个粒度：GAP-003 的一句话在标注里是两条主张；
3. 必须带可核查信号：带单位的数值，或认证／资质／排他／安全隐患这类断言词；
4. 没有数字的纯主观子句丢掉："续航表现不错"是表达，不是事实。注意按**子句**判断：
   同一句里前半句主观、后半句可核查时只丢前半句；整段过滤会把可核查的那半句一起丢掉，
   这是这一版真实踩到的坑；
5. 去重，最多保留 max_claims 条。

已知局限（写清楚，别让指标看起来比实际好）：

- "而且""并且"连接的并列判断仍可能留在一条里（没有做句法分析）；
- 指代不消解（"这款""该产品"这类主语还原不了）；
- 实体的识别交给下游策略层，这里不做。
"""

from __future__ import annotations

import re

SEGMENT_SPLIT = re.compile(r"[。！？；：\n]")

#: 带单位的数值才算可核查的量。"P20" 这种型号里的数字不算。
#: 比例写法（1:1、5:1）也算——净水器的废水比、功效比都是这个形态，
#: 第一版只认"数字+单位"，这类整条被丢掉了。
NUMBER_WITH_UNIT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:[A-Za-z]+|小时|个月|分钟|天|年|次|%|℃|平方米|平米)"
    r"|\d+\s*[:：]\s*\d+"
)

#: 这几类词意味着句子在下一个可核查的事实判断
ASSERTION_HINTS: tuple[str, ...] = (
    "认证",
    "资质",
    "专利",
    "名录",
    "批准文号",
    "唯一",
    "第一",
    "首款",
    "独家",
    "全系",
    "史上",
    "最强",
    "最高",
    "最低",
    "最大",
    "安全",
    "隐患",
    "超标",
    "有毒",
    "无害",
    "无毒",
    "零添加",
    "无添加",
    "未添加",
    "可以带上",
    "可随身携带",
    "无需申报",
)

#: 纯主观表述：没有数字时就只是表达，不是可核查判断
SUBJECTIVE_WORDS: tuple[str, ...] = (
    "好用",
    "喜欢",
    "不错",
    "舒服",
    "体验",
    "感觉",
    "觉得",
    "满意",
    "值得",
)

#: 分句用：先切段，再切子句
CLAUSE_SPLIT = re.compile(r"[，,、;；]")

#: 去掉子句两端的标点与空白
STRIP_CHARS = " ，、,.;：:。！？"

MIN_LENGTH = 6


def _clause_is_claimable(clause: str) -> bool:
    if len(clause) < MIN_LENGTH:
        return False
    has_number = bool(NUMBER_WITH_UNIT.search(clause))
    has_assertion = any(word in clause for word in ASSERTION_HINTS)
    if not has_number and not has_assertion:
        return False
    if not has_number and any(word in clause for word in SUBJECTIVE_WORDS):
        return False
    return True


def is_claimable(text: str) -> bool:
    """这一段值不值得当成一条主张。

    按**逗号分句**判断，但整段作为一条主张返回：同一句里"续航表现不错，还获得过认证"
    只有后半句可核查，前半句是主观表达——按整段过滤会把可核查的那半句一起丢掉
    （这是这一版真实踩到的坑：GAP-002 的认证句就是这么被丢掉的）。
    """

    part = (text or "").strip()
    if len(part) < MIN_LENGTH:
        return False
    clauses = [clause.strip() for clause in CLAUSE_SPLIT.split(part)]
    return any(_clause_is_claimable(clause) for clause in clauses)


def extract_claim_texts(content: str, max_claims: int = 5) -> list[str]:
    """把一段内容拆成待核查主张。确定、可复现、无标注依赖。

    粒度：先按句切分，再按逗号切子句，**一条子句一条主张**。这个粒度不是随手定的：
    标注规则要求"一条主张只包含一个可验证的核心判断"，标注本身也是这个粒度。
    切粗了会让下游的指标识别跟着错——"刷牙震感舒服，续航表现不错，还获得过认证"
    当成一条时，指标会被认成"续航"，冲突判定于是用错了指标（真实踩到的坑）。
    """

    out: list[str] = []
    for segment in SEGMENT_SPLIT.split(content or ""):
        for clause in CLAUSE_SPLIT.split(segment):
            text = clause.strip(STRIP_CHARS)
            if not _clause_is_claimable(text):
                continue
            if text in out:
                continue
            out.append(text)
            if len(out) >= max(1, int(max_claims)):
                return out
    return out

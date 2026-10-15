"""标出主张里需要人盯住的片段。

两类片段值得高亮，理由不同：

1. **数值与单位**——可以被客观比对。证据里有同一单位的权威值时就直接算偏差：
   一致标绿，不一致标红。这是唯一能自动判定的一类片段。
2. **断言词**——"唯一""全系""认证""治疗"这类词本身没错，但最容易省略限定条件，
   是人工复核的重点。系统只能提醒"这里有断言"，不能替人判断。

高亮只影响提示，不改变结论——结论仍然由运行时的规则给。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .grading import STRONG_THRESHOLD, rank_key_for_row, weight_for_row

NUMBER = re.compile(r"\d+(?:\.\d+)?\s*(?:[A-Za-z]+|小时|个月|分钟|天|年|次|%)")

ASSERTION_WORDS: tuple[str, ...] = (
    "唯一",
    "全系",
    "首款",
    "史上",
    "所有型号",
    "认证",
    "资质",
    "治疗",
    "根治",
    "改善",
    "无毒",
    "无害",
    "零添加",
    "绝对",
    "100%",
    # 许可与能力类表述：这类说法可以被核实（对照规定或规格），
    # 但既没有数字也不是"唯一"这类排他词，所以单独列出来。
    "可以带上飞机",
    "可以带上机",
    "可随身携带",
    "无需申报",
    "支持",
    "兼容",
    "适用于",
)

#: 兜底容差。正常情况下按指标类型取，与运行时判定共用同一张表。
DEFAULT_TOLERANCE = 0.10


def _tolerance(metric: str | None) -> float:
    """容差必须与判定用同一张表，否则界面说"一致"、结论说"冲突"。"""

    from .runtime.assessor import tolerance_for

    return tolerance_for(metric or "") if metric else DEFAULT_TOLERANCE


@dataclass(frozen=True)
class Highlight:
    text: str
    start: int
    end: int
    kind: str  # number-ok | number-bad | number | assertion
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "note": self.note,
        }


def split_number(token: str) -> tuple[float, str]:
    match = re.match(r"(\d+(?:\.\d+)?)\s*(.*)", token)
    if not match:
        return 0.0, ""
    return float(match.group(1)), match.group(2).strip()


def authoritative_value(
    facts: Iterable[Mapping[str, Any]], unit: str
) -> tuple[float, Mapping[str, Any]] | None:
    """在同单位的记录里挑最权威的一条。

    权威性走 `grading.rank_key_for_row`（身份 → 取样 → 资质 → 日期，越小越权威），
    与运行时判定用的是同一套口径——两处用不同排序会让同一份证据在界面和结论里
    得到不同的解释，这是真实发生过的 bug。

    **不要用展示权重做这件事**：权重是三档展示值，用它比大小会让"谁更权威"
    变成数值巧合。
    """

    best: tuple[float, Mapping[str, Any]] | None = None
    best_key: tuple[int, int, int, int] | None = None
    for row in facts:
        if str(row.get("unit", "")).lower() != unit.lower():
            continue
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        key = rank_key_for_row(row)
        # 排序键越小越权威；日期段已经折算成"越新越小"，所以直接比较即可。
        if best_key is None or key < best_key:
            best, best_key = (value, row), key
    return best


def _strong_rows(
    facts: Sequence[Mapping[str, Any]], unit: str, metric: str | None = None
) -> list[Mapping[str, Any]]:
    """同单位（且同指标）的 A／B 级记录。

    指标过滤不能省：民航规定那条限额也是"Wh"，但它说的是"上限 100Wh"，
    不是这台设备的测量值。只按单位过滤会把它当成分歧的一方。
    """

    rows = [
        row
        for row in facts
        if str(row.get("unit", "")).lower() == unit.lower()
        and (metric is None or str(row.get("metric", "")) == metric)
    ]
    # 这里筛的是"够格的来源"（门槛结论），不是权威性排序。
    # 用展示权重与 STRONG_THRESHOLD 比较是既有的兼容口径：语料里的记录过门槛即 0.90、
    # 未过门槛即 0.40，两者不会混淆。权威性排序另走 rank_key_for_row。
    strong = [row for row in rows if weight_for_row(row) >= STRONG_THRESHOLD]
    return strong or rows


def number_verdict(
    value: float,
    unit: str,
    facts: Sequence[Mapping[str, Any]],
    metric: str | None = None,
) -> tuple[str, str]:
    """给一个数值片段定性，返回 (kind, note)。

    三类结果，与运行时的判定口径保持一致：
    - `number-conflict`：可信来源之间本身就打架，此时不该说这个数值"对"或"错"；
    - `number-ok` / `number-bad`：与最权威来源一致／不一致。
    """

    strong = _strong_rows(facts, unit, metric)
    found = authoritative_value(strong, unit)
    if found is None:
        return "number", "证据里没有同单位的记录，需要人工确认"

    target, row = found
    source_id = str(row.get("source_id", ""))
    tolerance = _tolerance(metric)
    others = [
        float(other.get("value"))
        for other in strong
        if str(other.get("source_id", "")) != source_id
        and str(other.get("value", "")).strip()
    ]
    if target:
        conflicted = any(
            abs(other - target) / abs(target) > tolerance for other in others
        )
        if conflicted:
            shown = " / ".join(f"{item:g}{unit}" for item in sorted({target, *others}))
            return "number-conflict", f"可信来源之间不一致（{shown}），需要人工确认"
        deviation = abs(value - target) / abs(target)
        if deviation <= tolerance:
            return "number-ok", f"与最权威来源一致（{target:g}{unit}，{source_id}）"
        return (
            "number-bad",
            f"与最权威来源相差 {deviation:.0%}（{target:g}{unit}，{source_id}）",
        )
    return "number", "证据里没有同单位的记录，需要人工确认"


def highlights_for(
    claim_text: str, facts: Sequence[Mapping[str, Any]] = ()
) -> list[Highlight]:
    text = claim_text or ""
    out: list[Highlight] = []

    for match in NUMBER.finditer(text):
        token = match.group()
        value, unit = split_number(token)
        if unit:
            kind, note = number_verdict(
                value, unit, facts, metric_near(text, match.start(), match.end())
            )
        else:
            kind, note = "number", "证据里没有同单位的记录，需要人工确认"
        out.append(Highlight(token, match.start(), match.end(), kind, note))

    for word in ASSERTION_WORDS:
        start = text.find(word)
        while start >= 0:
            out.append(
                Highlight(
                    word,
                    start,
                    start + len(word),
                    "assertion",
                    "断言类表述，最容易省略限定条件",
                )
            )
            start = text.find(word, start + 1)

    # 同一位置命中多个词时，让更长的那个胜出（"可以带上飞机" 优于 "可以带上"）。
    # 前端会跳过与前一个重叠的标记，所以顺序决定谁被保留。
    out.sort(key=lambda item: (item.start, -item.end))
    return out


def metric_near(text: str, start: int, end: int, window: int = 12) -> str | None:
    """找离这个数值最近的指标词。

    整句只推一个指标是不够的：一段话里可能既有"20000mAh 容量"又有"额定能量
    74Wh"，两句用的是不同指标。按最近距离取，才能各归各位。
    """

    from .runtime.planner import METRIC_KEYWORDS

    lowered = text.lower()
    best: tuple[int, str] | None = None
    for metric, words in METRIC_KEYWORDS:
        for word in words:
            index = lowered.find(word)
            while index >= 0:
                word_end = index + len(word)
                if word_end <= start:
                    distance = start - word_end
                elif index >= end:
                    distance = index - end
                else:
                    distance = 0
                if best is None or distance < best[0]:
                    best = (distance, metric)
                index = lowered.find(word, index + 1)
    if best is None or best[0] > window:
        return None
    return best[1]

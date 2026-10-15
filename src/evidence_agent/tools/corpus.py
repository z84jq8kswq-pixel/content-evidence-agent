"""受控离线语料：让"自主检索"在本地可复现。

不做真实爬虫（PRD 2.2 的非目标）。语料里刻意包含噪音、互相冲突和过时资料
——否则"自主检索"会退化成固定查找，v3 的叙事就不成立了（风险 U4）。

检索是纯标准库实现的：中文按二元组切分，英文与数字按整词，标题权重加倍。
结果可复现，不依赖向量库或嵌入模型。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .base import Evidence

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
WORD_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# 数值＋单位的写法要拆出纯数字，否则用户写 "20000" 检索不到 "20000mAh"
NUMBER_PREFIX = re.compile(r"^(\d+)[a-z]+$")


class CorpusError(RuntimeError):
    pass


def terms(text: str, n: int = 2) -> list[str]:
    """把文本切成检索词：英文数字整词，中文 n 元组。"""

    lowered = (text or "").lower()
    out: list[str] = []
    for match in WORD_RUN.finditer(lowered):
        token = match.group()
        out.append(token)
        prefix = NUMBER_PREFIX.match(token)
        if prefix:
            out.append(prefix.group(1))
    for match in CJK_RUN.finditer(lowered):
        run = match.group()
        # 单个汉字不做检索词。"2024 年 12 月"里的"年""月"会单独成段，把它们
        # 当成词会让任何提到年份的资料都命中，属于假命中。
        if len(run) < 2:
            continue
        if len(run) <= n:
            out.append(run)
        else:
            out.extend(run[i : i + n] for i in range(len(run) - n + 1))
    return out


def score(query: str, title: str, text: str) -> float:
    """查询与文档的相关度。标题命中算双倍。"""

    query_terms = Counter(terms(query))
    if not query_terms:
        return 0.0
    title_terms = Counter(terms(title))
    body_terms = Counter(terms(text))

    total = 0.0
    for term, weight in query_terms.items():
        hit = body_terms.get(term, 0) + 2 * title_terms.get(term, 0)
        if hit:
            total += weight * (1.0 + math.log(hit))
    return round(total, 4)


@dataclass(frozen=True)
class CorpusDoc:
    id: str
    title: str
    tier: str
    date: str
    publisher: str
    text: str
    #: 来源类型。回答"这条材料是谁提供的"：品牌官方声明／独立检测／
    #: 监管公开库／行业组织名录／平台规则／站内用户内容／站内营销内容。
    kind: str = ""
    #: 别名关键词。文档正文用的是书面语（"乘机""可随身携带"），用户说的是
    #: 口语（"带上飞机"），字面检索够不着。这里补上同义说法。
    keywords: tuple[str, ...] = ()
    #: 取样方式与资质。来源分级用，见 evidence_agent/grading.py。
    sampling: str = ""
    accredited: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CorpusDoc":
        return cls(
            id=str(raw["id"]),
            title=str(raw.get("title", "")),
            tier=str(raw.get("tier", "")),
            date=str(raw.get("date", "")),
            publisher=str(raw.get("publisher", "")),
            text=str(raw.get("text", "")),
            kind=str(raw.get("kind", "")),
            keywords=tuple(str(k) for k in raw.get("keywords", [])),
            sampling=str(raw.get("sampling", "")),
            accredited=bool(raw.get("accredited", False)),
        )

    def meta(self) -> dict[str, Any]:
        """给证据片段用的来源元信息（不含实体与指标）。"""

        from ..grading import identity_of

        return {
            "source_id": self.id,
            "publisher": self.publisher,
            "kind": self.kind,
            "identity": identity_of(self.kind),
            "sampling": self.sampling,
            "accredited": self.accredited,
            "tier": self.tier,
            "date": self.date,
        }


class LocalCorpus:
    """离线资料库，供 search_notes / lookup_fact / fetch_source 使用。"""

    def __init__(
        self,
        docs: Iterable[CorpusDoc],
        facts: Iterable[Mapping[str, Any]] = (),
        aliases: Mapping[str, Sequence[str]] | None = None,
        version: str = "",
    ) -> None:
        self.version = version
        self.docs: dict[str, CorpusDoc] = {}
        for doc in docs:
            if doc.id in self.docs:
                raise CorpusError(f"语料 id 重复：{doc.id}")
            self.docs[doc.id] = doc
        self.facts: list[dict[str, Any]] = [dict(row) for row in facts]
        self.aliases: dict[str, list[str]] = {
            str(entity): [str(a) for a in alias_list]
            for entity, alias_list in (aliases or {}).items()
        }

    # --- 载入 ---------------------------------------------------------

    @classmethod
    def from_dir(cls, path: str | Path) -> "LocalCorpus":
        root = Path(path)
        sources_file = root / "sources.json"
        if not sources_file.exists():
            raise CorpusError(f"语料目录缺少 sources.json：{root}")
        raw = json.loads(sources_file.read_text(encoding="utf-8"))
        docs = [CorpusDoc.from_dict(item) for item in raw.get("sources", [])]

        facts: list[Mapping[str, Any]] = []
        facts_file = root / "facts.json"
        if facts_file.exists():
            facts = json.loads(facts_file.read_text(encoding="utf-8")).get("facts", [])

        aliases = raw.get("aliases", {})
        corpus = cls(docs, facts, aliases, version=str(raw.get("corpus_version", "")))
        corpus.validate()
        return corpus

    def validate(self) -> None:
        for row in self.facts:
            source_id = str(row.get("source_id", ""))
            if source_id and source_id not in self.docs:
                raise CorpusError(f"事实引用了不存在的资料：{source_id}")

    # --- 检索 ---------------------------------------------------------

    def search(self, query: str, top_k: int = 5) -> list[Evidence]:
        scored: list[tuple[float, CorpusDoc]] = []
        for doc in self.docs.values():
            value = score(query, doc.title, doc.text)
            if doc.keywords:
                value = max(value, score(query, "", " ".join(doc.keywords)))
            if value > 0:
                scored.append((value, doc))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))

        return [
            Evidence(
                source_id=doc.id,
                tier=doc.tier,
                date=doc.date,
                text=doc.text,
                score=value,
                meta=self.evidence_meta(doc),
            )
            for value, doc in scored[: max(1, int(top_k))]
        ]

    # --- 事实查询 -----------------------------------------------------

    def resolve_entity(self, entity: str) -> list[str]:
        """把别名归一成规范实体名。返回空列表表示认不出来。"""

        target = (entity or "").strip().lower()
        if not target:
            return []
        matched: list[str] = []
        for canonical, alias_list in self.aliases.items():
            pool = [canonical, *alias_list]
            if any(target == str(item).strip().lower() for item in pool):
                matched.append(canonical)
        if matched:
            return matched
        # 退一步：包含关系，避免"青禾X1牙刷"这类自然表述查不到
        for canonical, alias_list in self.aliases.items():
            pool = [canonical, *alias_list]
            if any(str(item).lower() in target for item in pool):
                matched.append(canonical)
        return matched

    def lookup_fact(
        self,
        entity: str,
        metric: str | None = None,
        period: str | None = None,
    ) -> list[dict[str, Any]]:
        entities = self.resolve_entity(entity) or [entity.strip()]
        rows: list[dict[str, Any]] = []
        for row in self.facts:
            if str(row.get("entity", "")) not in entities:
                continue
            if metric and metric.strip() not in str(row.get("metric", "")):
                continue
            if period and not str(row.get("period", "")).startswith(period.strip()):
                continue
            rows.append(self._enriched_fact(row))
        rows.sort(key=lambda row: str(row.get("date", "")), reverse=True)
        return rows

    def _enriched_fact(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """给一条事实行补上它所属资料的来源维度。"""

        enriched = dict(row)
        doc = self.docs.get(str(row.get("source_id", "")))
        if doc is not None:
            enriched["publisher"] = doc.publisher
            enriched["title"] = doc.title
            enriched["kind"] = doc.kind
            enriched["date"] = enriched.get("date") or doc.date
            enriched["tier"] = enriched.get("tier") or doc.tier
            if not enriched.get("sampling"):
                enriched["sampling"] = doc.sampling
            if "accredited" not in enriched:
                enriched["accredited"] = doc.accredited
        return enriched

    def facts_for(self, source_id: str) -> list[dict[str, Any]]:
        """这份资料在事实库里的全部结构化记录。"""

        target = (source_id or "").strip()
        return [
            self._enriched_fact(row)
            for row in self.facts
            if str(row.get("source_id", "")) == target
        ]

    def evidence_meta(self, doc: CorpusDoc) -> dict[str, Any]:
        """检索到的证据应该带的元信息。

        除了来源维度，还要带上这份资料在事实库里的结构化记录。原因：assess 的冲突与
        过时判定按实体与指标分组，只认结构化数值。检索路径上不把事实带出来，同一份
        资料用 lookup_fact 拿到、与用 search_notes 或 fetch_source 拿到，会得到完全
        不同的判定——同一份证据在不同路径上被排出不同优先级，这是真实发生过的 bug。
        """

        meta: dict[str, Any] = doc.meta()
        facts = self.facts_for(doc.id)
        if facts:
            meta["facts"] = facts
        return meta

    def get_doc(self, source_id: str) -> CorpusDoc | None:
        return self.docs.get((source_id or "").strip())

    def enriched_facts(self) -> list[dict[str, Any]]:
        """带上来源维度的事实行。

        检索、判定、高亮都用这一份，保证三处对"哪条更权威"的判断一致。
        """

        rows: list[dict[str, Any]] = []
        for row in self.facts:
            enriched = dict(row)
            doc = self.docs.get(str(row.get("source_id", "")))
            if doc is not None:
                enriched.update({k: v for k, v in doc.meta().items() if k not in enriched})
                enriched["tier"] = row.get("tier") or doc.tier
                enriched["date"] = row.get("date") or doc.date
                enriched["publisher"] = doc.publisher
                enriched["kind"] = doc.kind
            rows.append(enriched)
        return rows

    def locate(self, source_id: str, hint: str) -> tuple[str, tuple[int, int] | None]:
        """在资料里定位片段。定位不到时返回整段，由调用方决定是否降级。"""

        doc = self.docs.get(source_id)
        if doc is None:
            return "", None
        needle = (hint or "").strip()
        if not needle:
            return doc.text, None
        index = doc.text.find(needle)
        if index >= 0:
            return needle, (index, index + len(needle))
        # 定位不到就退回最相关的句子，并在调用方标记为降级
        best, best_score = "", 0.0
        for sentence in re.split(r"[。；\n]", doc.text):
            value = score(needle, "", sentence)
            if value > best_score:
                best, best_score = sentence.strip(), value
        if best:
            index = doc.text.find(best)
            return best, (index, index + len(best)) if index >= 0 else None
        return doc.text, None

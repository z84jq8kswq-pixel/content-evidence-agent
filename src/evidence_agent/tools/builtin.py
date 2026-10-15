"""Week 1 的三个内置工具。规格与 docs/10-prd-agent-v3.md 第 5.3 节逐字对齐。

| 工具 | 超时 | 重试 | 降级 |
|---|---|---|---|
| search_notes | 8s | 2 | lookup_fact |
| lookup_fact | 5s | 2 | 无（转人工） |
| fetch_source | 10s | 1 | 退回摘要，标 LOW_SOURCE_QUALITY |
"""

from __future__ import annotations

from typing import Any

from .base import (
    STATUS_BAD_REQUEST,
    STATUS_EMPTY,
    STATUS_OK,
    Evidence,
    Outcome,
    ParamSpec,
    Tool,
    ToolSpec,
)
from .corpus import LocalCorpus


class SearchNotesTool(Tool):
    """语料内相关讨论与资料检索。"""

    spec = ToolSpec(
        name="search_notes",
        purpose="按关键词在受控语料中检索相关片段",
        params={
            "query": ParamSpec("string", True, "检索词，建议包含实体与指标"),
            "top_k": ParamSpec("int", False, "返回条数，默认 5", default=5),
        },
        returns="证据片段列表（source_id、tier、date、相似度）",
        timeout_s=8.0,
        max_retries=2,
        fallback="lookup_fact",
        cost_per_call=0.0,
    )

    def __init__(self, corpus: LocalCorpus) -> None:
        self._corpus = corpus

    def _run(self, params: dict[str, Any]) -> Outcome:
        items = self._corpus.search(params["query"], int(params.get("top_k") or 5))
        if not items:
            return Outcome(STATUS_EMPTY)
        return Outcome(STATUS_OK, items=items)


class LookupFactTool(Tool):
    """事实与数值库精确查询。查不到就是查不到，不做模糊兜底。"""

    spec = ToolSpec(
        name="lookup_fact",
        purpose="按实体与指标查询结构化事实与数值",
        params={
            "entity": ParamSpec("string", True, "实体名，支持别名"),
            "metric": ParamSpec("string", False, "指标名，如续航／额定能量", default=None),
            "period": ParamSpec("string", False, "时间范围，如 2025", default=None),
        },
        returns="事实行（value、unit、period、来源与日期）",
        timeout_s=5.0,
        max_retries=2,
        fallback=None,
        cost_per_call=0.0,
    )

    def __init__(self, corpus: LocalCorpus) -> None:
        self._corpus = corpus

    def _run(self, params: dict[str, Any]) -> Outcome:
        rows = self._corpus.lookup_fact(
            params["entity"], params.get("metric"), params.get("period")
        )
        if not rows:
            return Outcome(STATUS_EMPTY)
        items = [
            Evidence(
                source_id=str(row.get("source_id", "")),
                tier=str(row.get("tier", "")),
                date=str(row.get("date", "")),
                text=(
                    f"{row.get('entity')} {row.get('metric')}："
                    f"{row.get('value')}{row.get('unit', '')}"
                    f"（{row.get('period', '时间未标注')}，"
                    f"{row.get('publisher', '出处未标注')}）"
                ),
                meta=dict(row),
            )
            for row in rows
        ]
        return Outcome(STATUS_OK, items=items)


class FetchSourceTool(Tool):
    """取原文并按提示定位段落。定位失败不报错，降级返回最相关片段。"""

    spec = ToolSpec(
        name="fetch_source",
        purpose="按资料 id 取回原文，并定位到具体段落",
        params={
            "source_id": ParamSpec("string", True, "资料 id，如 S-001"),
            "span_hint": ParamSpec("string", False, "想定位的片段提示", default=None),
        },
        returns="段落文本与字符偏移；定位失败时返回摘要并标记降级",
        timeout_s=10.0,
        max_retries=1,
        fallback=None,
        cost_per_call=0.0,
    )

    def __init__(self, corpus: LocalCorpus) -> None:
        self._corpus = corpus

    def _run(self, params: dict[str, Any]) -> Outcome:
        doc = self._corpus.get_doc(params["source_id"])
        if doc is None:
            return Outcome(STATUS_EMPTY, error=f"资料不存在：{params['source_id']}")

        hint = params.get("span_hint")
        text, span = self._corpus.locate(doc.id, hint or "")
        degraded = bool(hint) and span is None
        if degraded:
            text = text[:200]
        item = Evidence(
            source_id=doc.id,
            tier=doc.tier,
            date=doc.date,
            text=text,
            span=span,
            # 来源维度不能因为换了个工具就丢掉：丢了会退回按等级折算，
            # 同一份资料在检索路径和取原文路径上会拿到不同的权威权重。
            meta=self._corpus.evidence_meta(doc),
        )
        return Outcome(
            STATUS_OK,
            items=[item],
            degraded=degraded,
            degrade_reason="UNLOCATED_SPAN" if degraded else None,
        )


class FetchWebTool(Tool):
    """联网参考材料获取（影子模式）。

    两种模式分开，语义不混：`url` 取一个明确给出的公开地址，`search` 按查询词
    发现候选再取回。**这个工具不注册进 Agent 的工具集**（`build_tools` 里没有它），
    所以它不可能参与自动判定；注册它是为了让工作台按同一套工具协议调用，
    并让它的超时、重试、降级、成本在工具页上可见。
    """

    spec = ToolSpec(
        name="fetch_web",
        purpose="获取公开网页原文并保存快照，作为人工参考材料（不参与自动研判）",
        params={
            "mode": ParamSpec(
                "string",
                True,
                "url=获取明确地址；search=按查询词发现（需搜索 API 密钥）",
                default="url",
            ),
            "url": ParamSpec("string", False, "mode=url 时的目标地址", default=None),
            "query": ParamSpec("string", False, "mode=search 时的查询词", default=None),
            "limit": ParamSpec("int", False, "最多取回几条（search 模式）", default=3),
        },
        returns="参考材料列表（状态、标题、域名、取回时间、正文、快照与哈希）",
        timeout_s=20.0,
        max_retries=0,
        fallback=None,
        cost_per_call=0.0,
        read_only=True,
    )

    def __init__(self, shadow: Any) -> None:
        self._shadow = shadow

    def _run(self, params: dict[str, Any]) -> Outcome:
        mode = str(params.get("mode") or "url").strip().lower()
        if mode not in ("url", "search"):
            return Outcome(STATUS_BAD_REQUEST, error=f"mode 只能是 url 或 search：{mode}")

        if mode == "url":
            url = str(params.get("url") or "").strip()
            if not url:
                return Outcome(STATUS_BAD_REQUEST, error="mode=url 需要 url 参数")
            references = self._shadow.fetch_urls([url])
        else:
            query = str(params.get("query") or "").strip()
            if not query:
                return Outcome(STATUS_BAD_REQUEST, error="mode=search 需要 query 参数")
            references = self._shadow.discover(query)

        payload = self._shadow.references_payload(references)
        # 工具结果里带 items 是为了让 Tool Registry 的既有统计（条数、引用编号）
        # 能正常工作；这些 items 只用于展示，**不会**进入 Agent 的证据包。
        items = [
            Evidence(
                source_id=f"WEB-{index + 1}",
                tier="",
                date=str(row.get("fetched_at", ""))[:10],
                text=str(row.get("text", ""))[:500],
                meta={
                    "web_reference": True,
                    "status": row.get("status"),
                    "url": row.get("url"),
                    "domain": row.get("domain"),
                    "snapshot": row.get("snapshot"),
                    "raw_sha256": row.get("raw_sha256"),
                },
            )
            for index, row in enumerate(payload["items"])
        ]
        status = STATUS_OK if any(row["ok"] for row in payload["items"]) else STATUS_EMPTY
        return Outcome(
            status,
            items=items,
            error="；".join(
                row["error"] for row in payload["items"] if not row["ok"]
            )[:400] or None,
            degraded=any(not row["ok"] for row in payload["items"]),
            degrade_reason="PARTIAL_REFERENCE_FAILURE" if any(not row["ok"] for row in payload["items"]) else None,
        )


def build_tools(corpus: LocalCorpus) -> list[Tool]:
    """Agent 的工具集。

    **刻意不含 `fetch_web`**：影子模式的联网材料不参与自动判定，
    放进这个列表就等于让运行时能用它下结论。工作台走
    `build_shadow_tools()` 单独装配（见 `runtime/workbench.py`）。
    """

    return [SearchNotesTool(corpus), LookupFactTool(corpus), FetchSourceTool(corpus)]


def build_shadow_tools(shadow: Any) -> list[Tool]:
    """影子模式工具集：目前只有 `fetch_web`，不进 Agent。"""

    return [FetchWebTool(shadow)]

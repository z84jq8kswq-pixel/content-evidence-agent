"""内容研判工作台的服务层：把一次真实运行变成界面能直接用的数据。

这一层的职责边界要写清楚，否则很容易变成"第二个业务实现"：

1. **不重新判定**。结论、标志位、处置档位全部来自 `EvidenceAgent` 与
   `runtime.disposition`，这里一个业务判断都不做；
2. **只做两件事**：把运行结果摊平成界面能直接渲染的形状；把来源编号换成
   人能读的资料标题、维度与（结构化事实里的）取值；
3. **产物与评测同格式**。落盘仍然写 `results.json` / `run_meta.json` /
   `trace.jsonl`，所以既有的 `/api/queue`、版本对比、`rescore` 都能直接读，
   不需要为界面另造一套存储。

界面需要而运行结果里没有的东西（比如"这条主张的实体与指标"），按同样的
规则重新推断一次并**标注来源**，不假装是运行结果自带的。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..grading import grade_of
from ..runio import plan_run_dir
from ..tools.corpus import LocalCorpus
from .agent import ContentOutcome, EvidenceAgent, make_agent
from .extract import extract_claim_texts
from .planner import infer_claim_type, infer_metric
from .states import ESCALATED, JUDGED, TOOL_FAILED

#: 单条内容的长度上限。语料与判定都是按句处理的，超长内容只会拖慢一次同步请求；
#: 上限在这里定，服务端与前端提示共用同一个数字。
MAX_CONTENT_CHARS = 4000

#: 单条内容最多核查的主张数，交给规则抽取器。
MAX_CLAIMS = 5

STATE_LABELS: dict[str, str] = {
    JUDGED: "已给出结论",
    ESCALATED: "已交人工",
    TOOL_FAILED: "系统不可用",
}

VERDICT_LABELS: dict[str, str] = {
    "SUPPORTED": "支持",
    "PARTIAL": "部分支持",
    "CONTRADICTED": "冲突",
    "INSUFFICIENT": "证据不足",
}

FLAG_LABELS: dict[str, str] = {
    "STALE": "资料已过期或版本不符",
    "SOURCE_CONFLICT": "有效资料之间互相矛盾",
    "SCOPE_MISMATCH": "资料覆盖范围与主张不一致",
    "OMITTED_CONDITION": "主张省略了影响结论的条件",
    "UNIT_MISMATCH": "单位或统计口径不一致",
    "LOW_SOURCE_QUALITY": "只有营销或不可溯源来源",
    "NOT_COMPARABLE": "两份资料不可比",
    "HARM_ASSUMED": "危害等级按默认档处理",
}

#: 工具中文名。界面上说"按实体与指标查事实库"，比 `lookup_fact` 好读。
TOOL_LABELS: dict[str, str] = {
    "lookup_fact": "查结构化事实库",
    "search_notes": "检索语料资料",
    "fetch_source": "取原文定位段落",
}

#: 模型不可用等原因：这些是"本次核查不成立"，不是"证据不足"，必须分开说。
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RunIdError(ValueError):
    """运行编号不合法（防路径穿越，与 runio.plan_run_dir 同一套规则）。"""


class ContentValidationError(ValueError):
    """输入内容不合法。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_run_id(prefix: str = "content") -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def validate_content(content: str) -> str:
    """校验并归一化输入内容。空内容、超长内容都在这里拦掉。"""

    text = (content or "").strip()
    if not text:
        raise ContentValidationError("内容为空：请先粘贴一段待核查内容。")
    if len(text) > MAX_CONTENT_CHARS:
        raise ContentValidationError(
            f"内容过长：当前 {len(text)} 字，上限 {MAX_CONTENT_CHARS} 字。"
        )
    return text


# --- 来源展示 ---------------------------------------------------------


def _fact_value(row: Mapping[str, Any]) -> str:
    value = row.get("value")
    unit = str(row.get("unit", "") or "")
    period = str(row.get("period", "") or "")
    text = f"{value}{unit}"
    if period:
        text += f"（{period}）"
    return text


def source_view(corpus: LocalCorpus, source_id: str) -> dict[str, Any]:
    """把来源编号换成人能读的记录。

    界面上给使用者看 `S-006` 是设计错误——那是数据库主键。所以这里给
    标题、来源类型、等级、日期、发布者，以及**结构化事实里的取值**（如果这份
    资料在事实库里有记录）。取值是判定真正比对的东西，缺了它读者看不出
    "为什么这条证据能支持／反驳这个主张"。
    """

    doc = corpus.get_doc(source_id)
    if doc is None:
        return {
            "id": source_id,
            "title": source_id,
            "tier": "",
            "date": "",
            "publisher": "",
            "kind": "",
            "identity": "",
            "sampling": "",
            "accredited": False,
            "weight": None,
            "eligible": None,
            "grade_reasons": [],
            "facts": [],
            "excerpt": "",
            "in_corpus": False,
        }

    grade = grade_of(
        type(
            "_Row",
            (),
            {"meta": corpus.evidence_meta(doc), "tier": doc.tier, "date": doc.date},
        )()
    )
    facts = [
        {
            "metric": str(row.get("metric", "")),
            "value": _fact_value(row),
            "conditions": str(row.get("conditions", "") or ""),
        }
        for row in corpus.facts_for(doc.id)
    ]
    return {
        "id": doc.id,
        "title": doc.title,
        "tier": doc.tier,
        "date": doc.date,
        "publisher": doc.publisher,
        "kind": doc.kind,
        "identity": grade.identity,
        "sampling": grade.sampling,
        "accredited": grade.accredited,
        "weight": round(grade.weight, 2),
        "eligible": grade.strong,
        "rank": list(grade.rank_key),
        "grade_reasons": list(grade.reasons),
        "facts": facts,
        "excerpt": doc.text[:160],
        "in_corpus": True,
    }


def claim_facts(
    corpus: LocalCorpus, claim_text: str, entity: str = "", metric: str = ""
) -> list[dict[str, Any]]:
    """这条主张涉及的实体在事实库里的记录。

    实体与指标由 `planner` 推断（与运行时选工具时同一套推断），所以这里给出的
    是**与运行时同一个口径**的候选取值，不是另算一套。
    """

    resolved = entity or ""
    if not resolved:
        matches = corpus.resolve_entity(claim_text)
        resolved = matches[0] if matches else ""
    rows: list[Mapping[str, Any]] = []
    if resolved:
        rows = corpus.lookup_fact(resolved, metric or None)
        if not rows and metric:
            rows = corpus.lookup_fact(resolved)
    if not rows and metric:
        # 实体认不出来时，退一步按指标捞：法规、标准这类上位依据不属于任何产品，
        # 只按主体过滤会全部漏掉（docs/23 第三路召回的同一条结论）。
        rows = [row for row in corpus.enriched_facts() if metric in str(row.get("metric", ""))]
    return [
        {
            "source_id": str(row.get("source_id", "")),
            "entity": str(row.get("entity", "")),
            "metric": str(row.get("metric", "")),
            "value": _fact_value(row),
            "conditions": str(row.get("conditions", "") or ""),
            "publisher": str(row.get("publisher", "")),
            "date": str(row.get("date", "")),
            "tier": str(row.get("tier", "")),
        }
        for row in rows[:4]
    ]


# --- 结果摊平 ---------------------------------------------------------


def _claim_payload(
    claim: Any,
    index: int,
    *,
    resolved_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    disposition = claim.disposition
    layers = {layer: [] for layer in ("L1", "L2", "L3")}
    primary: list[str] = []
    for attempt in claim.attempts:
        if attempt.result.items:
            layer = str(getattr(attempt.plan, "layer", "") or "")
            if layer in layers:
                layers[layer].append(attempt.plan.tool)
                if attempt.plan.tool not in primary:
                    primary.append(attempt.plan.tool)
    # 运行时记录的承载来源优先；没有记录时退回检索到的来源，并保留顺序。
    decided = [sid for sid in (claim.decided_by or []) if sid]

    return {
        "index": index,
        "key": f"{index}",
        "claim": claim.claim_text,
        "claim_type": claim.claim_type,
        "state": claim.state,
        "state_label": STATE_LABELS.get(claim.state, claim.state),
        "verdict": claim.verdict,
        "verdict_label": VERDICT_LABELS.get(claim.verdict, claim.verdict),
        "reason": claim.reason,
        "flags": list(claim.flags),
        "flag_labels": [FLAG_LABELS.get(f, f) for f in claim.flags],
        "needs_human": bool(claim.in_review_queue),
        "disposition": disposition.to_dict() if disposition is not None else None,
        "evidence": list(claim.evidence),
        # 承载结论的来源（运行时记录）。与"检索到过"不是一回事，界面上必须分开。
        "decided_by": decided,
        "evidence_override": claim.evidence_override or "",
        "tool_calls": claim.tool_calls,
        "tool_labels": [TOOL_LABELS.get(tool, tool) for tool in primary],
        "layers": layers,
        "steps": [
            {
                "strategy": attempt.plan.strategy,
                "tool": attempt.plan.tool,
                "tool_label": TOOL_LABELS.get(attempt.plan.tool, attempt.plan.tool),
                "params": attempt.plan.params,
                "selection_reason": attempt.plan.reason,
                "status": attempt.result.status,
                "ok": attempt.result.ok,
                "degraded": attempt.result.degraded,
                "degrade_reason": attempt.result.degrade_reason,
                "digest": attempt.result.digest(),
                "sources": attempt.result.source_ids(),
                "sufficient_after": attempt.assessment.sufficient,
                "flags_after": list(attempt.assessment.flags),
                "assess_reason": attempt.assessment.reason,
                "decisive_sources": list(attempt.assessment.decisive_sources),
            }
            for attempt in claim.attempts
        ],
        "metrics": {
            "prompt_tokens": claim.prompt_tokens,
            "completion_tokens": claim.completion_tokens,
            "model_calls": len(claim.model_calls),
        },
        "facts": [dict(row) for row in resolved_facts],
    }


def content_view(
    corpus: LocalCorpus,
    content: str,
    outcome: ContentOutcome,
    *,
    highlights: Sequence[Any] = (),
    claims_extracted: Sequence[str] = (),
) -> dict[str, Any]:
    """把一次内容级运行摊平成界面用的结构。

    两层顺序是刻意的：**内容级摘要在前**（运营的工作单位是内容条），
    主张级证据在后。见 docs/11-scenario-validation.md 第 4 节。
    """

    from ..highlight import highlights_for

    facts = corpus.enriched_facts()
    claim_payloads: list[dict[str, Any]] = []
    for index, claim in enumerate(outcome.claims):
        metric = infer_metric(claim.claim_text) or ""
        payload = _claim_payload(
            claim,
            index,
            resolved_facts=claim_facts(corpus, claim.claim_text, metric=metric),
        )
        # 主张句自己也要标一遍：高亮是相对该条主张文本的字符偏移，用正文的高亮
        # 去标主张会错位。规则与正文完全一致（同一个函数），只是输入文本不同。
        payload["highlights"] = [
            item.to_dict() for item in highlights_for(claim.claim_text, facts)
        ]
        claim_payloads.append(payload)

    summary = outcome.summary()
    needs_human = [c for c in claim_payloads if c["needs_human"]]
    auto = [c for c in claim_payloads if not c["needs_human"]]
    blocked = [c for c in claim_payloads if c["state"] == TOOL_FAILED]
    high_priority = [
        c
        for c in claim_payloads
        if (c["disposition"] or {}).get("priority") == "high"
    ]
    uncertain = [
        c
        for c in claim_payloads
        if c["state"] == ESCALATED or c["verdict"] == "INSUFFICIENT"
    ]
    tiers: dict[str, int] = {}
    for row in claim_payloads:
        tier = (row["disposition"] or {}).get("tier")
        if tier:
            tiers[tier] = tiers.get(tier, 0) + 1

    source_ids: list[str] = []
    for row in claim_payloads:
        for sid in [*row["decided_by"], *row["evidence"]]:
            if sid not in source_ids:
                source_ids.append(sid)

    top = _top_tier(claim_payloads)
    return {
        "content": content,
        "claims_extracted": list(claims_extracted),
        "summary": {
            **summary,
            "needs_human": len(needs_human),
            "auto_released": len(auto),
            "tool_failed": len(blocked),
            "uncertain": len(uncertain),
            "high_priority": len(high_priority),
            "tiers": tiers,
            "top_tier": top,
            "triage": _triage_line(len(needs_human), len(claim_payloads), len(blocked)),
        },
        "highlights": [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in highlights],
        "claims": claim_payloads,
        "sources": [source_view(corpus, sid) for sid in source_ids],
        "notes": _data_notes(claim_payloads, claims_extracted),
    }


def _top_tier(claims: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """内容级处置建议 = 各主张里风险最高的那一档。

    这不是新规则：`TIER_RISK` 已经在处置模块里定义，这里只取最大值用于
    内容级摘要，并**标明它是取最高档**，不假装成一次独立的内容级判定。
    """

    ranked = [
        row["disposition"]
        for row in claims
        if row.get("disposition")
    ]
    if not ranked:
        return None
    from .disposition import TIER_RISK

    return max(ranked, key=lambda d: TIER_RISK.get(str(d.get("tier")), 9))


def _triage_line(needs: int, total: int, failed: int) -> str:
    if total == 0:
        return "没有拆出可核查主张：这段内容里没有带数值或资质表述的事实性说法。"
    if failed:
        return f"{total} 条主张里 {failed} 条因系统不可用无法核查，需人工接手。"
    if needs == 0:
        return f"{total} 条主张都给出了结论，不需要人工介入（可抽查）。"
    return f"{total} 条主张里 {needs} 条需要人工判断，其余 {total - needs} 条已给出结论。"


def _data_notes(
    claims: Sequence[Mapping[str, Any]], claims_extracted: Sequence[str]
) -> list[str]:
    """把"数据从哪来、哪些是推断"写在结果里，供界面如实转述。"""

    notes: list[str] = []
    if claims_extracted:
        notes.append(
            "主张由规则抽取器从内容里拆出（不读标注），拆多拆少都会影响后续判定。"
        )
    if any(row["state"] == TOOL_FAILED for row in claims):
        notes.append("存在工具不可用的主张：这类条目没有结论，属于系统可用性问题。")
    if any((row["disposition"] or {}).get("vendor_only") for row in claims):
        notes.append(
            "有条目的依据全部是厂商自述：结论照给，但对外动作已被拦为转人工（R13）。"
        )
    if any(row["facts"] for row in claims):
        notes.append("事实取值来自受控离线语料；实体与指标由规则推断，可能认错主体。")
    return notes


# --- 落盘 -------------------------------------------------------------


def write_content_run(
    root: Path,
    run_id: str,
    payload: Mapping[str, Any],
    *,
    run_dir: Path | None = None,
) -> Path:
    """把一次内容级运行写成与评测同格式的产物。

    复用 `runio.plan_run_dir`，所以运行编号的合法性与"目录已存在则报错"这两条
    与命令行入口完全一致，不在这里另写一套路径校验。目录已经建好时直接传入
    `run_dir`——重复调用 plan_run_dir 会把刚写下的 trace.jsonl 一起清掉。
    """

    if not RUN_ID_PATTERN.match(run_id or ""):
        raise RunIdError(f"运行编号不合法：{run_id!r}")
    if run_dir is None:
        run_dir = plan_run_dir(run_id, root)
    results = {
        "run_id": run_id,
        "adapter": payload.get("adapter", "rule-only"),
        "extractor": "rule",
        "content_run": True,
        "metrics": payload.get("metrics", {}),
        "cases": payload.get("cases", []),
    }
    (run_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "run_meta.json").write_text(
        json.dumps(dict(payload.get("meta", {})), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text(
        render_content_report(run_id, payload), encoding="utf-8"
    )
    return run_dir


def render_content_report(run_id: str, payload: Mapping[str, Any]) -> str:
    """内容级研判报告。

    与评测报告刻意不同：这是给运营看的单条内容结论，不是给维护者看的指标表。
    指标口径另见 `文档库/03-评测口径/评测指标.md`。
    """

    case = (payload.get("cases") or [{}])[0]
    view = case.get("view") or {}
    summary = view.get("summary") or {}
    lines = [
        f"# 内容研判报告：{run_id}",
        "",
        f"- 生成时间：{payload.get('meta', {}).get('created_at', now_iso())}",
        f"- 判定方式：{payload.get('adapter', 'rule-only')}（rule-only 表示只用规则判定，未调用模型）",
        f"- 主张来源：规则抽取（不读标注）",
        f"- 内容：{case.get('content', '')}",
        "",
        "## 一、内容级分诊",
        "",
        summary.get("triage", ""),
        "",
        f"- 拆出主张：{summary.get('claims', 0)} 条",
        f"- 已给出结论：{summary.get('judged', 0)} 条，其中不需要人工 {summary.get('auto_released', 0)} 条",
        f"- 需人工判断：{summary.get('needs_human', 0)} 条",
        f"- 系统不可用：{summary.get('tool_failed', 0)} 条",
        f"- 证据不足（不确定）：{summary.get('uncertain', 0)} 条",
        f"- 工具调用合计：{summary.get('tool_calls', 0)} 次，耗时 {summary.get('duration_ms', 0)} ms",
        "",
        "## 二、逐条主张",
        "",
    ]
    for row in view.get("claims") or []:
        disposition = row.get("disposition") or {}
        lines.append(f"### {row.get('index', 0) + 1}. {row.get('claim', '')}")
        lines.append("")
        lines.append(
            f"- 状态：{row.get('state_label', '')}；判定：{row.get('verdict_label', '')}"
            f"；处置建议：{disposition.get('label', '—')}（{disposition.get('rule', '—')}）"
        )
        if row.get("flags"):
            lines.append("- 标志位：" + "、".join(row.get("flag_labels") or row["flags"]))
        lines.append(f"- 理由：{row.get('reason', '')}")
        if row.get("decided_by"):
            lines.append("- 承载结论的来源：" + "、".join(row["decided_by"]))
        elif row.get("evidence"):
            lines.append("- 检索到的来源：" + "、".join(row["evidence"]))
        lines.append("")
    if view.get("notes"):
        lines.append("## 三、数据说明")
        lines.append("")
        for note in view["notes"]:
            lines.append(f"- {note}")
        lines.append("")
    lines.append("> 本报告由规则路径生成，未经人工复核；处置动作不自动执行。")
    lines.append("")
    return "\n".join(lines)


# --- 运行入口 ---------------------------------------------------------

#: 工作台可用的判定方式。rule-only 零成本、可复现；deepseek 需要配置密钥。
ADAPTERS = ("rule-only", "deepseek")


def build_adapter(name: str, root: Path) -> Any | None:
    """按名字装配模型适配器。rule-only 返回 None（规则判定）。"""

    if name in ("", "rule-only", "none"):
        return None
    if name != "deepseek":
        raise ValueError(f"不支持的判定方式：{name}")
    from ..adapters.deepseek import DeepSeekAdapter

    config = json.loads((root / "config" / "default.json").read_text(encoding="utf-8"))
    return DeepSeekAdapter(
        model_id=config["model_id"],
        base_url=config["base_url"],
        api_key_env=config["api_key_env"],
        timeout_s=config["timeout_s"],
        max_retries=config["max_retries"],
        temperature=config["temperature"],
        max_tokens=config.get("max_tokens", 3000),
        json_mode=bool(config.get("json_mode", True)),
        reasoning_effort=config.get("reasoning_effort") or None,
        thinking=config.get("thinking") or None,
    )


def run_content(
    root: Path,
    content: str,
    *,
    corpus: LocalCorpus,
    adapter_name: str = "rule-only",
    adapter: Any | None = None,
    run_id: str | None = None,
    persist: bool = True,
    force: bool = False,
    extractor: Any = extract_claim_texts,
    agent_factory: Any = make_agent,
) -> dict[str, Any]:
    """跑一次内容级研判，返回界面直接可用的载荷。

    单条内容是同步跑完的（本地语料 + 规则判定在毫秒级），所以**没有进度接口**：
    界面只能显示"正在研判"，不能编造分阶段进度。这条约束写在返回值的
    `stages` 里，由界面如实渲染。
    """

    text = validate_content(content)
    claims = list(extractor(text, max_claims=MAX_CLAIMS))
    # run_id 为 None 表示由服务端生成；显式传进来的编号必须自己合法，
    # "" 不能当成"没传"——否则校验被绕过，调用方以为传了合法编号。
    if run_id is None:
        run_id = default_run_id()
    elif not RUN_ID_PATTERN.match(run_id):
        raise RunIdError(f"运行编号不合法：{run_id!r}")

    if adapter is None and adapter_name not in ("", "rule-only", "none"):
        adapter = build_adapter(adapter_name, root)

    # 先建目录（校验编号合法性、拒绝覆盖已有产物），再真正跑；TraceWriter 会
    # 在同一目录里追加 trace.jsonl。
    run_dir = plan_run_dir(run_id, root, force=force) if persist else None

    from ..trace import TraceWriter

    agent = agent_factory(corpus, adapter=adapter, trace=None)
    if run_dir is not None:
        with TraceWriter(run_dir) as writer:
            outcome = agent.run_content("CONTENT-001", claims, content=text, trace=writer)
    else:
        outcome = agent.run_content("CONTENT-001", claims, content=text)

    from ..highlight import highlights_for

    view = content_view(
        corpus,
        text,
        outcome,
        highlights=highlights_for(text, corpus.enriched_facts()),
        claims_extracted=claims,
    )
    meta = {
        "run_id": run_id,
        "created_at": now_iso(),
        "adapter": adapter_name,
        "extractor": "rule",
        "corpus_version": corpus.version,
        "content_run": True,
        "run_kind": "content",
        "content_chars": len(text),
        "claims": len(outcome.claims),
        "needs_human": view["summary"]["needs_human"],
        "tool_calls": view["summary"]["tool_calls"],
        "duration_ms": view["summary"]["duration_ms"],
    }
    # 顶层 metrics 与 run_agent_eval.py 的 results.json 保持同名同义，
    # 这样既有的 /api/agent-runs、版本对比与 rescore 不用为工作台改一行。
    payload = {
        "run_id": run_id,
        "adapter": adapter_name,
        "metrics": {
            "claims": len(outcome.claims),
            "judged_count": view["summary"]["judged"],
            "escalated_count": view["summary"]["escalated"],
            "tool_failed_count": view["summary"]["tool_failed"],
            "needs_human": view["summary"]["needs_human"],
            "auto_released": view["summary"]["auto_released"],
            # 单条内容运行没有标注，不计算任何准确率——留空，不填 0 假装测过。
            "state_accuracy": None,
            "escalation_rate": (
                view["summary"]["escalated"] / len(outcome.claims)
                if outcome.claims
                else 0.0
            ),
            "avg_tool_calls_per_claim": (
                view["summary"]["tool_calls"] / len(outcome.claims)
                if outcome.claims
                else 0.0
            ),
        },
        "v2_ceiling": {"ceiling": None},
        "cases": [
            {
                "case_id": "CONTENT-001",
                "category": "用户提交",
                "difficulty": "",
                "content": text,
                "given_sources": [],
                "what_it_tests": "",
                "claims": [claim.to_dict() for claim in outcome.claims],
                "summary": outcome.summary(),
                "view": view,
            }
        ],
        "meta": meta,
        # 运行过程说明：界面按这个如实渲染，不许编造分阶段进度。
        "stages": {
            "mode": "sync",
            "label": "本次研判在本机同步跑完（受控离线语料 + 规则判定），没有分阶段进度",
            "steps": [
                {"name": "内容解析与主张识别", "detail": f"规则抽取器拆出 {len(claims)} 条可核查主张"},
                {"name": "证据检索与充分性判定", "detail": f"工具调用 {view['summary']['tool_calls']} 次"},
                {"name": "风险与处置判定", "detail": f"转人工 {view['summary']['needs_human']} 条"},
            ],
        },
    }
    if persist and run_dir is not None:
        write_content_run(root, run_id, payload, run_dir=run_dir)
        payload["run_dir"] = f"runs/{run_id}"
    return payload


def load_content_payload(root: Path, run_id: str) -> dict[str, Any]:
    """读回一次内容级运行。不是内容级运行就抛 ValueError。"""

    if not RUN_ID_PATTERN.match(run_id or ""):
        raise RunIdError(f"运行编号不合法：{run_id!r}")
    path = root / "runs" / run_id / "results.json"
    if not path.exists():
        raise FileNotFoundError(f"运行不存在：{run_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("content_run"):
        raise ValueError(f"不是内容研判运行：{run_id}")
    meta = {}
    meta_path = root / "runs" / run_id / "run_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    references = None
    try:
        from ..tools import load_reference_payload

        references = load_reference_payload(root, run_id)
    except Exception:  # 参考材料读不出来不影响研判结果展示
        references = None
    return {
        "run_id": run_id,
        "adapter": payload.get("adapter", "rule-only"),
        "metrics": payload.get("metrics", {}),
        "cases": payload.get("cases", []),
        "meta": meta,
        # 影子材料与自动研判结果平级、分开；没有就是 None
        "web_references": references,
        "stages": {
            "mode": "sync",
            "label": "历史运行：结论来自当时的受控离线语料与规则判定",
            "steps": [],
        },
    }


def list_content_runs(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    """最近的内容研判运行。只读内容级产物，不碰评测运行。"""

    runs_dir = root / "runs"
    if not runs_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for entry in sorted(runs_dir.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        results = entry / "results.json"
        if not results.exists():
            continue
        try:
            payload = json.loads(results.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not payload.get("content_run"):
            continue
        meta: dict[str, Any] = {}
        meta_path = entry / "run_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        case = (payload.get("cases") or [{}])[0]
        summary = (case.get("view") or {}).get("summary") or {}
        rows.append(
            {
                "run_id": entry.name,
                "created_at": meta.get("created_at", ""),
                "adapter": payload.get("adapter", "rule-only"),
                "claims": summary.get("claims", len(case.get("claims") or [])),
                "needs_human": summary.get("needs_human", 0),
                "tool_failed": summary.get("tool_failed", 0),
                "top_tier": (summary.get("top_tier") or {}).get("label", ""),
                "triage": summary.get("triage", ""),
                "excerpt": (case.get("content") or "")[:80],
            }
        )
        if len(rows) >= max(1, int(limit)):
            break
    return rows


# --- 联网影子模式（人工参考材料，不参与自动研判） ---------------------

#: 一次请求最多接受的 URL 条数（与 shadow.MAX_REQUESTS_PER_RUN 一致的上限来源）。
MAX_REFERENCE_URLS = 3


def shadow_status(root: Path) -> dict[str, Any]:
    """联网参考能力的真实状态。缺密钥时如实说缺，不在界面上假造一个按钮。"""

    from ..tools import REFERENCE_DISCLAIMER, build_search_provider, load_web_config
    from ..tools import websearch

    config = load_web_config(root)
    provider = build_search_provider(config)
    subdir = str(config.get("snapshot_dir") or "snapshots/web")
    return {
        "enabled": bool(config.get("enabled", True)),
        "disclaimer": REFERENCE_DISCLAIMER,
        # URL 获取不需要密钥；发现模式需要
        "url_mode": {"available": True, "requires_key": False},
        "search_mode": {
            "available": bool(provider.available),
            "requires_key": True,
            "api_key_env": provider.api_key_env,
            "provider": provider.provider,
            "note": (
                "已配置搜索 API 密钥"
                if provider.available
                else f"未配置 {provider.api_key_env}：发现模式不可用，只能直接给 URL"
            ),
        },
        "limits": {
            "timeout_s": float(config.get("timeout_s") or websearch.DEFAULT_TIMEOUT_S),
            "max_bytes": int(config.get("max_bytes") or websearch.DEFAULT_MAX_BYTES),
            "max_text_chars": int(
                config.get("max_text_chars") or websearch.DEFAULT_MAX_TEXT_CHARS
            ),
            "max_redirects": int(
                config.get("max_redirects") or websearch.DEFAULT_MAX_REDIRECTS
            ),
            "max_requests_per_run": MAX_REFERENCE_URLS,
            "max_urls_per_request": MAX_REFERENCE_URLS,
        },
        "snapshot_dir": subdir,
        "storage": {
            "schema": "sha256; raw_bytes|text_utf8|meta_json",
            "note": "快照写在 runs/<run_id>/snapshots/web/，只增不改",
        },
        "policy": [
            "只允许 http／https，默认拒绝 localhost、私有地址、链路本地与云元数据地址",
            "每次重定向后重新校验目标，最多跟随 3 跳",
            "不执行网页中的 JavaScript；网页内容一律当不可信数据",
            "参考材料不参与主张判定、来源分级与 R1–R13 处置",
        ],
    }


def capture_references(
    root: Path,
    run_id: str,
    *,
    urls: Sequence[str] = (),
    query: str = "",
    config: Mapping[str, Any] | None = None,
    fetcher: Any | None = None,
    provider: Any | None = None,
) -> dict[str, Any]:
    """为一次已有运行取联网参考材料并落盘。

    刻意**只做取回与存档**：不写进 `results.json`，不触碰任何主张的判定。
    """

    from ..tools import ShadowRetrieval, ShadowRetrievalError, write_reference_payload

    clean_urls = [str(url or "").strip() for url in urls if str(url or "").strip()]
    if not clean_urls and not (query or "").strip():
        raise ContentValidationError("至少给出一个 URL 或一个查询词。")
    if len(clean_urls) > MAX_REFERENCE_URLS:
        raise ContentValidationError(
            f"一次最多取 {MAX_REFERENCE_URLS} 个 URL，收到 {len(clean_urls)} 个。"
        )

    if not RUN_ID_PATTERN.match(run_id or ""):
        raise RunIdError(f"运行编号不合法：{run_id!r}")
    if not (root / "runs" / run_id).exists():
        raise FileNotFoundError(f"运行不存在：{run_id}")

    shadow = ShadowRetrieval(
        root,
        run_id=run_id,
        config=config,
        fetcher=fetcher,
        provider=provider,
        max_requests=MAX_REFERENCE_URLS,
    )
    if not shadow.enabled:
        raise ContentValidationError("联网参考模式未启用（config/web_retrieval.json 的 enabled=false）。")

    references = (
        shadow.fetch_urls(clean_urls) if clean_urls else shadow.discover(query)
    )
    payload = shadow.references_payload(references)
    try:
        write_reference_payload(root, run_id, payload)
        payload["saved"] = True
    except ShadowRetrievalError as exc:
        payload["saved"] = False
        payload["save_error"] = str(exc)
    # 返回合并后的完整列表：界面显示的内容与落盘的内容必须一致，
    # 否则刷新页面会"少几条"，读者无法判断是没取到还是没保存。
    merged = load_reference_payload(root, run_id)
    return merged if merged else payload


def list_references(root: Path, run_id: str) -> dict[str, Any] | None:
    from ..tools import load_reference_payload

    return load_reference_payload(root, run_id)


def sample_contents(root: Path) -> list[dict[str, str]]:
    """给界面用的演示样例：直接读真实任务集与语料，不另写一份。

    样例必须来自 `data/gap_tasks.json`（真实标注任务）或 `data/corpus`
    （受控语料），界面上标注为演示样例，不冒充用户真实内容。
    """

    samples: list[dict[str, str]] = []
    tasks = root / "data" / "gap_tasks.json"
    if tasks.exists():
        try:
            payload = json.loads(tasks.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        for case in (payload.get("cases") or [])[:3]:
            samples.append(
                {
                    "id": str(case.get("id", "")),
                    "label": f"{case.get('id', '')} · {case.get('category', '')}（来自缺口任务集）",
                    "content": str(case.get("content", "")),
                    "origin": "data/gap_tasks.json",
                }
            )
    return samples

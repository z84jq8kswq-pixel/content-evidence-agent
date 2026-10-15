"""Web interface for the content evidence agent.

Standard library only, same as the rest of the project. Serves the three views
described in docs/09-web-interface.md and reads its data from ``runs/`` so the
interface can never disagree with the evaluation reports.

    python app/server.py
    python app/server.py --port 8788
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import mimetypes
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.adapters.base import AdapterConfigError  # noqa: E402
from evidence_agent.agent_compare import compare_agent_runs  # noqa: E402
from evidence_agent.compare import comparison_for  # noqa: E402
from evidence_agent.config import DEFAULTS, resolve_pricing  # noqa: E402
from evidence_agent.dataset import load_cases  # noqa: E402
from evidence_agent.envfile import load_env_files  # noqa: E402
from evidence_agent.metrics import aggregate, score_case  # noqa: E402
from evidence_agent.runio import list_runs, load_run  # noqa: E402
from evidence_agent.runtime import workbench  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"
REVIEWS_PATH = Path(__file__).parent / "reviews.jsonl"
DATASET_PATH = ROOT / "data" / "eval_tasks.json"

#: 从聊天、文档里复制链接时，紧跟 URL 的中文标点常被一起带进来
#: （例如 `/agent）。`）。这不是使用者的输入错误，路径归一化时去掉即可。
TRAILING_NOISE = "）。，、；：！？”’〉》」』"


def normalize_path(raw_path: str) -> str:
    """归一化请求路径：去掉结尾的中文标点与多余斜杠。

    只做这两件事，不做模糊匹配——真拼错的路径仍然应该 404。
    """

    path = (raw_path or "/").strip().rstrip(TRAILING_NOISE)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def route_path(request_target: str) -> str:
    """从请求行得到路由路径：先解百分号编码，再归一化。

    必须先解码——浏览器把 `/agent）。` 编成 `/agent%EF%BC%89%E3%80%82`，
    不解码的话去噪规则一个字符都匹配不上。
    """

    return normalize_path(unquote(urlparse(request_target).path))

RUN_KINDS = {
    "full": "完整评测",
    "legacy": "旧任务集",
    "smoke": "子集测试",
    "stub": "离线桩",
    "content": "单条内容研判",
}

RUN_METRICS: list[tuple[str, str]] = [
    ("unsupported_certainty_rate", "无依据确定性结论率"),
    ("verdict_accuracy", "主判定准确率"),
    ("claim_recall", "主张召回率"),
    ("claim_precision", "主张精确率"),
    ("flag_recall", "标志位召回率"),
    ("citation_precision_proxy", "引用命中率（近似）"),
    ("review_queue_agreement", "复核队列一致率（推导）"),
    ("insufficient_missed", "证据不足主张漏检"),
    ("p0_errors", "P0 错误"),
    ("p1_errors", "P1 错误"),
    ("model_calls", "模型调用次数"),
    ("prompt_tokens", "输入 token"),
    ("completion_tokens", "输出 token"),
    ("avg_latency_ms", "平均单任务耗时(ms)"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_cases_by_id() -> dict:
    _, cases = load_cases(DATASET_PATH)
    return {case.id: case for case in cases}


def current_eval_set() -> tuple[str, int]:
    data = DATASET_PATH.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:8]
    return digest, len(json.loads(data.decode("utf-8")).get("cases", []))


def classify_run(meta: dict, case_count: int, current_hash: str, current_total: int) -> str:
    """Not every run is a quality baseline. Say which kind each one is.

    Scores are only comparable across runs that used the same task set, the
    same model class and the full case list.
    """

    # 工作台的单条内容研判不是评测运行：它跑的是用户提交的内容，没有标注，
    # 也没有"任务集"这个概念，混进评测对比里会污染口径。
    if meta.get("content_run") or meta.get("run_kind") == "content":
        return "content"
    if str(meta.get("adapter", "")) == "stub":
        return "stub"
    if case_count < current_total:
        return "smoke"
    run_hash = meta.get("eval_set_hash")
    if not run_hash or run_hash != current_hash:
        return "legacy"
    return "full"


def run_cost(meta: dict, summary: dict) -> float:
    pricing = {**DEFAULTS["pricing"], **(meta.get("pricing") or {})}
    prompt_rate, completion_rate, _ = resolve_pricing(pricing)
    return round(
        summary["prompt_tokens"] / 1_000_000 * prompt_rate
        + summary["completion_tokens"] / 1_000_000 * completion_rate,
        4,
    )


def summarize_run(run_id: str) -> dict:
    meta, results, _ = load_run(run_id, ROOT)
    by_id = load_cases_by_id()
    cases = [by_id[r.case_id] for r in results if r.case_id in by_id]
    summary = aggregate(cases, results, float(meta.get("match_threshold", 0.3)))
    current_hash, current_total = current_eval_set()
    kind = classify_run(meta, summary["cases"], current_hash, current_total)
    return {
        "id": meta.get("run_dir", run_id),
        "config": meta.get("config_path", ""),
        "pipeline": meta.get("pipeline", ""),
        "model": meta.get("model_id", ""),
        "adapter": meta.get("adapter", ""),
        "steps": meta.get("steps", {}),
        "started_at": meta.get("started_at", ""),
        "cases": summary["cases"],
        "kind": kind,
        "kind_label": RUN_KINDS.get(kind, kind),
        "comparable": kind == "full",
        "eval_set_hash": meta.get("eval_set_hash") or "",
        "eval_set_current": current_hash,
        "cost": run_cost(meta, summary),
        "verdict_accuracy": summary["verdict_accuracy"],
        "unsupported_certainty_rate": summary["unsupported_certainty_rate"],
        "p0_errors": summary["p0_errors"],
        "p1_errors": summary["p1_errors"],
    }


def run_detail(run_id: str) -> dict:
    meta, results, _ = load_run(run_id, ROOT)
    by_id = load_cases_by_id()
    threshold = float(meta.get("match_threshold", 0.3))
    cases = [by_id[r.case_id] for r in results if r.case_id in by_id]
    summary = aggregate(cases, results, threshold)
    scores = {s.case_id: s for s in summary["score_by_case"]}
    result_by_id = {r.case_id: r for r in results}

    case_rows = []
    for case in cases:
        score = scores.get(case.id)
        result = result_by_id.get(case.id)
        case_rows.append(
            {
                "id": case.id,
                "category": case.category,
                "difficulty": case.difficulty,
                "what_it_tests": case.what_it_tests,
                "expected": len(case.expected_claims),
                "matched": score.matched if score else 0,
                "correct": score.verdict_correct if score else 0,
                "p0": len(score.p0_errors) if score else 0,
                "p1": len(score.p1_errors) if score else 0,
                "needs_review": any(
                    c.in_review_queue for c in (result.predicted_claims if result else [])
                ),
                "verdicts": [c.verdict for c in (result.predicted_claims if result else [])],
                "duration_ms": result.duration_ms if result else 0,
                "error": (result.error or result.parse_error) if result else None,
            }
        )

    metrics = [
        {"key": key, "name": name, "value": summary.get(key)}
        for key, name in RUN_METRICS
    ]

    return {
        "meta": meta,
        "metrics": metrics,
        "cost": run_cost(meta, summary),
        "cases": case_rows,
    }


def case_detail(run_id: str, case_id: str) -> dict:
    meta, results, _ = load_run(run_id, ROOT)
    by_id = load_cases_by_id()
    case = by_id.get(case_id)
    if case is None:
        raise KeyError(f"任务集里没有 {case_id}")
    result = next((r for r in results if r.case_id == case_id), None)
    if result is None:
        raise KeyError(f"运行 {run_id} 里没有 {case_id} 的结果")

    score = score_case(case, result, float(meta.get("match_threshold", 0.3)))
    reviews = review_state(run_id)
    predicted = []
    for index, claim in enumerate(result.predicted_claims, start=1):
        item = dataclasses.asdict(claim)
        item["index"] = index
        item["in_review_queue"] = claim.in_review_queue
        item["review"] = reviews.get(f"{case_id}#{index}")
        predicted.append(item)

    return {
        "case": {
            "id": case.id,
            "category": case.category,
            "difficulty": case.difficulty,
            "content": case.content,
            "sources": [dataclasses.asdict(s) for s in case.sources],
            "expected_claims": [dataclasses.asdict(c) for c in case.expected_claims],
            "what_it_tests": case.what_it_tests,
            "requires_human": case.requires_human,
        },
        "predicted": predicted,
        "score": dataclasses.asdict(score),
        "calls": [
            {
                "step": call.step,
                "latency_ms": call.response.latency_ms,
                "prompt_tokens": call.response.prompt_tokens,
                "completion_tokens": call.response.completion_tokens,
                "attempts": call.response.attempts,
                "finish_reason": call.response.finish_reason,
                "error": call.response.error,
                "error_code": call.response.error_code,
                "output": call.response.text,
                "raw_body": call.response.raw_body,
            }
            for call in result.calls
        ],
        "error": result.error,
        "parse_error": result.parse_error,
        "format_violation": result.format_violation,
        "duration_ms": result.duration_ms,
    }


def review_state(run_id: str) -> dict[str, dict]:
    if not REVIEWS_PATH.exists():
        return {}
    latest: dict[str, dict] = {}
    for line in REVIEWS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("run") != run_id:
            continue
        key = f"{record.get('case_id')}#{record.get('claim_index')}"
        latest[key] = record
    return latest


# --- v3 Agent 运行（缺口任务评测的产物） -----------------------------
#
# 老评测与 v3 Agent 运行都用 runs/<id>/results.json，两者都是对象，区别在
# 顶层有没有 metrics 键：老格式是 meta/summary/cases，v3 是 metrics/cases。
# 按这个键区分，避免两种运行混进同一个下拉框里比分数。


def load_agent_results(run_id: str, root: Path = ROOT) -> dict:
    path = root / "runs" / run_id / "results.json"
    if not path.exists():
        raise FileNotFoundError(f"运行不存在：{run_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "metrics" not in payload:
        raise ValueError(f"不是 v3 Agent 运行：{run_id}")
    # 内容研判与缺口评测是两类产物：前者没有标注，进不了评测口径，
    # 所以这里明确拒绝，而不是让它混进评测视图。
    if payload.get("content_run"):
        raise ValueError(f"不是评测运行（这是内容研判产物）：{run_id}")
    return payload


_CORPUS_CACHE: dict[str, object] = {}


def corpus_for(root: Path = ROOT):
    """按根目录缓存语料，避免每个请求都重新读盘。"""

    from evidence_agent.tools import LocalCorpus

    key = str(root)
    if key not in _CORPUS_CACHE:
        _CORPUS_CACHE[key] = LocalCorpus.from_dir(root / "data" / "corpus")
    return _CORPUS_CACHE[key]


def source_detail(corpus, source_id: str) -> dict:
    """把资料编号换成人能读的东西：标题、日期、等级、来源类型。

    给使用者看 `S-006` 是设计错误——那是数据库主键。
    """

    doc = corpus.get_doc(source_id)
    if doc is None:
        return {
            "id": source_id,
            "title": source_id,
            "date": "",
            "tier": "",
            "kind": "",
            "identity": "",
            "sampling": "",
            "accredited": False,
            "weight": None,
            "eligible": None,
            "rank": [],
            "publisher": "",
            "chunk": "",
        }
    from evidence_agent.grading import grade_source

    grade = grade_source(
        kind=doc.kind,
        sampling=doc.sampling,
        accredited=doc.accredited,
        tier=doc.tier,
        date=doc.date,
    )
    return {
        "id": doc.id,
        "title": doc.title,
        "date": doc.date,
        "tier": doc.tier,
        "kind": doc.kind,
        "identity": grade.identity,
        "sampling": grade.sampling,
        "accredited": grade.accredited,
        "weight": round(grade.weight, 2),
        "eligible": grade.strong,
        "rank": list(grade.rank_key),
        "grade_reasons": grade.reasons,
        "publisher": doc.publisher,
        "chunk": doc.text[:120],
    }


def agent_run_meta(run_id: str, root: Path = ROOT) -> dict:
    path = root / "runs" / run_id / "run_meta.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def list_agent_runs(root: Path = ROOT) -> list[dict]:
    """评测运行列表。

    **内容研判运行刻意排除在外**：它是工作台的产物（用户提交的单条内容，没有
    标注、没有任务集），混进版本对比的下拉框会被当成评测运行比分数。
    工作台自己用 /api/content-runs 列它。
    """

    runs_dir = root / "runs"
    if not runs_dir.exists():
        return []
    summaries: list[dict] = []
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        meta = agent_run_meta(entry.name, root)
        if meta.get("content_run") or meta.get("run_kind") == "content":
            continue
        try:
            payload = load_agent_results(entry.name, root)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue
        metrics = payload.get("metrics", {})
        meta = agent_run_meta(entry.name, root)
        summaries.append(
            {
                "id": entry.name,
                "adapter": payload.get("adapter", "rule-only"),
                "created_at": meta.get("created_at", ""),
                "claims": metrics.get("claims", 0),
                "judged": metrics.get("judged_count", 0),
                "escalated": metrics.get("escalated_count", 0),
                "tool_failed": metrics.get("tool_failed_count", 0),
                "state_accuracy": metrics.get("state_accuracy"),
                "escalation_rate": metrics.get("escalation_rate"),
                "avg_tool_calls": metrics.get("avg_tool_calls_per_claim"),
                "v2_ceiling": (payload.get("v2_ceiling") or {}).get("ceiling"),
            }
        )
    return summaries


def agent_comparison_for(
    base_id: str, new_id: str, root: Path = ROOT, threshold: float = 0.3
) -> dict:
    """两次 v3 Agent 运行的对比：指标 delta + 主张级变化。

    和 v1／v2 的 v1／v2 接口分开：v3 的结果里没有标注，只能两次运行互相比。
    """

    base_payload = load_agent_results(base_id, root)
    new_payload = load_agent_results(new_id, root)
    return compare_agent_runs(
        base_payload,
        agent_run_meta(base_id, root),
        new_payload,
        agent_run_meta(new_id, root),
        threshold=threshold,
    )


def is_legacy_run(run_id: str, root: Path = ROOT) -> bool:
    """老评测没有顶层 metrics 键；v3 Agent 运行有。

    两类运行都是 JSON 对象，曾经误以为老格式是数组——那是凭记忆写的，
    真实文件是 {"meta":…, "summary":…, "cases":[…]}。
    """

    path = root / "runs" / run_id / "results.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and "metrics" not in payload


def legacy_run_ids(root: Path = ROOT) -> list[str]:
    """只返回老评测运行。

    v3 运行不能出现在这个列表里：指标口径完全不同，混进同一个下拉框会出现
    "选了却什么都看不到"的空视图（cases=0），而且会诱导人拿两种不可比的数字
    做对比。
    """

    return [run_id for run_id in list_runs(root) if is_legacy_run(run_id, root)]


def build_queue(run_id: str, root: Path = ROOT) -> dict:
    """把一次 Agent 运行摊平成以**主张**为单位的处置队列。

    任务流的单位是内容条，但人真正要处理的是"这条内容里的哪句话"。所以每条
    记录同时带内容条的上下文（原文、类别、用户给了哪些资料）和主张级证据。
    """

    from evidence_agent.highlight import highlights_for

    payload = load_agent_results(run_id, root)
    corpus = corpus_for(root)
    facts = corpus.enriched_facts()
    reviews = review_state(run_id)
    items: list[dict] = []

    for case in payload.get("cases", []):
        case_id = str(case.get("case_id", ""))
        for index, claim in enumerate(case.get("claims", [])):
            key = f"{case_id}#{index}"
            state = str(claim.get("state", ""))
            claim_text = str(claim.get("claim", ""))
            disposition = claim.get("disposition") or {}
            items.append(
                {
                    "key": key,
                    "case_id": case_id,
                    "claim_index": index,
                    "category": case.get("category", ""),
                    "difficulty": case.get("difficulty", ""),
                    "content": case.get("content", ""),
                    "given_sources": case.get("given_sources", []),
                    "what_it_tests": case.get("what_it_tests", ""),
                    "claim": claim_text,
                    "claim_type": claim.get("claim_type", ""),
                    "state": state,
                    "verdict": claim.get("verdict", ""),
                    "flags": claim.get("flags", []),
                    "reason": claim.get("reason", ""),
                    "evidence": claim.get("evidence", []),
                    "tool_calls": claim.get("tool_calls", 0),
                    # 档位优先：降级分发、打标、通知创作者都是自动动作，不进人工队列
                    "needs_human": bool(
                        disposition.get("needs_human", state != "JUDGED")
                    ),
                    "disposition": disposition or None,
                    "steps": claim.get("steps", []),
                    "highlights": [
                        item.to_dict() for item in highlights_for(claim_text, facts)
                    ],
                    "content_highlights": [
                        item.to_dict()
                        for item in highlights_for(str(case.get("content", "")), facts)
                    ],
                    "given_sources_detail": [
                        source_detail(corpus, sid)
                        for sid in case.get("given_sources", [])
                    ],
                    "evidence_detail": [
                        source_detail(corpus, sid) for sid in claim.get("evidence", [])
                    ],
                    "review": reviews.get(key),
                }
            )

    items.sort(
        key=lambda item: (not item["needs_human"], item["case_id"], item["claim_index"])
    )
    needs_human = [item for item in items if item["needs_human"]]
    tiers: dict[str, int] = {}
    for item in items:
        tier = (item["disposition"] or {}).get("tier")
        if tier:
            tiers[tier] = tiers.get(tier, 0) + 1
    return {
        "run_id": run_id,
        "adapter": payload.get("adapter", "rule-only"),
        "metrics": payload.get("metrics", {}),
        "v2_ceiling": payload.get("v2_ceiling", {}),
        "contents": len(payload.get("cases", [])),
        "claims": len(items),
        "counts": {
            "needs_human": len(needs_human),
            "judged": len(items) - len(needs_human),
            "tool_failed": len([i for i in items if i["state"] == "TOOL_FAILED"]),
            "reviewed": len([i for i in items if i["review"]]),
            "tiers": tiers,
        },
        "items": items,
    }


def tool_registry_view(root: Path = ROOT) -> dict:
    """工具管理页的数据：每个工具的六项规格 + 语料概况。"""

    from evidence_agent.tools import LocalCorpus, build_tools

    corpus = LocalCorpus.from_dir(root / "data" / "corpus")
    tools = build_tools(corpus)
    return {
        "corpus": {
            "path": "data/corpus",
            "version": corpus.version,
            "sources": len(corpus.docs),
            "facts": len(corpus.facts),
            "entities": sorted(corpus.aliases),
        },
        "tools": [
            {
                "name": tool.spec.name,
                "purpose": tool.spec.purpose,
                "returns": tool.spec.returns,
                "timeout_s": tool.spec.timeout_s,
                "max_retries": tool.spec.max_retries,
                "fallback": tool.spec.fallback,
                "cost_per_call": tool.spec.cost_per_call,
                "read_only": tool.spec.read_only,
                "params": [
                    {
                        "name": name,
                        "type": param.type,
                        "required": param.required,
                        "desc": param.desc,
                        "default": param.default,
                    }
                    for name, param in tool.spec.params.items()
                ],
            }
            for tool in tools
        ],
    }


# --- 内容研判工作台（用户提交内容 → 真实运行 Agent） -------------------
#
# 与上面那批接口的区别：上面读的是**已有产物**，这里会**真的跑一次**。
# 运行入口复用 `runtime.workbench`，业务判定仍然在 EvidenceAgent 与
# runtime.disposition 里，服务端只做校验、串行化与错误翻译。

#: 同时只允许一次内容研判。run_id 到秒，两次同秒提交会撞目录；串行化最省事，
#: 也避免多线程同时写 runs/ 下的文件。
_RUN_LOCK = threading.Lock()

#: 单次请求体上限（字节）。内容上限是 4000 字，留足 JSON 包装的余量。
MAX_BODY_BYTES = 64 * 1024


def read_json_body(handler: BaseHTTPRequestHandler) -> tuple[dict, str]:
    """读并解析请求体。返回 (payload, error)。错误只给中性描述，不给堆栈。"""

    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except ValueError:
        return {}, "Content-Length 无效"
    if length <= 0:
        return {}, "请求体为空"
    if length > MAX_BODY_BYTES:
        return {}, "请求体过大"
    raw = handler.rfile.read(length)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, "请求体不是合法的 UTF-8 JSON"
    if not isinstance(payload, dict):
        return {}, "请求体必须是 JSON 对象"
    return payload, ""


def workbench_status(root: Path = ROOT) -> dict:
    """工作台的真实运行条件：判定方式、语料版本、可用样例。

    界面上展示"数据来源状态"时必须来自这里——没有真实依据的字段不编。
    """

    from evidence_agent.tools import LocalCorpus

    corpus = LocalCorpus.from_dir(root / "data" / "corpus")
    adapter = configured_adapter()
    key_env = DEFAULTS.get("api_key_env", "DEEPSEEK_API_KEY")
    return {
        "adapter": adapter,
        "adapter_label": (
            "规则判定（本地离线，可复现）" if adapter == "rule-only" else "规则 + 模型判定"
        ),
        "model_key_ready": bool(os.environ.get(key_env, "").strip()),
        "model_key_env": key_env,
        "corpus": {
            "version": corpus.version,
            "sources": len(corpus.docs),
            "facts": len(corpus.facts),
            "path": "data/corpus",
        },
        "limits": {
            "max_content_chars": workbench.MAX_CONTENT_CHARS,
            "max_claims": workbench.MAX_CLAIMS,
            "max_reference_urls": workbench.MAX_REFERENCE_URLS,
        },
        "samples": workbench.sample_contents(root),
        "run_kind": "content",
        # 联网参考能力（影子模式）：真实状态，缺密钥就如实说缺
        "web": workbench.shadow_status(root),
    }


def run_content_request(root: Path, payload: dict) -> tuple[dict, int]:
    """处理一次内容研判请求。返回 (响应体, HTTP 状态码)。

    错误分三类，不能混：
    - 输入问题 → 400（内容为空／过长、判定方式不支持、运行编号不合法）；
    - 运行条件缺失 → 200 + 结构化 error（例如没配密钥），因为这是**可预期的
      运行条件不足**，界面要给出明确指引，而不是当成服务器故障；
    - 其他异常 → 500，只回一句中性描述，不带堆栈。
    """

    content = payload.get("content")
    if not isinstance(content, str):
        return {"error": {"kind": "invalid_content", "message": "content 必须是字符串"}}, 400

    adapter_name = str(payload.get("adapter") or "").strip() or None
    if adapter_name is not None and adapter_name not in workbench.ADAPTERS:
        return {
            "error": {
                "kind": "invalid_adapter",
                "message": f"不支持的判定方式：{adapter_name}",
                "allowed": list(workbench.ADAPTERS),
            }
        }, 400

    run_id = payload.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        return {"error": {"kind": "invalid_run_id", "message": "run_id 必须是字符串"}}, 400

    try:
        with _RUN_LOCK:
            result = workbench.run_content(
                root,
                content,
                corpus=corpus_for(root),
                adapter_name=adapter_name or configured_adapter(),
                run_id=run_id,
            )
    except workbench.ContentValidationError as exc:
        return {"error": {"kind": "invalid_content", "message": str(exc)}}, 400
    except workbench.RunIdError as exc:
        return {"error": {"kind": "invalid_run_id", "message": str(exc)}}, 400
    except ValueError as exc:
        # plan_run_dir 的"目录已存在"也走这里
        return {"error": {"kind": "run_conflict", "message": str(exc)}}, 409
    except AdapterConfigError as exc:
        return {
            "error": {
                "kind": "model_unavailable",
                "message": str(exc),
                "hint": "改用规则判定（rule-only）仍然可以完成本次研判。",
            }
        }, 200
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，绝不把堆栈发给用户
        return {"error": {"kind": "run_failed", "message": f"{type(exc).__name__}"}}, 500
    return result, 200


def reference_request(root: Path, payload: dict) -> tuple[dict, int]:
    """取一次联网参考材料（影子模式）。

    与 `run_content_request` 的关键区别：**这里不跑 Agent**，只取回网页并存快照。
    错误分类沿用同一套：输入问题 400、运行不存在 404、运行条件不足 200 + 结构化 error。
    """

    run_id = payload.get("run")
    if not isinstance(run_id, str) or not run_id.strip():
        return {"error": {"kind": "invalid_run", "message": "缺少参数 run（运行编号）"}}, 400

    urls = payload.get("urls")
    if urls is None:
        urls = [payload["url"]] if payload.get("url") else []
    if not isinstance(urls, list) or any(not isinstance(u, str) for u in urls):
        return {"error": {"kind": "invalid_urls", "message": "urls 必须是字符串数组"}}, 400

    query = payload.get("query")
    if query is not None and not isinstance(query, str):
        return {"error": {"kind": "invalid_query", "message": "query 必须是字符串"}}, 400

    try:
        with _RUN_LOCK:
            result = workbench.capture_references(
                root, run_id.strip(), urls=urls, query=query or ""
            )
    except workbench.RunIdError as exc:
        return {"error": {"kind": "invalid_run", "message": str(exc)}}, 400
    except workbench.ContentValidationError as exc:
        return {"error": {"kind": "invalid_request", "message": str(exc)}}, 400
    except FileNotFoundError as exc:
        return {"error": {"kind": "run_not_found", "message": str(exc)}}, 404
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，绝不把堆栈发给用户
        return {"error": {"kind": "fetch_failed", "message": f"{type(exc).__name__}"}}, 500
    return result, 200


def configured_adapter() -> str:
    """判定方式来自环境变量，默认规则判定。

    刻意不暴露成页面上的开关：换判定方式会改变结论，属于运行配置，
    不是运营在页面上随手切的东西。
    """

    adapter = os.environ.get("EVIDENCE_AGENT_ADAPTER", "rule-only").strip() or "rule-only"
    return adapter if adapter in workbench.ADAPTERS else "rule-only"


class Handler(BaseHTTPRequestHandler):
    server_version = "EvidenceAgentUI/0.1"

    def log_message(self, format: str, *args) -> None:  # quieter console
        return

    def _json(self, payload, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def do_GET(self) -> None:
        path = route_path(self.path)
        query = self._query()
        try:
            if path == "/favicon.ico":
                # 浏览器会自动请求。没有图标就明确回 204，不要伪装成 404
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/api/runs":
                summaries = []
                for run_id in legacy_run_ids(ROOT):
                    try:
                        summaries.append(summarize_run(run_id))
                    except Exception as exc:  # a broken run must not break the list
                        summaries.append({"id": run_id, "error": str(exc)})
                self._json({"runs": summaries})
                return
            if path == "/api/run":
                run_id = query.get("run", "")
                self._json(run_detail(run_id))
                return
            if path == "/api/case":
                self._json(case_detail(query.get("run", ""), query.get("case", "")))
                return
            if path == "/api/compare":
                self._json(
                    comparison_for(
                        query.get("base", ""), query.get("compare", ""), ROOT
                    )
                )
                return
            if path == "/api/reviews":
                self._json({"reviews": review_state(query.get("run", ""))})
                return
            if path == "/api/agent-runs":
                self._json({"runs": list_agent_runs()})
                return
            if path == "/api/agent-compare":
                base_id = query.get("base", "")
                new_id = query.get("compare", "")
                if not base_id or not new_id:
                    self._error("缺少参数 base 或 compare", 400)
                    return
                try:
                    self._json(agent_comparison_for(base_id, new_id))
                except ValueError as exc:
                    # 拿老评测的编号问 v3 接口属于用错接口，不是服务器错误
                    self._error(str(exc), 400)
                return
            if path == "/api/queue":
                run_id = query.get("run", "")
                if not run_id:
                    self._error("缺少参数 run", 400)
                    return
                self._json(build_queue(run_id))
                return
            if path == "/api/tools":
                self._json(tool_registry_view())
                return
            if path == "/api/workbench":
                self._json(workbench_status())
                return
            if path == "/api/content-runs":
                limit_raw = query.get("limit", "20")
                try:
                    limit = max(1, min(100, int(limit_raw)))
                except ValueError:
                    self._error("limit 必须是整数", 400)
                    return
                self._json({"runs": workbench.list_content_runs(ROOT, limit=limit)})
                return
            if path == "/api/content-run":
                run_id = query.get("run", "")
                if not run_id:
                    self._error("缺少参数 run", 400)
                    return
                try:
                    self._json(workbench.load_content_payload(ROOT, run_id))
                except workbench.RunIdError as exc:
                    self._error(str(exc), 400)
                return
            if path == "/api/web-references":
                run_id = query.get("run", "")
                if not run_id:
                    self._error("缺少参数 run", 400)
                    return
                if not workbench.RUN_ID_PATTERN.match(run_id):
                    self._error(f"运行编号不合法：{run_id!r}", 400)
                    return
                payload = workbench.list_references(ROOT, run_id)
                if payload is None:
                    self._json({"run_id": run_id, "reference_only": True, "items": [], "counts": {"total": 0, "ok": 0, "failed": 0}}, 200)
                    return
                self._json(payload)
                return
            if path in ("/workbench", "/workbench.html"):
                self._send_static(STATIC_DIR / "workbench.html")
                return
            if path in ("/agent", "/agent.html"):
                self._send_static(STATIC_DIR / "agent.html")
                return
            if path in ("/tools", "/tools.html"):
                self._send_static(STATIC_DIR / "tools.html")
                return
            if path in ("/", "", "/index.html"):
                self._send_static(STATIC_DIR / "index.html")
                return
            if path.startswith("/static/"):
                self._send_static(STATIC_DIR / path[len("/static/") :])
                return
            # 404 必须带上路径。不带路径的 not_found 谁也查不出问题在哪
            self._json(
                {
                    "error": "not_found",
                    "path": path,
                    "hint": "页面：/workbench、/agent、/tools、/；接口：/api/workbench、/api/run-content、/api/content-runs、/api/content-run?run=、/api/runs、/api/agent-runs、/api/queue?run=、/api/tools",
                },
                404,
            )
        except FileNotFoundError as exc:
            self._error(str(exc), 404)
        except KeyError as exc:
            self._error(str(exc), 404)
        except Exception as exc:
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def _send_static(self, file_path: Path) -> None:
        resolved = file_path.resolve()
        if not str(resolved).startswith(str(STATIC_DIR.resolve())) or not resolved.is_file():
            self._error("not_found", 404)
            return
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if resolved.suffix in (".html", ".js", ".css"):
            content_type += "; charset=utf-8"
        data = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/run-content":
            payload, error = read_json_body(self)
            if error:
                self._error(error, 400)
                return
            body, status = run_content_request(ROOT, payload)
            self._json(body, status)
            return

        if path == "/api/web-references":
            payload, error = read_json_body(self)
            if error:
                self._error(error, 400)
                return
            body, status = reference_request(ROOT, payload)
            self._json(body, status)
            return

        if path != "/api/review":
            self._error("not_found", 404)
            return

        payload, error = read_json_body(self)
        if error:
            self._error(error, 400)
            return

        required = ("run", "case_id", "claim_index", "decision")
        missing = [key for key in required if payload.get(key) in (None, "")]
        if missing:
            self._error("缺少字段：" + ", ".join(missing), 400)
            return

        record = {
            "run": str(payload["run"]),
            "case_id": str(payload["case_id"]),
            "claim_index": int(payload["claim_index"]),
            "decision": str(payload["decision"]),
            "note": str(payload.get("note", "")),
            "reviewed_at": now_iso(),
        }
        with REVIEWS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._json({"ok": True, "record": record})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="内容风险研判工作台")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--adapter",
        default=None,
        choices=list(workbench.ADAPTERS),
        help="内容研判的判定方式：rule-only（默认，本地离线可复现）或 deepseek（需密钥）",
    )
    args = parser.parse_args(argv)

    # 密钥按与 harness 相同的顺序查找：项目内 .env.local → EVIDENCE_AGENT_ENV_FILE
    # → 项目外 ~/.deepseek/evidence-agent.env。找不到也能跑，只是模型判定会失败。
    load_env_files(ROOT)
    if args.adapter:
        os.environ["EVIDENCE_AGENT_ADAPTER"] = args.adapter

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"内容风险研判工作台：http://{args.host}:{args.port}/workbench")
    print(f"判定方式：{configured_adapter()}")
    print(f"可用的运行：{', '.join(list_runs(ROOT)[:5]) or '（暂无）'}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

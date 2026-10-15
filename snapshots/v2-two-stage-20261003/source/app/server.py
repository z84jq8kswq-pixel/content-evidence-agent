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
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.compare import comparison_for  # noqa: E402
from evidence_agent.config import DEFAULTS, resolve_pricing  # noqa: E402
from evidence_agent.dataset import load_cases  # noqa: E402
from evidence_agent.metrics import aggregate, score_case  # noqa: E402
from evidence_agent.runio import list_runs, load_run  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"
REVIEWS_PATH = Path(__file__).parent / "reviews.jsonl"
DATASET_PATH = ROOT / "data" / "eval_tasks.json"

RUN_KINDS = {
    "full": "完整评测",
    "legacy": "旧任务集",
    "smoke": "子集测试",
    "stub": "离线桩",
}

RUN_METRICS: list[tuple[str, str]] = [
    ("unsupported_certainty_rate", "无依据确定性结论率"),
    ("verdict_accuracy", "主判定准确率"),
    ("claim_recall", "主张召回率"),
    ("claim_precision", "主张精确率"),
    ("flag_recall", "标志位召回率"),
    ("citation_precision_proxy", "引用命中率（近似）"),
    ("review_queue_agreement", "复核队列一致率"),
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
        path = urlparse(self.path).path
        query = self._query()
        try:
            if path == "/api/runs":
                summaries = []
                for run_id in list_runs(ROOT):
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
            if path in ("/", "", "/index.html"):
                self._send_static(STATIC_DIR / "index.html")
                return
            if path.startswith("/static/"):
                self._send_static(STATIC_DIR / path[len("/static/") :])
                return
            self._error("not_found", 404)
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
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._error("invalid_json", 400)
            return

        if path != "/api/review":
            self._error("not_found", 404)
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
    parser = argparse.ArgumentParser(description="内容证据核查工作台")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"内容证据核查工作台：http://{args.host}:{args.port}")
    print(f"可用的运行：{', '.join(list_runs(ROOT)[:5]) or '（暂无）'}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

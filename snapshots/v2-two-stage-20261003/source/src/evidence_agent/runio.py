"""Read evaluation runs back from disk.

The harness, the CLI comparison tool and the web interface all need to rebuild
the same objects from ``runs/<id>/results.json``. Keeping that in one place
means the interface and the report can never disagree about a score.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import CallRecord, CaseResult, ModelResponse, PredictedClaim

RUNS_DIRNAME = "runs"


def rebuild_calls(raw_calls: list[dict[str, Any]]) -> list[CallRecord]:
    return [
        CallRecord(
            step=str(call.get("step", "")),
            response=ModelResponse(
                text=str(call.get("output", "") or ""),
                model_id=str(call.get("model_id", "")),
                prompt_tokens=int(call.get("prompt_tokens", 0) or 0),
                completion_tokens=int(call.get("completion_tokens", 0) or 0),
                latency_ms=int(call.get("latency_ms", 0) or 0),
                attempts=int(call.get("attempts", 1) or 1),
                error=call.get("error"),
                error_code=call.get("error_code"),
                finish_reason=call.get("finish_reason"),
                raw_body=call.get("raw_body"),
            ),
        )
        for call in raw_calls
    ]


def rebuild_result(raw: dict[str, Any]) -> CaseResult:
    return CaseResult(
        case_id=str(raw["case_id"]),
        category="",
        difficulty="",
        predicted_claims=[
            PredictedClaim.from_dict(item)
            for item in raw.get("predicted_claims", [])
        ],
        calls=rebuild_calls(raw.get("calls", [])),
        error=raw.get("error"),
        parse_error=raw.get("parse_error"),
        format_violation=raw.get("format_violation"),
        duration_ms=int(raw.get("duration_ms", 0) or 0),
    )


def runs_root(project_root: Path) -> Path:
    return Path(project_root) / RUNS_DIRNAME


def list_runs(project_root: Path) -> list[str]:
    root = runs_root(project_root)
    if not root.exists():
        return []
    names = [
        item.name
        for item in root.iterdir()
        if item.is_dir() and (item / "results.json").exists()
    ]
    return sorted(names, reverse=True)


def resolve_run_dir(ref: str | Path, project_root: Path) -> Path:
    candidate = Path(ref)
    if candidate.is_dir() and (candidate / "results.json").exists():
        return candidate
    fallback = runs_root(project_root) / str(ref)
    if fallback.is_dir():
        return fallback
    raise FileNotFoundError(f"找不到运行目录：{ref}")


def load_run(ref: str | Path, project_root: Path) -> tuple[dict[str, Any], list[CaseResult], Path]:
    run_dir = resolve_run_dir(ref, project_root)
    payload = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    meta_path = run_dir / "run_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = dict(payload.get("meta", {}))
    meta.setdefault("run_id", run_dir.name)
    meta["run_dir"] = run_dir.name
    results = [rebuild_result(item) for item in payload.get("cases", [])]
    return meta, results, run_dir

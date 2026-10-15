"""Read evaluation runs back from disk.

The harness, the CLI comparison tool and the web interface all need to rebuild
the same objects from ``runs/<id>/results.json``. Keeping that in one place
means the interface and the report can never disagree about a score.
"""

from __future__ import annotations

import json
import re
import shutil
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


#: 运行编号的允许字符集。编号会被拼进路径，所以保守一点。
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def plan_run_dir(run_id: str, project_root: Path, *, force: bool = False) -> Path:
    """校验运行编号并返回可写的产物目录。

    运行编号来自命令行。直接把它拼进路径再 shutil.rmtree 覆盖是危险动作：它既是
    路径穿越的入口（".."），也会让"重跑一次基线"变成静默删除别人的产物。
    所以这里只接受保守字符集，并且默认拒绝覆盖。
    """

    name = str(run_id or "").strip()
    invalid = (
        not RUN_ID_PATTERN.match(name)
        or ".." in name
        or name.endswith(".")
        or name in (".", "..")
    )
    if invalid:
        raise ValueError(
            "运行编号不合法：" + repr(run_id) + "（只允许字母、数字、点、下划线、连字符，"
            "不能包含 ..，不能以点结尾，长度不超过 64）"
        )
    run_dir = runs_root(project_root) / name
    if run_dir.exists():
        if not force:
            raise ValueError(
                "运行目录已存在：" + str(run_dir) + "\n"
                "换一个 --run-id，或加 --force 覆盖（覆盖会删除目录里已有的产物）。"
            )
        shutil.rmtree(run_dir)
    return run_dir


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

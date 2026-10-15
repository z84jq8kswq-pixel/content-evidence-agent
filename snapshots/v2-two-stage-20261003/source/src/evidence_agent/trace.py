"""Run traces: every model call and every case result is appended as JSONL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import CallRecord, CaseResult


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TraceWriter:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "trace.jsonl"
        # One run owns one trace file. Opening in append mode would mix a
        # re-run's events into the previous attempt's trace.
        self._handle = self.path.open("w", encoding="utf-8")

    def log(self, event: str, **payload: Any) -> None:
        record = {"event": event, "timestamp": now_iso(), **payload}
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()

    def log_call(self, case_id: str, call: CallRecord) -> None:
        response = call.response
        self.log(
            "model_call",
            case_id=case_id,
            step=call.step,
            model=response.model_id,
            attempts=response.attempts,
            latency_ms=response.latency_ms,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            error=response.error,
            error_code=response.error_code,
            finish_reason=response.finish_reason,
            raw_body=response.raw_body,
            ok=response.ok,
            messages=call.request_messages,
            output=response.text,
        )

    def log_case(self, result: CaseResult) -> None:
        for call in result.calls:
            self.log_call(result.case_id, call)
        self.log(
            "case_finished",
            case_id=result.case_id,
            predicted=[
                {
                    "text": c.text,
                    "claim_type": c.claim_type,
                    "verdict": c.verdict,
                    "flags": list(c.flags),
                    "evidence": list(c.evidence),
                    "in_review_queue": c.in_review_queue,
                }
                for c in result.predicted_claims
            ],
            error=result.error,
            parse_error=result.parse_error,
            duration_ms=result.duration_ms,
            model_calls=result.model_calls,
        )

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

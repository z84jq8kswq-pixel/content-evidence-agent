"""Adapter interface with timeout, retry and error codes.

Every adapter returns a ModelResponse instead of raising, so a single failed
call never kills a whole evaluation run. Retryable failures (timeout, rate
limit, network error, 5xx) are retried with exponential backoff; fatal
failures (bad request, missing auth) return immediately.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any, Sequence

from ..models import ModelResponse

RETRYABLE_CODES = (
    "TIMEOUT",
    "RATE_LIMITED",
    "NETWORK_ERROR",
    "SERVER_ERROR",
    "EMPTY_RESPONSE",
)


def json_snapshot(raw, limit: int = 1200) -> str:
    """Serialise a raw response body for the trace, truncated."""

    try:
        text = json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(raw)
    return text[:limit]


class AdapterConfigError(RuntimeError):
    """Raised when an adapter cannot be constructed (missing key, bad config)."""


class RetryableError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FatalAdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ModelAdapter(ABC):
    name = "base"

    def __init__(
        self,
        model_id: str,
        *,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        backoff_s: float = 1.5,
        temperature: float = 0.0,
        max_tokens: int = 1200,
        sleep=time.sleep,
    ) -> None:
        self.model_id = model_id
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = backoff_s
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._sleep = sleep

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        temp = self.temperature if temperature is None else temperature
        limit = self.max_tokens if max_tokens is None else max_tokens
        attempts = 0
        last_code = "UNKNOWN"
        last_message = "未执行的调用"
        last_raw = None
        last_latency = 0

        while attempts <= self.max_retries:
            attempts += 1
            started = time.perf_counter()
            try:
                text, usage = self._invoke(
                    list(messages), temp, limit, thinking, reasoning_effort
                )
                latency = int((time.perf_counter() - started) * 1000)
                return ModelResponse(
                    text=text,
                    model_id=self.model_id,
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    latency_ms=latency,
                    attempts=attempts,
                    finish_reason=usage.get("finish_reason"),
                )
            except RetryableError as exc:
                last_code, last_message = exc.code, str(exc)
                last_latency = int((time.perf_counter() - started) * 1000)
                last_raw = getattr(self, "last_raw_body", None)
                if attempts <= self.max_retries:
                    self._sleep(self.backoff_s * (2 ** (attempts - 1)))
            except FatalAdapterError as exc:
                latency = int((time.perf_counter() - started) * 1000)
                return ModelResponse(
                    text="",
                    model_id=self.model_id,
                    latency_ms=latency,
                    attempts=attempts,
                    error=str(exc),
                    error_code=exc.code,
                    raw_body=self._raw_snapshot(),
                )
            except Exception as exc:  # defensive: never leak an unexpected raise
                latency = int((time.perf_counter() - started) * 1000)
                return ModelResponse(
                    text="",
                    model_id=self.model_id,
                    latency_ms=latency,
                    attempts=attempts,
                    error=f"{type(exc).__name__}: {exc}",
                    error_code="UNEXPECTED_ERROR",
                )

        return ModelResponse(
            text="",
            model_id=self.model_id,
            latency_ms=last_latency,
            attempts=attempts,
            error=last_message,
            error_code=last_code,
            raw_body=json_snapshot(last_raw) if last_raw is not None else None,
        )

    def _raw_snapshot(self) -> str | None:
        raw = getattr(self, "last_raw_body", None)
        return json_snapshot(raw) if raw is not None else None

    @abstractmethod
    def _invoke(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        thinking: str | None,
        reasoning_effort: str | None,
    ) -> tuple[str, dict[str, Any]]:
        """Return (text, usage). Raise RetryableError or FatalAdapterError."""

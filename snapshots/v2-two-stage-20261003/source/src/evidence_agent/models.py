"""Core data structures shared by the harness, pipelines and adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

VERDICTS: tuple[str, ...] = ("SUPPORTED", "PARTIAL", "CONTRADICTED", "INSUFFICIENT")
CERTAIN_VERDICTS: tuple[str, ...] = ("SUPPORTED", "CONTRADICTED")

FLAGS: tuple[str, ...] = (
    "STALE",
    "SOURCE_CONFLICT",
    "SCOPE_MISMATCH",
    "OMITTED_CONDITION",
    "UNIT_MISMATCH",
    "LOW_SOURCE_QUALITY",
)

QUEUE_TRIGGER_FLAGS: tuple[str, ...] = ("STALE", "SOURCE_CONFLICT")

CLAIM_TYPES: tuple[str, ...] = (
    "numeric",
    "scope",
    "exclusive",
    "certification",
    "health_safety",
    "causal",
    "timeline",
    "other",
)

CORE_CLAIM_TYPES: tuple[str, ...] = (
    "numeric",
    "certification",
    "health_safety",
    "exclusive",
)


def should_review(verdict: str, flags: Iterable[str], claim_type: str) -> bool:
    """Apply the review-queue rule from docs/02-labeling-rules.md.

    CONTRADICTED / PARTIAL always queue. STALE or SOURCE_CONFLICT always queue.
    INSUFFICIENT queues only when the claim is a core claim type.
    """

    flag_set = set(flags)
    if verdict in ("CONTRADICTED", "PARTIAL"):
        return True
    if flag_set & set(QUEUE_TRIGGER_FLAGS):
        return True
    if verdict == "INSUFFICIENT" and claim_type in CORE_CLAIM_TYPES:
        return True
    return False


@dataclass(frozen=True)
class Source:
    id: str
    tier: str
    date: str
    text: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Source":
        return cls(
            id=str(raw["id"]),
            tier=str(raw.get("tier", "")),
            date=str(raw.get("date", "")),
            text=str(raw.get("text", "")),
        )


@dataclass(frozen=True)
class ExpectedClaim:
    text: str
    verdict: str
    flags: tuple[str, ...]
    evidence: tuple[str, ...]
    in_review_queue: bool
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExpectedClaim":
        return cls(
            text=str(raw["text"]),
            verdict=str(raw["verdict"]),
            flags=tuple(str(f) for f in raw.get("flags", [])),
            evidence=tuple(str(e) for e in raw.get("evidence", [])),
            in_review_queue=bool(raw.get("in_review_queue", False)),
            note=str(raw.get("note", "")),
        )


@dataclass(frozen=True)
class TaskCase:
    id: str
    category: str
    difficulty: str
    content: str
    sources: tuple[Source, ...]
    expected_claims: tuple[ExpectedClaim, ...]
    requires_human: bool
    what_it_tests: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskCase":
        return cls(
            id=str(raw["id"]),
            category=str(raw.get("category", "")),
            difficulty=str(raw.get("difficulty", "")),
            content=str(raw.get("content", "")),
            sources=tuple(Source.from_dict(s) for s in raw.get("sources", [])),
            expected_claims=tuple(
                ExpectedClaim.from_dict(c) for c in raw.get("expected_claims", [])
            ),
            requires_human=bool(raw.get("requires_human", False)),
            what_it_tests=str(raw.get("what_it_tests", "")),
        )

    def source_map(self) -> dict[str, Source]:
        return {source.id: source for source in self.sources}


@dataclass
class PredictedClaim:
    text: str
    verdict: str
    flags: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    claim_type: str = "other"
    reasoning: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PredictedClaim":
        verdict = str(raw.get("verdict", "INSUFFICIENT")).upper().strip()
        if verdict not in VERDICTS:
            verdict = "INSUFFICIENT"
        flags = tuple(
            str(f).upper().strip() for f in raw.get("flags", []) if str(f).strip()
        )
        evidence = raw.get("evidence", [])
        if isinstance(evidence, str):
            evidence = [evidence]
        claim_type = str(raw.get("claim_type", "other")).strip().lower()
        if claim_type not in CLAIM_TYPES:
            claim_type = "other"
        return cls(
            text=str(raw.get("text", raw.get("claim", ""))).strip(),
            verdict=verdict,
            flags=flags,
            evidence=tuple(str(e).strip() for e in evidence),
            claim_type=claim_type,
            reasoning=str(raw.get("reasoning", "")).strip(),
        )

    @property
    def in_review_queue(self) -> bool:
        return should_review(self.verdict, self.flags, self.claim_type)


@dataclass
class ModelResponse:
    text: str
    model_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    error: str | None = None
    error_code: str | None = None
    finish_reason: str | None = None
    raw_body: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class CallRecord:
    step: str
    response: ModelResponse
    request_messages: list[dict[str, str]] = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    category: str
    difficulty: str
    predicted_claims: list[PredictedClaim] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    error: str | None = None
    parse_error: str | None = None
    format_violation: str | None = None
    duration_ms: int = 0

    @property
    def failed(self) -> bool:
        return self.error is not None or self.parse_error is not None

    @property
    def prompt_tokens(self) -> int:
        return sum(c.response.prompt_tokens for c in self.calls)

    @property
    def completion_tokens(self) -> int:
        return sum(c.response.completion_tokens for c in self.calls)

    @property
    def model_calls(self) -> int:
        return len(self.calls)


def coerce_verdict(value: Any) -> str:
    verdict = str(value or "").upper().strip()
    return verdict if verdict in VERDICTS else "INSUFFICIENT"


def coerce_flags(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence):
        return ()
    cleaned = []
    for item in values:
        flag = str(item).upper().strip()
        if flag in FLAGS and flag not in cleaned:
            cleaned.append(flag)
    return tuple(cleaned)

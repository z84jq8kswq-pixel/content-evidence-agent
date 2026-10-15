"""Offline stub adapter: a deliberately naive baseline model.

This adapter does not read the expected labels. It reacts to the prompt only,
using simple heuristics, and always answers with a definite verdict. That makes
it a useful baseline: the harness should report a high
``unsupported_certainty_rate`` for it, which proves the metric discriminates.

It exists so the evaluation pipeline can be exercised without network access or
an API key. Results from this adapter must never be reported as model quality.
"""

from __future__ import annotations

import re
from typing import Any

from .base import ModelAdapter

CONTENT_BLOCK = re.compile(r"待核查内容[^\n：:]*[：:]\s*\n?(.*?)\n\n可用资料", re.S)
SOURCES_BLOCK = re.compile(r"可用资料[：:]\s*\n(.*)", re.S)
CLAIM_LINE = re.compile(r"需要判断的主张[：:]\s*(.+)")
CLAIM_TYPE_LINE = re.compile(r"主张类型[：:]\s*(\S+)")
SOURCE_ID = re.compile(r"^\[(S\d+)\]", re.M)
TASK_MARKER = re.compile(r"\[task:([a-z_]+)\]")

SENTENCE_SPLIT = re.compile(r"[。！？；\n]")
CLAIM_HINT = re.compile(
    r"\d|认证|专利|独家|唯一|第一|最强|最高|支持|含|续航|推荐|复购|降解|防水|浓度|减重"
)


class StubAdapter(ModelAdapter):
    name = "stub"

    def _invoke(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        system = next(
            (m["content"] for m in messages if m.get("role") == "system"), ""
        )
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        marker = TASK_MARKER.search(system)
        task = marker.group(1) if marker else "judge_all"

        sources = SOURCES_BLOCK.search(user)
        first_source = "S1"
        if sources:
            ids = SOURCE_ID.findall(sources.group(1))
            if ids:
                first_source = ids[0]

        if task == "extract_claims":
            return self._extract(user), {"prompt_tokens": len(user), "completion_tokens": 64}
        if task == "judge_claim":
            return self._judge(user, first_source), {
                "prompt_tokens": len(user),
                "completion_tokens": 48,
            }
        return self._judge_all(user, first_source), {
            "prompt_tokens": len(user),
            "completion_tokens": 96,
        }

    def _sentences(self, user: str) -> list[str]:
        block = CONTENT_BLOCK.search(user)
        content = block.group(1).strip() if block else user
        parts = [p.strip(" ，、,.") for p in SENTENCE_SPLIT.split(content)]
        return [p for p in parts if len(p) >= 6]

    def _extract(self, user: str) -> str:
        sentences = self._sentences(user)
        hits = [s for s in sentences if CLAIM_HINT.search(s)]
        chosen = (hits or sentences)[:3]
        claims = [{"text": text, "claim_type": "other"} for text in chosen]
        import json

        return json.dumps({"claims": claims}, ensure_ascii=False)

    def _judge(self, user: str, first_source: str) -> str:
        import json

        claim_match = CLAIM_LINE.search(user)
        claim = claim_match.group(1).strip() if claim_match else ""
        type_match = CLAIM_TYPE_LINE.search(user)
        claim_type = type_match.group(1).strip() if type_match else "other"
        return json.dumps(
            {
                "text": claim,
                "claim_type": claim_type,
                "verdict": "SUPPORTED",
                "flags": [],
                "evidence": [first_source],
                "reasoning": "资料中出现了与该主张相关的内容，因此判断为支持。",
            },
            ensure_ascii=False,
        )

    def _judge_all(self, user: str, first_source: str) -> str:
        import json

        sentences = self._sentences(user)
        hits = [s for s in sentences if CLAIM_HINT.search(s)]
        chosen = (hits or sentences)[:3]
        claims = [
            {
                "text": text,
                "claim_type": "other",
                "verdict": "SUPPORTED",
                "flags": [],
                "evidence": [first_source],
                "reasoning": "资料中出现了与该主张相关的内容，因此判断为支持。",
            }
            for text in chosen
        ]
        return json.dumps({"claims": claims}, ensure_ascii=False)

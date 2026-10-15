"""DeepSeek adapter using the OpenAI-compatible chat completions API.

Uses only the standard library so the harness runs without installing anything.
Configuration is read from the run config; the API key comes from the
environment variable named in the config (default ``DEEPSEEK_API_KEY``).

With ``base_url = https://api.deepseek.com`` the request target is
``https://api.deepseek.com/chat/completions``.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

from .base import AdapterConfigError, FatalAdapterError, ModelAdapter, RetryableError


class DeepSeekAdapter(ModelAdapter):
    name = "deepseek"

    def __init__(
        self,
        model_id: str,
        *,
        base_url: str = "https://api.deepseek.com",
        api_key_env: str = "DEEPSEEK_API_KEY",
        json_mode: bool = True,
        reasoning_effort: str | None = None,
        thinking: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_id, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.json_mode = json_mode
        self.reasoning_effort = reasoning_effort
        self.thinking = thinking
        self.last_raw_body = None
        self._api_key = os.environ.get(api_key_env, "").strip()
        if not self._api_key:
            raise AdapterConfigError(
                f"环境变量 {api_key_env} 未设置，无法调用真实模型。"
                f"可以先使用 --adapter stub 跑通评测管线。"
            )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _invoke(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        effort = reasoning_effort if reasoning_effort is not None else self.reasoning_effort
        thinking_mode = thinking if thinking is not None else self.thinking
        if effort:
            payload["reasoning_effort"] = effort
        if thinking_mode:
            payload["thinking"] = {"type": thinking_mode}

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
            self.last_raw_body = body
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 429:
                raise RetryableError("RATE_LIMITED", f"触发限流：{detail}") from exc
            if exc.code >= 500:
                raise RetryableError("SERVER_ERROR", f"服务端错误 {exc.code}：{detail}") from exc
            raise FatalAdapterError(
                "HTTP_ERROR", f"请求失败 {exc.code}：{detail}"
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise RetryableError("TIMEOUT", f"请求超时（{self.timeout_s}s）") from exc
        except urllib.error.URLError as exc:
            raise RetryableError("NETWORK_ERROR", f"网络错误：{exc.reason}") from exc

        choices = body.get("choices") or []
        if not choices:
            raise RetryableError(
                "EMPTY_RESPONSE", f"返回体没有 choices：{str(body)[:300]}"
            )

        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        finish_reason = choice.get("finish_reason")
        usage = body.get("usage") or {}

        if not text.strip():
            # Reasoning-heavy models can spend the whole max_tokens budget on
            # hidden reasoning and return an empty final answer. This is
            # usually recoverable, so it is treated as a retryable failure and
            # the message carries enough detail to diagnose it without a rerun.
            raise RetryableError(
                "EMPTY_RESPONSE",
                "模型返回了空内容"
                f"（finish_reason={finish_reason}，"
                f"reasoning_content 长度={len(reasoning)}，"
                f"completion_tokens={usage.get('completion_tokens', 0)}，"
                f"max_tokens={max_tokens}）",
            )

        return text, {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "finish_reason": finish_reason,
        }

"""Minimal chat-completions client for the bundled runtime.

Replaces the third-party ``openai`` package with a ~40-line ``requests``-based
client so the one-click package does not need the openai/httpx2/pydantic
dependency chain (which repeatedly failed to install intact on this machine).
Only the chat-completions subset used by agent.py / iterative_discovery.py is
implemented: ``client.chat.completions.create(...)`` returning an object with
``.choices[0].finish_reason`` and ``.choices[0].message.content``.

The endpoint is an OpenAI-compatible chat API (DeepSeek by default).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import requests

#: Hard per-call ceiling for one LLM request. The webapp must never hang
#: indefinitely on a single model call; this is a floor even if config sets a
#: larger LLM_TIMEOUT.
HARD_LLM_TIMEOUT = 45.0


@dataclass
class _Config:
    api_key: str
    base_url: str
    timeout: float
    max_retries: int


class ChatCompletions:
    def __init__(self, cfg: _Config):
        self._cfg = cfg

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        **_: Any,
    ) -> Any:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if response_format is not None:
            body["response_format"] = response_format
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        url = self._cfg.base_url.rstrip("/") + "/chat/completions"
        last_exc: Exception | None = None
        for _attempt in range(self._cfg.max_retries + 1):
            try:
                resp = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._cfg.api_key}",
                        "Content-Type": "application/json",
                    },
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    timeout=self._cfg.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:  # connection / HTTP / JSON
                last_exc = exc
        else:
            raise last_exc  # type: ignore[misc]
        choices = [
            SimpleNamespace(
                finish_reason=c.get("finish_reason"),
                message=SimpleNamespace(content=(c.get("message") or {}).get("content")),
            )
            for c in data.get("choices", [])
        ]
        return SimpleNamespace(choices=choices)


class _Chat:
    def __init__(self, cfg: _Config):
        self.completions = ChatCompletions(cfg)


class OpenAI:
    """Drop-in replacement for ``openai.OpenAI`` (chat completions only)."""

    def __init__(self, *, api_key: str, base_url: str, timeout: float = 60.0, max_retries: int = 0, **_: Any):
        effective_timeout = min(float(timeout), HARD_LLM_TIMEOUT)
        self.chat = _Chat(_Config(api_key=api_key, base_url=base_url, timeout=effective_timeout, max_retries=max_retries))

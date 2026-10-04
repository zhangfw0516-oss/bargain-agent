"""The single LLM entry point for every agent.

Every agent calls :func:`call_json`: load a prompt from ``prompts/``, send it with
the agent's input, require a JSON object back, and run the agent's validator on it.
Empty, truncated, non-JSON or invalid output is retried with the error fed back to
the model; anything still wrong raises :class:`LLMError`, which callers turn into a
deterministic fallback. Model output is never trusted without a validator.

The transport is a small ``requests``-based OpenAI-compatible chat client (DeepSeek
by default). Each agent can use its own model/endpoint through
``LLM_MODEL_<AGENT>`` / ``LLM_API_BASE_URL_<AGENT>`` / ``LLM_API_KEY_<AGENT>``,
falling back to the global ``LLM_*`` settings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import requests

import config

PROMPTS_DIR = Path(__file__).with_name("prompts")

#: Hard per-request ceiling; a configured LLM_TIMEOUT above this is clamped.
HARD_LLM_TIMEOUT = 45.0
MAX_RESPONSE_TOKENS = 4096
MAX_INPUT_CHARS = 24_000


class LLMError(RuntimeError):
    """Configuration, transport or response error. Messages never contain secrets."""


@dataclass
class _Endpoint:
    api_key: str
    base_url: str
    timeout: float
    max_retries: int


class _Completions:
    def __init__(self, endpoint: _Endpoint):
        self._endpoint = endpoint

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
        url = self._endpoint.base_url.rstrip("/") + "/chat/completions"
        last_exc: Exception | None = None
        for _attempt in range(self._endpoint.max_retries + 1):
            try:
                resp = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._endpoint.api_key}",
                        "Content-Type": "application/json",
                    },
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    timeout=self._endpoint.timeout,
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


class ChatClient:
    """OpenAI-compatible chat client: ``client.chat.completions.create(...)``."""

    def __init__(self, *, api_key: str, base_url: str, timeout: float = 30.0, max_retries: int = 1):
        endpoint = _Endpoint(api_key, base_url, min(float(timeout), HARD_LLM_TIMEOUT), max_retries)
        self.chat = SimpleNamespace(completions=_Completions(endpoint))


def is_configured(agent: str | None = None) -> bool:
    """True when key, endpoint and model are all set for ``agent`` (or globally)."""
    try:
        config.llm_settings(agent)
    except config.ConfigError:
        return False
    return True


def make_client(agent: str | None = None) -> tuple[ChatClient, str]:
    """Build a client and resolve the model for ``agent`` from configuration."""
    try:
        api_key, base_url, model = config.llm_settings(agent)
        timeout = config.llm_timeout()
    except config.ConfigError as exc:
        raise LLMError(str(exc)) from None
    return ChatClient(api_key=api_key, base_url=base_url, timeout=timeout), model


def load_prompt(name: str) -> str:
    return PROMPTS_DIR.joinpath(name).read_text(encoding="utf-8")


def call_json(
    prompt_name: str,
    payload: Any,
    *,
    validate: Callable[[Any], Any] | None = None,
    agent: str | None = None,
    client: Any = None,
    model: str | None = None,
    retries: int = 1,
    max_tokens: int = MAX_RESPONSE_TOKENS,
) -> Any:
    """Run one agent prompt and return its validated JSON output.

    ``payload`` is sent as the user message (dicts/lists as JSON). ``validate``
    receives the parsed object and returns the cleaned value or raises
    ``ValueError``/``TypeError``; up to ``retries`` extra attempts are made with the
    error fed back. Transport failures are not retried here (the client retries once).
    """
    if client is None:
        client, resolved = make_client(agent)
        model = model or resolved
    elif not model:
        try:
            model = config.llm_settings(agent)[2]
        except config.ConfigError as exc:
            raise LLMError(str(exc)) from None

    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    if not content.strip():
        raise LLMError("Empty input.")
    if len(content) > MAX_INPUT_CHARS:
        raise LLMError(f"Input exceeds {MAX_INPUT_CHARS} characters.")

    messages = [
        {"role": "system", "content": load_prompt(prompt_name)},
        {"role": "user", "content": content},
    ]
    last_error = "Empty model response."
    for _attempt in range(retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
            )
            choice = response.choices[0]
            finish_reason = choice.finish_reason
            text = choice.message.content
        except Exception as exc:
            raise LLMError(f"LLM request failed ({type(exc).__name__}).") from None

        if finish_reason == "length":
            last_error = "Model response was truncated."
            continue
        if not text or not text.strip():
            last_error = "Empty model response."
            continue
        try:
            data = json.loads(text)
            return validate(data) if validate else data
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            last_error = f"Invalid model output: {str(exc)[:200] or type(exc).__name__}"
            messages = messages[:2] + [
                {"role": "assistant", "content": text[:4000]},
                {
                    "role": "user",
                    "content": f"That output was rejected: {last_error}. Reply with corrected JSON only.",
                },
            ]
    raise LLMError(last_error)

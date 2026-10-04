"""Shared offline fakes: an LLM keyed by prompt name, HTTP senders, a notifier."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(*parts: str) -> Any:
    return json.loads(FIXTURES.joinpath(*parts).read_text(encoding="utf-8"))


class FakeLLM:
    """OpenAI-compatible fake. Responses are chosen by the system prompt's file.

    ``responses`` maps a prompt file name (e.g. ``"find_select.md"``) to either a
    JSON-serialisable object, a list of them (consumed in order), an Exception to
    raise, or a callable ``(user_content) -> object``. A raw string is returned
    verbatim (for malformed-output tests). Every call is recorded in ``calls``.
    """

    def __init__(self, responses: dict[str, Any] | None = None, *, finish_reason: str = "stop"):
        import llm

        self._prompt_by_text: dict[str, str] = {}
        for path in llm.PROMPTS_DIR.glob("*.md"):
            self._prompt_by_text[path.read_text(encoding="utf-8")] = path.name
        self.responses = dict(responses or {})
        self.finish_reason = finish_reason
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def prompts_called(self) -> list[str]:
        return [c["prompt"] for c in self.calls]

    def _create(self, *, model: str, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        prompt = self._prompt_by_text.get(messages[0]["content"], "<unknown>")
        user = messages[1]["content"]
        self.calls.append({"prompt": prompt, "model": model, "user": user, "messages": messages})
        if prompt not in self.responses:
            raise RuntimeError(f"FakeLLM has no response for {prompt}")
        value = self.responses[prompt]
        if isinstance(value, list):
            if not value:
                raise RuntimeError(f"FakeLLM responses for {prompt} exhausted")
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(user)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=self.finish_reason, message=SimpleNamespace(content=text))]
        )


class FakeSender:
    """A ``sources.base.Sender`` fake: records calls, returns queued responses.

    Each queued item is a JSON-able object (HTTP 200), a ``(status, object)``
    tuple, raw ``bytes``, an Exception to raise, or a callable
    ``(method, url, kwargs) -> item``.
    """

    def __init__(self, *responses: Any):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"Unexpected HTTP call: {method} {url}")
        value = self.responses.pop(0)
        if callable(value) and not isinstance(value, type):
            value = value(method, url, kwargs)
        if isinstance(value, Exception):
            raise value
        status = 200
        if isinstance(value, tuple):
            status, value = value
        body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        return status, body


class FakeNotifier:
    def __init__(self, fail_with: Exception | None = None):
        self.sent: list[dict[str, Any]] = []
        self.fail_with = fail_with

    def __call__(self, to: str, subject: str, body: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append({"to": to, "subject": subject, "body": body})


def static(value: Any) -> Callable[..., Any]:
    return lambda *a, **k: value

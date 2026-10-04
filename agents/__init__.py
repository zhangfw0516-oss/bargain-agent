"""The AI agents. Each one wraps a single prompt behind :func:`llm.call_json`,
validates the model's output locally, and falls back to deterministic code when
the model is unavailable or wrong.

Agents never fetch anything, never create tasks and never send email; they only
turn data the server already holds into a decision or text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Stage status values recorded in pipeline traces.
OK = "ok"
FALLBACK = "fallback"
SKIPPED = "skipped"


@dataclass
class LLMOptions:
    """Optional client/model override (tests inject a fake client here)."""

    client: Any = None
    model: str | None = None


@dataclass
class AgentResult:
    value: Any
    status: str
    note: str | None = None

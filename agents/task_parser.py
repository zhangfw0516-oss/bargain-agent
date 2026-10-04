"""Task-parser agent: a natural-language monitoring instruction -> task fields.

Used when the user pastes a product URL with a target price. The output must pass
:func:`schemas.validate_task`, which also checks that the URL was really supplied
by the user. Parsing never creates or starts a task.
"""

from __future__ import annotations

import llm
from agents import LLMOptions
from schemas import validate_task

NAME = "task_parser"
PROMPT = "parse_instruction.md"
MAX_INSTRUCTION_CHARS = 8000


class TaskParseError(RuntimeError):
    """The instruction could not be parsed; no task was created."""


def run(instruction: str, *, opts: LLMOptions | None = None) -> dict:
    opts = opts or LLMOptions()
    if not isinstance(instruction, str) or not instruction.strip():
        raise TaskParseError("Please enter a monitoring request.")
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise TaskParseError(f"Request exceeds {MAX_INSTRUCTION_CHARS} characters.")
    try:
        return llm.call_json(
            PROMPT,
            instruction,
            validate=lambda data: validate_task(data, instruction),
            agent=NAME,
            client=opts.client,
            model=opts.model,
        )
    except llm.LLMError as exc:
        raise TaskParseError(f"Could not parse the request: {exc}") from None

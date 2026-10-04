"""Rewrite agent: structured need -> Google Shopping search queries."""

from __future__ import annotations

from typing import Any

import llm
from agents import FALLBACK, OK, AgentResult, LLMOptions

NAME = "rewrite"
PROMPT = "rewrite.md"
MAX_QUERY_CHARS = 100

_OPERATOR_MARKERS = (
    "site:", "inurl:", "intitle:", "intext:", "filetype:", "related:", "cache:", "link:", '"', " or ", " -",
)


def sanitize(query: Any) -> str | None:
    """A plain keyword query, or None when it is empty, too long or uses operators."""
    if not isinstance(query, str):
        return None
    text = " ".join(query.split())
    if not text or len(text) > MAX_QUERY_CHARS:
        return None
    lowered = f" {text.lower()}"
    if any(marker in lowered for marker in _OPERATOR_MARKERS):
        return None
    return text


def clean(raw: Any, *, max_queries: int, exclude: list[str] | None = None) -> list[str]:
    if not isinstance(raw, dict) or not isinstance(raw.get("queries"), list):
        raise ValueError('expected {"queries": [...]}')
    seen = {q.lower() for q in exclude or []}
    queries: list[str] = []
    for item in raw["queries"]:
        query = sanitize(item)
        if query and query.lower() not in seen:
            seen.add(query.lower())
            queries.append(query)
        if len(queries) >= max_queries:
            break
    if not queries:
        raise ValueError("no usable query (operators, duplicates or empty strings)")
    return queries


def fallback_queries(description: str, need: dict[str, Any] | None) -> list[str]:
    need = need or {}
    parts = [need.get("brand"), need.get("model") or need.get("category"), need.get("colour")]
    candidates = [" ".join(p for p in parts if p), " ".join(str(description or "").split())[:MAX_QUERY_CHARS]]
    out: list[str] = []
    for candidate in candidates:
        query = sanitize(candidate)
        if query and query.lower() not in {q.lower() for q in out}:
            out.append(query)
    return out[:1] or ([" ".join(str(description).split())[:MAX_QUERY_CHARS]] if str(description).strip() else [])


def run(
    description: str,
    need: dict[str, Any] | None,
    *,
    max_queries: int,
    previous_queries: list[str] | None = None,
    feedback: str | None = None,
    opts: LLMOptions | None = None,
) -> AgentResult:
    opts = opts or LLMOptions()
    payload: dict[str, Any] = {"description": description, "need": need or {}, "max_queries": max_queries}
    if previous_queries:
        payload["previous_queries"] = previous_queries
    if feedback:
        payload["feedback"] = feedback
    try:
        queries = llm.call_json(
            PROMPT,
            payload,
            validate=lambda raw: clean(raw, max_queries=max_queries, exclude=previous_queries),
            agent=NAME,
            client=opts.client,
            model=opts.model,
        )
        return AgentResult(queries, OK)
    except llm.LLMError as exc:
        if previous_queries:
            return AgentResult([], FALLBACK, f"Rewrite agent unavailable for the retry ({exc}).")
        return AgentResult(fallback_queries(description, need), FALLBACK, f"Rewrite agent unavailable ({exc}); using a keyword query.")

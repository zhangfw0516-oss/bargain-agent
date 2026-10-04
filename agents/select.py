"""Select agent: choose concrete products from server-observed evidence.

The agent may only return evidence ids the server assigned; anything else is
dropped. Budget, condition and accessory vetoes are re-applied in code afterwards
(:func:`pipeline.build_recommendations`), so the model cannot widen the result.
"""

from __future__ import annotations

from typing import Any

import llm
from agents import FALLBACK, OK, AgentResult, LLMOptions

NAME = "select"
PROMPT = "select.md"


def evidence_summary(item: dict[str, Any]) -> dict[str, Any]:
    """The fields the model sees for one evidence item (no links, no tokens)."""
    return {
        "id": item["id"],
        "title": item.get("title"),
        "merchant": item.get("merchant"),
        "price": item.get("price"),
        "currency": item.get("currency"),
        "condition": item.get("condition") or "new/unknown",
        "trusted": bool(item.get("trusted")),
        "source": item.get("provider"),
    }


def clean(raw: Any, known_ids: set[str]) -> tuple[list[str], int, str | None]:
    if not isinstance(raw, dict) or not isinstance(raw.get("selected"), list):
        raise ValueError('expected {"selected": [...]}')
    picked: list[str] = []
    unknown = 0
    for item in raw["selected"]:
        if not isinstance(item, str):
            continue
        key = item.strip()
        if key in known_ids:
            if key not in picked:
                picked.append(key)
        else:
            unknown += 1
    if raw["selected"] and not picked:
        raise ValueError("none of the selected ids exist in the evidence")
    reasoning = raw.get("reasoning") if isinstance(raw.get("reasoning"), str) else None
    return picked, unknown, (reasoning or "")[:300] or None


def fallback_order(evidence: list[dict[str, Any]]) -> list[str]:
    """Priced, new, trusted first; observed order otherwise."""
    ranked = sorted(
        evidence,
        key=lambda e: (e.get("price") is None, e.get("condition") is not None, not e.get("trusted")),
    )
    return [e["id"] for e in ranked]


def run(
    description: str,
    need: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    *,
    opts: LLMOptions | None = None,
) -> AgentResult:
    opts = opts or LLMOptions()
    if not evidence:
        return AgentResult([], OK, None)
    known = {e["id"] for e in evidence}
    payload = {"description": description, "need": need or {}, "evidence": [evidence_summary(e) for e in evidence]}
    try:
        picked, unknown, reasoning = llm.call_json(
            PROMPT, payload, validate=lambda raw: clean(raw, known), agent=NAME, client=opts.client, model=opts.model
        )
    except llm.LLMError as exc:
        return AgentResult(fallback_order(evidence), FALLBACK, f"Select agent unavailable ({exc}); ranking by price and merchant.")
    note = f"Select agent referenced {unknown} unknown id(s); ignored." if unknown else None
    if not picked and reasoning:
        note = f"Select agent found nothing suitable: {reasoning}"
    return AgentResult(picked, OK, note)

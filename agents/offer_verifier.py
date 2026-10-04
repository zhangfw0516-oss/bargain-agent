"""Offer-verifier agent: during monitoring, veto seller offers that are not the
monitored product (wrong model, bundle, accessory, second-hand).

The agent can only *downgrade* an offer. Offers already rejected by the
deterministic pre-filter never reach it, and an offer it calls "same" stays
exactly as the pre-filter left it. When the agent is unavailable every offer it
was asked about becomes ``unverified``: still usable for the deal decision, but it
blocks an event reset (see ``scheduler``).
"""

from __future__ import annotations

from typing import Any

import llm
from agents import FALLBACK, OK, AgentResult, LLMOptions

NAME = "offer_verifier"
PROMPT = "verify_offers.md"

SAME = "same"
DIFFERENT = "different"
NEEDS_CONFIRMATION = "needs_confirmation"
UNVERIFIED = "unverified"
_VERDICTS = (SAME, DIFFERENT, NEEDS_CONFIRMATION)


def clean(raw: Any, ids: set[str]) -> dict[str, tuple[str, str | None]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("verdicts"), list):
        raise ValueError('expected {"verdicts": [...]}')
    out: dict[str, tuple[str, str | None]] = {}
    for item in raw["verdicts"]:
        if not isinstance(item, dict):
            continue
        key, verdict = item.get("id"), item.get("verdict")
        if key in ids and verdict in _VERDICTS and key not in out:
            reason = item.get("reason") if isinstance(item.get("reason"), str) else None
            out[key] = (verdict, (reason or "")[:200] or None)
    if not out:
        raise ValueError("no verdict for any known offer id")
    return out


def run(product: dict[str, Any], offers: list[dict[str, Any]], *, opts: LLMOptions | None = None) -> AgentResult:
    """``offers`` items carry ``id``, ``merchant``, ``title``, ``condition``,
    ``price``, ``currency``. Returns ``{id: (verdict, reason)}`` for every offer."""
    opts = opts or LLMOptions()
    if not offers:
        return AgentResult({}, OK)
    ids = {o["id"] for o in offers}
    try:
        verdicts = llm.call_json(
            PROMPT,
            {"product": product, "offers": offers},
            validate=lambda raw: clean(raw, ids),
            agent=NAME,
            client=opts.client,
            model=opts.model,
        )
    except llm.LLMError as exc:
        return AgentResult({i: (UNVERIFIED, None) for i in ids}, FALLBACK, f"Offer verifier unavailable ({exc}).")
    for key in ids - set(verdicts):
        verdicts[key] = (UNVERIFIED, "verifier gave no verdict")
    return AgentResult(verdicts, OK)

"""Deal-summary agent: a short plain-text summary at the top of an alert email.

An enhancement only: on any failure the caller keeps the deterministic body.
"""

from __future__ import annotations

from typing import Any

import llm
from agents import LLMOptions

NAME = "deal_summary"
PROMPT = "summarise_deal.md"
MAX_CHARS = 1200


def _clean(raw: Any) -> str:
    text = raw.get("summary") if isinstance(raw, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ValueError('expected {"summary": "..."}')
    return " ".join(text.split())[:MAX_CHARS]


def run(task: dict[str, Any], quote: dict[str, Any], *, opts: LLMOptions | None = None) -> str | None:
    opts = opts or LLMOptions()
    payload = {
        "product_name": task.get("product_name"),
        "target": f"{task.get('comparison')} {task.get('currency')} {task.get('target_price')}",
        "best": {
            "retailer": quote.get("retailer"),
            "price": quote.get("price"),
            "currency": quote.get("currency"),
            "in_stock": quote.get("in_stock"),
        },
        "other_sources": quote.get("other_sources") or [],
    }
    try:
        return llm.call_json(PROMPT, payload, validate=_clean, agent=NAME, client=opts.client, model=opts.model)
    except llm.LLMError:
        return None

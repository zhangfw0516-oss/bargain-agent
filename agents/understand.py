"""Understand agent: free-text shopping description -> structured need."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import llm
from agents import FALLBACK, OK, AgentResult, LLMOptions

NAME = "understand"
PROMPT = "understand.md"

_TEXT_FIELDS = {"category": 120, "brand": 60, "model": 80, "colour": 40, "use_case": 80}
_LIST_FIELDS = ("key_specs", "exclusions")
_CURRENCIES = ("NZD", "AUD", "USD")
_CONDITIONS = ("new", "used", "refurbished")


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def clean(raw: Any) -> dict[str, Any]:
    """Normalise the model's JSON. Raises ValueError when nothing usable remains."""
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object")
    need: dict[str, Any] = {}
    for field, limit in _TEXT_FIELDS.items():
        value = _text(raw.get(field), limit)
        if value:
            need[field] = value
    for field in _LIST_FIELDS:
        items = raw.get(field)
        if isinstance(items, list):
            values = [v for v in (_text(i, 60) for i in items) if v][:8]
            if values:
                need[field] = values
    budget = raw.get("budget")
    if budget not in (None, "") and not isinstance(budget, bool):
        try:
            amount = Decimal(str(budget))
        except InvalidOperation:
            raise ValueError("budget must be a number") from None
        if amount.is_finite() and amount > 0:
            need["budget"] = str(amount.quantize(Decimal("0.01")))
    currency = raw.get("currency")
    if isinstance(currency, str) and currency.strip().upper() in _CURRENCIES:
        need["currency"] = currency.strip().upper()
    condition = raw.get("condition")
    if isinstance(condition, str) and condition.strip().lower() in _CONDITIONS:
        need["condition"] = condition.strip().lower()
    if not need.get("category") and not need.get("model") and not need.get("brand"):
        raise ValueError("no category, brand or model identified")
    return need


def run(description: str, *, opts: LLMOptions | None = None) -> AgentResult:
    opts = opts or LLMOptions()
    try:
        need = llm.call_json(
            PROMPT, {"description": description}, validate=clean, agent=NAME, client=opts.client, model=opts.model
        )
        return AgentResult(need, OK)
    except llm.LLMError as exc:
        return AgentResult({}, FALLBACK, f"Understand agent unavailable ({exc}); using the raw description.")

"""Describe agent: a short Chinese summary per recommended product."""

from __future__ import annotations

from typing import Any

import llm
from agents import FALLBACK, OK, AgentResult, LLMOptions

NAME = "describe"
PROMPT = "describe.md"
MAX_ITEMS = 8
MAX_SUMMARY_CHARS = 600


def _item(rec: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": rec["id"],
        "title": rec.get("title"),
        "merchant": rec.get("merchant"),
        "price": rec.get("price"),
        "currency": rec.get("currency"),
        "condition": rec.get("condition"),
        "trusted": bool(rec.get("trusted")),
    }
    return {k: v for k, v in item.items() if v is not None}


def clean(raw: Any, ids: set[str]) -> dict[str, str]:
    if not isinstance(raw, dict) or not isinstance(raw.get("summaries"), dict):
        raise ValueError('expected {"summaries": {...}}')
    out = {
        key: " ".join(text.split())[:MAX_SUMMARY_CHARS]
        for key, text in raw["summaries"].items()
        if key in ids and isinstance(text, str) and text.strip()
    }
    if not out:
        raise ValueError("no summary for any known id")
    return out


def template(rec: dict[str, Any]) -> str:
    parts = [rec.get("title") or "该商品"]
    if rec.get("merchant"):
        parts.append(f"商家 {rec['merchant']}" + ("" if rec.get("trusted") else "（商家未验证）"))
    if rec.get("price"):
        parts.append(f"售价 {rec['price']} {rec.get('currency') or '（币种未注明）'}")
    else:
        parts.append("价格未注明")
    return "，".join(parts) + "。"


def run(description: str, need: dict[str, Any] | None, recommendations: list[dict[str, Any]], *, opts: LLMOptions | None = None) -> AgentResult:
    opts = opts or LLMOptions()
    items = [_item(r) for r in recommendations[:MAX_ITEMS]]
    if not items:
        return AgentResult({}, OK)
    ids = {i["id"] for i in items}
    try:
        summaries = llm.call_json(
            PROMPT,
            {"description": description, "need": need or {}, "products": items},
            validate=lambda raw: clean(raw, ids),
            agent=NAME,
            client=opts.client,
            model=opts.model,
        )
        status, note = OK, None
    except llm.LLMError as exc:
        summaries, status, note = {}, FALLBACK, f"Describe agent unavailable ({exc}); using template summaries."
    for rec in recommendations:
        summaries.setdefault(rec["id"], template(rec))
    return AgentResult(summaries, status, note)

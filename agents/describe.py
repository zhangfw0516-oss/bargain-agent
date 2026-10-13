"""Describe agent: a short English summary, with a Chinese version, per recommended product."""

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


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:MAX_SUMMARY_CHARS]


def clean(raw: Any, ids: set[str]) -> dict[str, dict[str, str | None]]:
    """``{id: {"en": …, "zh": …}}``; a plain string (older format) is kept as ``en``."""
    if not isinstance(raw, dict) or not isinstance(raw.get("summaries"), dict):
        raise ValueError('expected {"summaries": {...}}')
    out: dict[str, dict[str, str | None]] = {}
    for key, value in raw["summaries"].items():
        if key not in ids:
            continue
        if isinstance(value, dict):
            en, zh = _text(value.get("en")), _text(value.get("zh"))
        else:
            en, zh = _text(value), None
        if en or zh:
            out[key] = {"en": en, "zh": zh}
    if not out:
        raise ValueError("no summary for any known id")
    return out


def template(rec: dict[str, Any]) -> dict[str, str]:
    title = rec.get("title")
    en = [title or "This product"]
    zh = [title or "该商品"]
    if rec.get("merchant"):
        en.append(f"sold by {rec['merchant']}" + ("" if rec.get("trusted") else " (unverified merchant)"))
        zh.append(f"商家 {rec['merchant']}" + ("" if rec.get("trusted") else "（商家未验证）"))
    if rec.get("price"):
        en.append(f"price {rec['price']} {rec.get('currency') or '(currency not stated)'}")
        zh.append(f"售价 {rec['price']} {rec.get('currency') or '（币种未注明）'}")
    else:
        en.append("price not stated")
        zh.append("价格未注明")
    return {"en": ", ".join(en) + ".", "zh": "，".join(zh) + "。"}


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

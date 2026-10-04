"""Demo mode for the Stage D local web interface.

Everything here is offline and deterministic, so the web acceptance flow never
calls the real LLM, never scrapes real shops and never sends email:

- `parse_demo_request`: a tiny bilingual (EN/ZH) stand-in for the LLM parser that
  produces a schema v2 search request.
- `DemoRetailer`: two fake shops whose candidate list deliberately covers every
  identity verdict (same / needs_confirmation / different / accessory / wrong
  generation) so the UI can show all of them.
- `demo_fetch_offer` / `demo_check_fetcher`: fixed quotes for the demo URLs,
  including out-of-stock, unknown-stock, accessory (NZD 19) and injectable
  failures, so the UI can display failures and unknown states honestly.
- `fake_notify_deal` / `RECORDED_SENDS`: the notification stand-in. Pass it
  explicitly as `send_fn=` — scheduler.check_task binds its keyword default at
  import time, so patching the module attribute would NOT take effect.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from retailers.base import Candidate, Retailer

# --------------------------------------------------------------------------- parse

_COLOUR_WORDS = {
    "black": "black", "white": "white", "blue": "blue", "red": "red",
    "green": "green", "pink": "pink", "silver": "silver", "grey": "grey",
    "gray": "grey", "gold": "gold", "purple": "purple", "orange": "orange",
    "yellow": "yellow", "lavender": "lavender", "obsidian": "obsidian",
}
_ZH_COLOUR_WORDS = {
    "黑色": "black", "白色": "white", "蓝色": "blue", "藍色": "blue",
    "红色": "red", "紅色": "red", "绿色": "green", "粉色": "pink",
    "银色": "silver", "銀色": "silver", "灰色": "grey", "金色": "gold",
    "紫色": "purple", "橙色": "orange", "黄色": "yellow",
}
_CURRENCIES = {"NZD", "USD", "AUD"}


def parse_demo_request(instruction: str) -> dict[str, Any]:
    """Deterministic bilingual parser: real LLM parsing is NOT used in demo mode."""
    text = str(instruction or "").strip()
    lowered = text.lower()

    # Colour (Chinese first, then English word match). The raw word is kept so
    # schemas.validate_search_request sees it in the instruction (anti-invention
    # guard); the canonical mapping happens in discovery.
    colour = None
    for zh in _ZH_COLOUR_WORDS:
        if zh in text:
            colour = zh
            break
    if colour is None:
        for en in _COLOUR_WORDS:
            if re.search(rf"\b{en}\b", lowered):
                colour = en
                break

    # Budget + currency: "budget NZD 200" / "预算 200 纽币" / "under 200".
    budget = None
    currency = None
    match = re.search(r"(?:budget|预算|under|低于|以下)\s*([a-z]{3})?\s*[$]?\s*(\d+(?:\.\d+)?)", lowered)
    if match:
        currency = (match.group(1) or "").upper() or None
        budget = match.group(2)
    if currency is None:
        if "纽币" in text or "新西兰元" in text:
            currency = "NZD"
        elif "美元" in text:
            currency = "USD"
        elif re.search(r"\bnzd\b", lowered):
            currency = "NZD"
        elif re.search(r"\baud\b", lowered):
            currency = "AUD"
    if budget is not None:
        budget = f"{float(budget):.2f}"

    # Frequency: "every 60 minutes" / "每小时" / "每 60 分钟"; default once per day (1440 minutes).
    frequency = 1440
    match = re.search(r"every\s+(\d+)\s*(minutes?|hours?)", lowered)
    if match:
        frequency = int(match.group(1)) * (60 if match.group(2).startswith("hour") else 1)
    else:
        match = re.search(r"每\s*(\d+)\s*分钟", text)
        if match:
            frequency = int(match.group(1))
        elif "每小时" in text:
            frequency = 60

    # Capacity: "128gb" / "128gb" style.
    capacity = None
    match = re.search(r"\b(\d+\s?(?:gb|tb))\b", lowered)
    if match:
        capacity = match.group(1).replace(" ", "").lower()

    # Product name: strip the budget/colour/frequency scaffolding words.
    name = re.sub(
        r"(find|search for|monitor|找|查找|搜|帮我找|i want|我想买|我要买)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    for zh in _ZH_COLOUR_WORDS:
        name = name.replace(zh, " ")
    name = re.sub(
        r"\b(black|white|blue|red|green|pink|silver|grey|gray|gold|purple|orange|yellow|lavender|obsidian)\b",
        " ",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(
        r"(budget|预算|under|低于|以下)\s*([a-z]{3})?\s*[$]?\s*\d+(\.\d+)?", " ", name, flags=re.IGNORECASE
    )
    name = re.sub(r"纽币|新西兰元|美元", " ", name)
    name = re.sub(r"every\s+\d+\s*(minutes?|hours?)|每\s*\d+\s*分钟(查一次|查一次价)?|每小时(查一次)?",
                  " ", name, flags=re.IGNORECASE)
    name = re.sub(r"查一次|查询一次|查一次价|\bcheck\b|监控|监视", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"\b\d+\s?(gb|tb)\b", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[,，。;；!！?？]", " ", name)
    name = " ".join(name.split())

    request: dict[str, Any] = {
        "schema_version": "2",
        "status": "ready",
        "product_name": name or None,
        "url": None,
        "budget": budget,
        "currency": currency,
        "colour": colour,
        "capacity": capacity,
        "condition": None,
        "comparison": "lte",
        "frequency_minutes": frequency,
        "missing_fields": [],
        "clarification_question": None,
    }
    if not name:
        request["status"] = "needs_clarification"
        request["missing_fields"] = ["product_name"]
        request["clarification_question"] = "Which product would you like to search for?"
    elif budget is None:
        request["status"] = "needs_clarification"
        request["missing_fields"] = ["budget"]
        request["clarification_question"] = (
            "What is your budget? (A target price is required before a monitor task "
            "can be created; you can still browse the comparison without one.)"
        )
    elif currency is None:
        request["status"] = "needs_clarification"
        request["missing_fields"] = ["currency"]
        request["clarification_question"] = "Which currency is the budget in (NZD/USD/AUD)?"
    return request


# --------------------------------------------------------------------------- shops

_DEMO_PRODUCT = "AeroBuds Pro"


class DemoRetailer(Retailer):
    """A fake shop with a fixed candidate list covering every identity verdict."""

    def __init__(self, name: str, domain: str, candidates: list[Candidate]):
        self.name = name
        self.domains = (domain,)
        self._candidates = candidates

    def search(self, query, *, limit=10):
        return list(self._candidates)[:limit]

    def fetch_offer(self, url):
        if url in DEMO_QUOTES:
            return dict(DEMO_QUOTES[url])
        raise RuntimeError(f"No demo offer configured for {url}")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_demo_retailers() -> list[Retailer]:
    tech = DemoRetailer(
        "Demo Tech", "demo-tech.example",
        [
            Candidate(retailer="Demo Tech", url="https://demo-tech.example/aerobuds-pro-black",
                      title="AeroBuds Pro Wireless Earbuds - Black", sku="DT-1"),
            Candidate(retailer="Demo Tech", url="https://demo-tech.example/aerobuds-pro-lavender",
                      title="AeroBuds Pro Wireless Earbuds - Lavender", sku="DT-2"),
            Candidate(retailer="Demo Tech", url="https://demo-tech.example/aerobuds-one",
                      title="AeroBuds One Earbuds", sku="DT-3"),
            Candidate(retailer="Demo Tech", url="https://demo-tech.example/aerobuds-charging-cable",
                      title="AeroBuds Charging Cable", sku="DT-4"),
            Candidate(retailer="Demo Tech", url="https://demo-tech.example/aerobuds-case-cover",
                      title="AeroBuds Case Cover", sku="DT-5"),
        ],
    )
    mart = DemoRetailer(
        "Demo Mart", "demo-mart.example",
        [
            Candidate(retailer="Demo Mart", url="https://demo-mart.example/aerobuds-pro-black",
                      title="AeroBuds Pro Wireless Earbuds - Black", sku="DM-1"),
            Candidate(retailer="Demo Mart", url="https://demo-mart.example/aerobuds-pro-2",
                      title="AeroBuds Pro 2 Earbuds - Black", sku="DM-2"),
            Candidate(retailer="Demo Mart", url="https://demo-mart.example/aerobuds-pro-active-band",
                      title="AeroBuds Pro Active Band", sku="DM-3"),
            # States no colour at all: with a colour requested this must come back
            # needs_confirmation, never same.
            Candidate(retailer="Demo Mart", url="https://demo-mart.example/aerobuds-pro",
                      title="AeroBuds Pro Wireless Earbuds", sku="DM-4"),
        ],
    )
    return [tech, mart]


DEMO_QUOTES: dict[str, dict[str, Any]] = {
    "https://demo-tech.example/aerobuds-pro-black": {
        "product_name": "AeroBuds Pro Wireless Earbuds - Black",
        "url": "https://demo-tech.example/aerobuds-pro-black",
        "price": "199.00", "currency": "NZD", "in_stock": True,
        "fetched_at": None, "shipping_cost": None,
    },
    "https://demo-tech.example/aerobuds-pro-lavender": {
        "product_name": "AeroBuds Pro Wireless Earbuds - Lavender",
        "url": "https://demo-tech.example/aerobuds-pro-lavender",
        "price": "199.00", "currency": "NZD", "in_stock": True,
        "fetched_at": None, "shipping_cost": None,
    },
    "https://demo-mart.example/aerobuds-pro-black": {
        "product_name": "AeroBuds Pro Wireless Earbuds - Black",
        "url": "https://demo-mart.example/aerobuds-pro-black",
        "price": "189.00", "currency": "NZD", "in_stock": True,
        "fetched_at": None, "shipping_cost": None,
    },
    # Out of stock: must never be the best offer.
    "https://demo-tech.example/aerobuds-one": {
        "product_name": "AeroBuds One Earbuds",
        "url": "https://demo-tech.example/aerobuds-one",
        "price": "149.00", "currency": "NZD", "in_stock": False,
        "fetched_at": None, "shipping_cost": None,
    },
    # Unknown stock: must be reported as unknown, never as buyable.
    "https://demo-mart.example/aerobuds-pro-2": {
        "product_name": "AeroBuds Pro 2 Earbuds - Black",
        "url": "https://demo-mart.example/aerobuds-pro-2",
        "price": "249.00", "currency": "NZD", "in_stock": None,
        "fetched_at": None, "shipping_cost": None,
    },
    # Accessory page: cheap, must never trigger an alert (identity gate).
    "https://demo-mart.example/aerobuds-pro-active-band": {
        "product_name": "AeroBuds Pro Active Band",
        "url": "https://demo-mart.example/aerobuds-pro-active-band",
        "price": "19.00", "currency": "NZD", "in_stock": True,
        "fetched_at": None, "shipping_cost": None,
    },
    # Colour unstated: identity gate classifies as needs_confirmation.
    "https://demo-mart.example/aerobuds-pro": {
        "product_name": "AeroBuds Pro Wireless Earbuds",
        "url": "https://demo-mart.example/aerobuds-pro",
        "price": "179.00", "currency": "NZD", "in_stock": True,
        "fetched_at": None, "shipping_cost": None,
    },
}

#: URLs that fail when this flag is on — used to demonstrate an honest
#: per-source failure in the UI.
DEMO_FAIL_URLS: set[str] = set()


def _stamp(quote: dict[str, Any]) -> dict[str, Any]:
    quote = dict(quote)
    quote["fetched_at"] = quote.get("fetched_at") or _ts()
    return quote


def demo_check_fetcher(url: str) -> dict[str, Any]:
    """Fetcher injected into scheduler.check_task during demo mode."""
    if url in DEMO_FAIL_URLS:
        raise RuntimeError("Demo source is temporarily unavailable (injected failure)")
    if url in DEMO_QUOTES:
        return _stamp(DEMO_QUOTES[url])
    raise RuntimeError(f"No demo offer configured for {url}")


# ----------------------------------------------------------- iterative discovery
#
# The "intelligent find" flow needs an AI decision loop and a broad web search,
# neither of which may run for real in demo mode. These three collaborators give
# the loop a deterministic, offline brain: it searches once for a concrete demo
# product, asks to read every page it found, then finishes. The real business
# modules (the bounded loop, validation, later matching and comparison) are NOT
# faked — only the model decision, the web search and the page fetch are.

#: Concrete demo products the fake broad search "finds" for the demo description.
DEMO_FIND_RESULTS: list[dict[str, Any]] = [
    {"url": "https://demo-tech.example/aerobuds-pro-black",
     "title": "AeroBuds Pro Wireless Earbuds - Black", "source": "web_search"},
    {"url": "https://demo-mart.example/aerobuds-pro-black",
     "title": "AeroBuds Pro Wireless Earbuds - Black", "source": "web_search"},
    {"url": "https://demo-mart.example/aerobuds-pro",
     "title": "AeroBuds Pro Wireless Earbuds", "source": "web_search"},
    {"url": "https://demo-mart.example/aerobuds-pro-active-band",
     "title": "AeroBuds Pro Active Band", "source": "web_search"},
]


def demo_find_search(query: str) -> list[dict[str, Any]]:
    """Fake broad web search: real URLs+titles only, never a price."""
    return [dict(item) for item in DEMO_FIND_RESULTS]


def demo_find_fetch(url: str) -> dict[str, Any]:
    """Fake detail fetch built from the real demo quotes (no invented values)."""
    return demo_check_fetcher(url)


# ---------------------------------------------------------------------------
# Intelligent-find demo collaborators (5-stage pipeline)
#
# Each fake has exactly the shape of the real AI stage it stands in for, so the
# pipeline's local validation and fallback logic run unchanged offline.


def demo_find_understand(description: str) -> dict[str, Any]:
    """Fake stage 1: parse the description into a structured need."""
    return {
        "category": "wireless earbuds",
        "key_specs": ["black"],
        "budget": "200",
        "currency": "NZD",
        "colour": "black",
    }


def demo_find_rewrite(payload: dict[str, Any]) -> list[str]:
    """Fake stage 2: turn the need into search queries."""
    return ["AeroBuds Pro black", "AeroBuds wireless earbuds"]


def demo_find_select(payload: dict[str, Any]) -> list[str]:
    """Fake stage 4: pick the observed evidence ids in recommendation order."""
    return [e["provenance_id"] for e in payload.get("evidence", [])]


def demo_find_describe(payload: dict[str, Any]) -> dict[str, str]:
    """Fake stage 5: a Chinese summary per recommended product."""
    out: dict[str, str] = {}
    for item in payload.get("products", []):
        pid = item.get("provenance_id")
        title = item.get("title") or "该产品"
        price = item.get("price")
        currency = item.get("currency") or ""
        if pid:
            if price:
                out[pid] = f"{title}，{price} {currency}，有货。符合你的需求。"
            else:
                out[pid] = f"{title}，价格页面未写明。符合你的需求。"
    return out


# --------------------------------------------------------------------------- notify

#: Every notification produced in demo mode is recorded here instead of dialling
#: SMTP. Pass `send_fn=fake_notify_deal` explicitly to scheduler.check_task.
RECORDED_SENDS: list[dict[str, Any]] = []


def fake_notify_deal(email: str, task: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    RECORDED_SENDS.append(
        {"to": email, "task_id": task.get("id"), "quote_retailer": quote.get("retailer"),
         "price": quote.get("price"), "at": _ts()}
    )
    return {"success": True, "attempts": 1, "transport": "demo-stub"}

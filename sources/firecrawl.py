"""Firecrawl provider: supplementary product search and AI page extraction.

- :meth:`Firecrawl.search` (``POST /v1/search`` with markdown scraping) finds pages
  Google Shopping does not list. Its page content is only evidence for the price
  agent; a search result is never a price by itself.
- :meth:`Firecrawl.extract` (``POST /v1/scrape`` with a JSON schema) lets
  Firecrawl's own model return a structured price. The result must still pass
  :func:`schemas.validate_price_extraction` before it becomes a quote.
"""

from __future__ import annotations

from typing import Any, Callable

import config
from schemas import validate_price_extraction
from sources.base import (
    NotConfigured,
    ShoppingResult,
    Sender,
    Unavailable,
    clean_text,
    detect_condition,
    is_http_url,
    request_json,
)

BASE_URL = "https://api.firecrawl.dev/v1"
NAME = "firecrawl"
MAX_PAGE_CHARS = 60_000

PRICE_SCHEMA = {
    "type": "object",
    "properties": {
        "product_name": {"type": "string"},
        "price": {"type": ["string", "null"], "description": "Current selling price as digits, e.g. 1299.00"},
        "currency": {"type": ["string", "null"], "description": "ISO code: NZD, AUD or USD"},
        "in_stock": {"type": ["boolean", "null"]},
        "brand": {"type": ["string", "null"]},
        "colour": {"type": ["string", "null"]},
        "capacity": {"type": ["string", "null"]},
    },
    "required": ["product_name", "price", "currency", "in_stock"],
}
PRICE_PROMPT = (
    "Extract the single product sold on this page and the price a consumer pays now. "
    "When the page shows both a price excluding tax (+GST, ex GST) and one including tax "
    "(inc GST), use the tax-inclusive price. Use null for anything the page does not "
    "state. Never guess a price."
)


class Firecrawl:
    name = NAME

    def __init__(
        self,
        api_key: str | None = None,
        *,
        sender: Sender | None = None,
        timeout: float | None = None,
        before_call: Callable[[str], None] | None = None,
    ):
        self._api_key = api_key if api_key is not None else config.firecrawl_api_key()
        self._sender = sender
        self._timeout = timeout or config.firecrawl_timeout()
        self._before_call = before_call

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise NotConfigured("Firecrawl is not configured (missing FIRECRAWL_API_KEY).")
        if self._before_call is not None:
            self._before_call(NAME)
        data = request_json(
            "Firecrawl",
            "POST",
            BASE_URL + path,
            sender=self._sender,
            payload=payload,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        if not isinstance(data, dict) or data.get("success") is False:
            raise Unavailable(f"Firecrawl request failed: {clean_text((data or {}).get('error'), 120) or 'unknown error'}.")
        return data

    def search(self, query: str, *, limit: int = 5) -> list[ShoppingResult]:
        text = clean_text(query, 200)
        if not text:
            raise ValueError("Search query is empty.")
        data = self._post(
            "/search",
            {
                "query": text,
                "limit": max(1, min(limit, 10)),
                "country": "nz",
                "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
            },
        )
        results = []
        for item in data.get("data") or []:
            if not isinstance(item, dict) or not is_http_url(item.get("url")):
                continue
            title = clean_text(item.get("title") or (item.get("metadata") or {}).get("title"))
            if not title:
                continue
            markdown = str(item.get("markdown") or "").strip()
            results.append(
                ShoppingResult(
                    title=title,
                    link=str(item["url"]),
                    provider=NAME,
                    snippet=clean_text(item.get("description"), 200),
                    condition=detect_condition(title),
                    page_text=markdown[:MAX_PAGE_CHARS] or None,
                )
            )
        return results

    def extract(self, url: str) -> dict[str, Any]:
        if not is_http_url(url):
            raise ValueError("Only http(s) URLs can be extracted.")
        data = self._post(
            "/scrape",
            {"url": url, "formats": ["json"], "jsonOptions": {"schema": PRICE_SCHEMA, "prompt": PRICE_PROMPT}},
        )
        extracted = (data.get("data") or {}).get("json")
        if not isinstance(extracted, dict):
            raise Unavailable("Firecrawl returned no structured data for the page.")
        try:
            return validate_price_extraction({k: extracted.get(k) for k in PRICE_SCHEMA["properties"]})
        except ValueError as exc:
            raise Unavailable(f"Firecrawl extraction failed validation: {exc}") from None

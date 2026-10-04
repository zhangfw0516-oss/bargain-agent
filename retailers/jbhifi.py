"""JB Hi-Fi NZ search adapter.

Search page: ``/search?q=<query>``. The visible results are rendered client-side, but the
page server-renders an ``initData`` object inside a plain ``<script>`` that lists the
matched products by Shopify ``handle`` and ``id``:

    ...,initData: {"shop": {...}, "products": [{"id": "...", "handle": "...", ...}], ...}

So, exactly like PB Tech, the search page provides **identity** and the product page
provides **price**: candidates are built as ``/products/<handle>`` and priced through the
already-verified JSON-LD detail parser (``scraper.fetch_quote``). No CSS selectors are
needed for the result grid, which is the part that breaks most often.

Verified live on 2026-09-24:

- ``https://www.jbhifi.co.nz/search?q=fitbit+air`` → HTTP 200, ``initData.products``
  lists handles such as ``google-fitbit-air-obsidian``.
- ``https://www.jbhifi.co.nz/products/google-fitbit-air-obsidian`` → JSON-LD
  ``Product``/``Offer`` → NZD 239.00, ``InStock``, name "Google Fitbit Air (Obsidian)".

Known honest limitation: the shop's search is fuzzy. A nonsense query still returns
matches (the engine token-matches, so "zzqqxxnotathing" returns "Nothing Phone ..."
items). The adapter therefore **never claims "no results"** — it returns whatever the
shop offered and lets deterministic matching reject things that are not the requested
product. Reporting "we found nothing" here would be a false negative.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

import scraper

from .base import Candidate, Retailer, RetailerError, normalise_space, title_from_slug

logger = logging.getLogger(__name__)

BASE = "https://www.jbhifi.co.nz/"
SEARCH_PATH = "search"
SEARCH_QUERY_PARAM = "q"
INIT_DATA_MARKER = "initData:"


def _balanced_object(text: str, start: int) -> str | None:
    """Return the brace-balanced substring starting at ``start``, or None.

    String-aware so quoted braces (which do occur in embedded JSON) cannot end it early.
    """
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_init_data(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Return the page's ``initData`` object, or None if this shape is absent."""
    for script in soup.find_all("script"):
        content = script.string or script.get_text() or ""
        if not content:
            continue
        marker = content.find(INIT_DATA_MARKER)
        if marker < 0:
            continue
        raw = _balanced_object(content, content.find("{", marker))
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("products"), list):
            return data
    return None


class JbHiFiRetailer(Retailer):
    name = "JB Hi-Fi"
    domains = ("jbhifi.co.nz",)

    def search_url(self, query: str) -> str:
        return f"{BASE}{SEARCH_PATH}?{SEARCH_QUERY_PARAM}={quote_plus(query)}"

    def search(self, query: str, *, limit: int = 10) -> list[Candidate]:
        if not query or not query.strip():
            raise RetailerError("Search query is empty.")
        url = self.search_url(query)
        try:
            final_url, body = scraper.fetch_html(url)
        except scraper.ScraperError as exc:
            raise RetailerError(f"JB Hi-Fi search could not be read: {exc}") from exc
        return self.parse_search_page(body, final_url, limit=limit)

    def parse_search_page(self, body: bytes, page_url: str, *, limit: int = 10) -> list[Candidate]:
        soup = BeautifulSoup(body, "html.parser")
        data = extract_init_data(soup)

        if data is None:
            # Without the server-rendered payload we cannot tell "no matches" from
            # "the page changed". Raising is the honest outcome: an empty list would be
            # reported as "the shop had nothing", which may be false.
            raise RetailerError(
                "JB Hi-Fi search page format was not recognised; no product payload found."
            )

        products = [p for p in data.get("products") or [] if isinstance(p, dict)]
        if not products:
            return []

        candidates: list[Candidate] = []
        seen: set[str] = set()
        for product in products:
            handle = normalise_space(str(product.get("handle") or ""))
            if not handle or handle in seen:
                continue
            seen.add(handle)
            # The slug is machine-generated from the product name, so it is a faithful
            # identity signal (colour word included) and the only one the search page
            # exposes. SKU/price are left unknown: they come from the detail page.
            candidates.append(
                Candidate(
                    retailer=self.name,
                    url=f"{BASE}products/{handle}",
                    title=title_from_slug(handle),
                    sku=None,
                    variant=None,
                )
            )
            if len(candidates) >= limit:
                break
        logger.info("JB Hi-Fi search page %s -> %s candidates", page_url, len(candidates))
        return candidates

    def fetch_offer(self, url: str) -> dict[str, Any]:
        try:
            return scraper.fetch_quote(url)
        except scraper.ScraperError as exc:
            raise RetailerError(f"JB Hi-Fi page could not be read: {exc}") from exc

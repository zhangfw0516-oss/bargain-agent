"""PB Tech search adapter.

Search page: ``/search?sf=<query>`` renders one ``div.js-product-card`` per result.
Each card links to ``product/<SKU>/<slug>`` and exposes ``data-product-code`` (the
SKU). Prices are intentionally **not** read from the search page: the detail page
carries the authoritative JSON-LD offer, and reusing it keeps one parsing path for
price, currency and stock.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

import scraper

from .base import Candidate, Retailer, RetailerError, normalise_space, title_from_slug

logger = logging.getLogger(__name__)

BASE = "https://www.pbtech.co.nz/"
CARD_SELECTOR = "div.js-product-card"
LINK_SELECTOR = "a.js-product-link"

#: Verified against a live page on 2026-09-23. The card also contains an
#: ``h3.np_title`` holding *marketing bullets* ("- Up to 7 Day Battery Life - ..."),
#: so a generic ``h3``/``h4`` selector picks up promo copy instead of the product
#: name. Only these specific selectors are trusted; otherwise the URL slug is used,
#: which is machine-generated from the product name.
TITLE_SELECTORS = (
    "h2.js-main-categoty-product-title-breakdown",
    ".product-title-holder h2",
    "a.js-product-title",
)


class PbTechRetailer(Retailer):
    name = "PB Tech"
    domains = ("pbtech.co.nz",)

    def search_url(self, query: str) -> str:
        return f"{BASE}search?sf={quote_plus(query)}"

    def search(self, query: str, *, limit: int = 10) -> list[Candidate]:
        if not query or not query.strip():
            raise RetailerError("Search query is empty.")
        url = self.search_url(query)
        final_url, body = scraper.fetch_html(url)
        return self.parse_search_page(body, final_url, limit=limit)

    def parse_search_page(self, body: bytes, page_url: str, *, limit: int = 10) -> list[Candidate]:
        soup = BeautifulSoup(body, "html.parser")
        cards = soup.select(CARD_SELECTOR)
        if not cards:
            # Distinguish "no matches" from "we no longer understand this page".
            if soup.select_one(LINK_SELECTOR) or soup.select_one("[data-product-code]"):
                return []
            text = soup.get_text(" ", strip=True).lower()
            if "no result" in text or "did not match" in text or "0 product" in text:
                return []
            raise RetailerError(
                "PB Tech search page format was not recognised; no product cards found."
            )

        candidates: list[Candidate] = []
        seen: set[str] = set()
        for card in cards:
            candidate = self._candidate_from_card(card, page_url)
            if candidate is None or candidate.sku in seen:
                continue
            seen.add(candidate.sku or candidate.url)
            candidates.append(candidate)
            if len(candidates) >= limit:
                break
        logger.info("PB Tech search page %s -> %s candidates", page_url, len(candidates))
        return candidates

    def _candidate_from_card(self, card: Any, page_url: str) -> Candidate | None:
        link = card.select_one(LINK_SELECTOR) or card.find("a", href=True)
        if link is None:
            return None
        href = link.get("href") or ""
        if "product/" not in href:
            return None
        absolute = urljoin(page_url, href.split("?")[0])
        sku = link.get("data-product-code") or card.get("data-product-code")

        title = self._title_from_card(card, link)
        if not title:
            return None
        sku_value = normalise_space(str(sku)) if sku else ""
        return Candidate(
            retailer=self.name,
            url=absolute,
            title=title,
            sku=sku_value or None,
            variant=None,
        )

    @staticmethod
    def _title_from_card(card: Any, link: Any) -> str:
        for selector in TITLE_SELECTORS:
            node = card.select_one(selector)
            if node is not None:
                text = normalise_space(node.get_text(" ", strip=True))
                if text:
                    return text
        # Fall back to the product-name part of the slug, which is derived from the
        # product name and is always present.
        href = link.get("href") or ""
        parts = [p for p in href.split("?")[0].split("/") if p]
        if len(parts) >= 3:
            return title_from_slug(parts[2])
        # Last resort: the link's own text, trimmed at the first bullet separator.
        text = normalise_space(link.get_text(" ", strip=True))
        if text:
            return text.split(" - ")[0].strip()
        return ""

    def fetch_offer(self, url: str) -> dict[str, Any]:
        try:
            return scraper.fetch_quote(url)
        except scraper.ScraperError as exc:
            raise RetailerError(f"PB Tech page could not be read: {exc}") from exc

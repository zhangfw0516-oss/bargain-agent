"""Mighty Ape search adapter.

Search page: ``/search?query=<query>``, per the shop's historical pattern. Parsing
prefers structured data: a JSON-LD ``ItemList`` of products is read first, and only if
that is absent does the adapter fall back to product links. Prices are not taken from
the search page; the detail page is read for the authoritative offer.

Verification status (2026-09-24, B1): the domain spelling is now correct
(``mightyape.co.nz``). The live site **is reachable** from this environment, but every
request is refused by the shop's bot-protection service:

    GET https://www.mightyape.co.nz/                     -> 403 Forbidden
    GET https://www.mightyape.co.nz/search?q=fitbit+air  -> 403 Forbidden
    response header: server: DataDome

That is a site-side access control, not a proxy failure, and it is **not** bypassed
here (no forged User-Agent, no cookie or challenge handling). Consequences to keep in
mind:

- Neither ``SEARCH_PATH``/``SEARCH_QUERY_PARAM`` nor the page structure below has been
  confirmed against the live site. Treat them as **unverified assumptions**; when access
  becomes possible, re-check the search URL and the JSON-LD shape first.
- Offline fixture tests prove the parser's logic only. The fixture is synthetic and is
  **not** evidence about the real page.
- At runtime the adapter raises ``RetailerError``, and discovery reports the source as
  failed with the HTTP error, rather than pretending the shop had no results.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

import scraper

from .base import Candidate, Retailer, RetailerError, normalise_space, title_from_slug

logger = logging.getLogger(__name__)

BASE = "https://www.mightyape.co.nz/"

#: UNVERIFIED (see module docstring): the live site refuses automated requests with
#: HTTP 403 from DataDome, so this path and query parameter have not been confirmed.
#: Kept as single constants so a future verified run only has to change one place.
SEARCH_PATH = "search"
SEARCH_QUERY_PARAM = "query"


class MightyApeRetailer(Retailer):
    name = "Mighty Ape"
    domains = ("mightyape.co.nz",)

    def search_url(self, query: str) -> str:
        return f"{BASE}{SEARCH_PATH}?{SEARCH_QUERY_PARAM}={quote_plus(query)}"

    def search(self, query: str, *, limit: int = 10) -> list[Candidate]:
        if not query or not query.strip():
            raise RetailerError("Search query is empty.")
        url = self.search_url(query)
        try:
            final_url, body = scraper.fetch_html(url)
        except scraper.ScraperError as exc:
            raise RetailerError(f"Mighty Ape search could not be read: {exc}") from exc
        return self.parse_search_page(body, final_url, limit=limit)

    def parse_search_page(self, body: bytes, page_url: str, *, limit: int = 10) -> list[Candidate]:
        soup = BeautifulSoup(body, "html.parser")

        candidates = self._from_jsonld(soup, page_url)
        source = "json-ld"
        if not candidates:
            candidates = self._from_links(soup, page_url)
            source = "links"

        if not candidates:
            text = soup.get_text(" ", strip=True).lower()
            if any(marker in text for marker in ("no result", "no products", "0 results", "did not match")):
                return []
            raise RetailerError(
                "Mighty Ape search page format was not recognised; no product data found."
            )

        deduped: list[Candidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = candidate.sku or candidate.url
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
            if len(deduped) >= limit:
                break
        logger.info("Mighty Ape search page %s -> %s candidates (via %s)", page_url, len(deduped), source)
        return deduped

    def _from_jsonld(self, soup: BeautifulSoup, page_url: str) -> list[Candidate]:
        out: list[Candidate] = []
        for block in scraper.extract_jsonld(soup):
            items: list[Any] = []
            if isinstance(block, dict) and block.get("@type") == "ItemList":
                items = block.get("itemListElement") or []
            for item in items:
                product = item.get("item") if isinstance(item, dict) else None
                if not isinstance(product, dict):
                    product = item if isinstance(item, dict) else None
                if not isinstance(product, dict) or product.get("@type") != "Product":
                    continue
                url = product.get("url")
                name = normalise_space(str(product.get("name") or ""))
                if not url or not name:
                    continue
                offers = product.get("offers")
                offer = offers if isinstance(offers, dict) else (offers[0] if isinstance(offers, list) and offers else None)
                sku = normalise_space(str(product.get("sku"))) if product.get("sku") else ""
                model = normalise_space(str(product.get("model"))) if product.get("model") else ""
                price = None
                currency = None
                if isinstance(offer, dict):
                    if offer.get("price") is not None:
                        price = str(offer["price"])
                    if offer.get("priceCurrency"):
                        currency = str(offer["priceCurrency"]).upper()
                out.append(
                    Candidate(
                        retailer=self.name,
                        url=urljoin(page_url, url),
                        title=name,
                        sku=sku or None,
                        model=model or None,
                        price=price,
                        currency=currency,
                    )
                )
        return out

    def _from_links(self, soup: BeautifulSoup, page_url: str) -> list[Candidate]:
        out: list[Candidate] = []
        for link in soup.find_all("a", href=True):
            href = link["href"].split("?")[0]
            if "/product/" not in href and not href.startswith("product/"):
                continue
            title = normalise_space(link.get_text(" ", strip=True))
            if not title:
                title = title_from_slug(href.rstrip("/").split("/")[-1])
            if not title:
                continue
            out.append(
                Candidate(
                    retailer=self.name,
                    url=urljoin(page_url, href),
                    title=title,
                )
            )
        return out

    def fetch_offer(self, url: str) -> dict[str, Any]:
        try:
            return scraper.fetch_quote(url)
        except scraper.ScraperError as exc:
            raise RetailerError(f"Mighty Ape page could not be read: {exc}") from exc

"""Serper.dev Google Shopping provider: the primary search and monitoring source.

``POST https://google.serper.dev/shopping`` returns Google Shopping listings with the
same ``productId`` values SerpApi reports, at a lower cost (2 credits per search).
Serper has no product-detail (immersive) endpoint, so an offer is the listing itself:
no shipping and no store page link. Most listings also come without a link, so one is
built from the product id (the Google Shopping product URL SerpApi returns).

The key travels only in the ``X-API-KEY`` header.
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from urllib.parse import quote_plus

import config
from sources.base import (
    NotConfigured,
    Offer,
    ProductRef,
    Sender,
    ShoppingResult,
    clean_text,
    detect_condition,
    is_http_url,
    listing_offer,
    parse_price,
    request_json,
)

logger = logging.getLogger(__name__)

ENDPOINT = "https://google.serper.dev/shopping"
NAME = "serper"
MAX_RESULTS = 40


def product_link(product_id: str, query: str, locale: dict[str, str]) -> str:
    domain = locale.get("google_domain") or "google.co.nz"
    gl = locale.get("gl") or "nz"
    return (f"https://www.{domain}/search?ibp=oshop&q={quote_plus(query)}"
            f"&prds=productid:{quote_plus(product_id)}&gl={gl}&udm=28")


class Serper:
    name = NAME

    def __init__(
        self,
        api_key: str | None = None,
        *,
        sender: Sender | None = None,
        locale: dict[str, str] | None = None,
        timeout: float | None = None,
        before_call: Callable[[str], None] | None = None,
    ):
        self._api_key = api_key if api_key is not None else config.serper_api_key()
        self._sender = sender
        self._locale = locale or config.serpapi_locale()
        self._timeout = timeout or config.serper_timeout()
        self._before_call = before_call
        self.calls = 0

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def search(self, query: str, *, limit: int = 20) -> list[ShoppingResult]:
        text = clean_text(query, 200)
        if not text:
            raise ValueError("Search query is empty.")
        if not self._api_key:
            raise NotConfigured("Serper is not configured (missing SERPER_API_KEY).")
        if self._before_call is not None:
            self._before_call(NAME)
        self.calls += 1
        payload = {"q": text, "gl": self._locale.get("gl") or "nz", "hl": self._locale.get("hl") or "en"}
        if self._locale.get("location"):
            payload["location"] = self._locale["location"]
        data = request_json(
            "Serper", "POST", ENDPOINT, sender=self._sender, payload=payload,
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"}, timeout=self._timeout,
        )
        results: list[ShoppingResult] = []
        seen: set[str] = set()
        for item in (data.get("shopping") if isinstance(data, dict) else None) or []:
            result = self._result(item, text)
            if result is None:
                continue
            key = result.product_id or result.link
            if key in seen:
                continue
            seen.add(key)
            results.append(result)
            if len(results) >= min(limit, MAX_RESULTS):
                break
        logger.info("Serper shopping search returned %s result(s)", len(results))
        return results

    def offers(self, ref: ProductRef) -> tuple[list[Offer], ProductRef]:
        """The listing for ``ref.product_id`` as found by re-running its search."""
        if not (ref.product_id and ref.search_query):
            return [], ref
        for result in self.search(ref.search_query, limit=MAX_RESULTS):
            if result.product_id == ref.product_id:
                offer = listing_offer(result)
                return ([offer] if offer else []), ref
        return [], ref

    def _result(self, item: Any, query: str) -> ShoppingResult | None:
        if not isinstance(item, dict):
            return None
        title = clean_text(item.get("title"))
        product_id = clean_text(item.get("productId"), 80)
        link = item.get("link") if is_http_url(item.get("link")) else None
        if not link and product_id:
            link = product_link(product_id, query, self._locale)
        if not title or not link:
            return None
        default = {"nz": "NZD", "au": "AUD", "us": "USD"}.get((self._locale.get("gl") or "").lower())
        price, currency = parse_price(item.get("price"), None, default_dollar=default)
        return ShoppingResult(
            title=title,
            link=link,
            provider=NAME,
            merchant=clean_text(item.get("source"), 80),
            price=price,
            currency=currency,
            product_id=product_id,
            thumbnail=item.get("imageUrl") if is_http_url(item.get("imageUrl")) else None,
            condition=detect_condition(title),
            snippet=clean_text(item.get("delivery"), 80),
        )


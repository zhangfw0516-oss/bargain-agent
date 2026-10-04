"""SerpApi Google Shopping provider: product search and per-product seller offers.

Search uses ``engine=google_shopping``. Offers for one product use
``engine=google_immersive_product`` when a page token is known, otherwise the
classic ``engine=google_product`` with the product id. A stale page token is
refreshed by re-running the original search and matching the product id.

SerpApi takes its key as the ``api_key`` query parameter, so errors are rebuilt
without the URL (see :func:`sources.base.request_json`) and the key is never logged.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import config
from sources.base import (
    NotConfigured,
    Offer,
    ProductRef,
    QuotaExceeded,
    Sender,
    ShoppingResult,
    Unavailable,
    clean_text,
    detect_condition,
    is_http_url,
    parse_price,
    request_json,
)

logger = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search.json"
NAME = "serpapi"
MAX_RESULTS = 40

#: SerpApi reports "no results" as HTTP 200 with an ``error`` field.
_NO_RESULTS_MARKERS = ("hasn't returned any results", "has not returned any results", "no results")


class SerpApi:
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
        self._api_key = api_key if api_key is not None else config.serpapi_api_key()
        self._sender = sender
        self._locale = locale or config.serpapi_locale()
        self._timeout = timeout or config.serpapi_timeout()
        #: Called with the provider name before every billable request (quota guard).
        self._before_call = before_call
        self.calls = 0

    def is_configured(self) -> bool:
        return bool(self._api_key)

    # -- public API -------------------------------------------------------

    def search(self, query: str, *, limit: int = 20) -> list[ShoppingResult]:
        text = clean_text(query, 200)
        if not text:
            raise ValueError("Search query is empty.")
        data = self._get({"engine": "google_shopping", "q": text})
        if data is None:
            return []
        results: list[ShoppingResult] = []
        seen: set[str] = set()
        for item in list(data.get("shopping_results") or []) + list(data.get("inline_shopping_results") or []):
            result = _shopping_result(item, self._locale)
            if result is None:
                continue
            key = result.product_id or result.link
            if key in seen:
                continue
            seen.add(key)
            results.append(result)
            if len(results) >= min(limit, MAX_RESULTS):
                break
        logger.info("SerpApi shopping search returned %s result(s)", len(results))
        return results

    def offers(self, ref: ProductRef) -> tuple[list[Offer], ProductRef]:
        """All sellers' current offers for one product, plus a refreshed ref."""
        if ref.page_token:
            data = self._get({"engine": "google_immersive_product", "page_token": ref.page_token})
            if data is not None:
                offers = _immersive_offers(data, self._locale)
                if offers:
                    return offers, ref
        if ref.product_id:
            data = self._get({"engine": "google_product", "product_id": ref.product_id, "offers": "1"})
            if data is not None:
                offers = _product_offers(data, self._locale)
                if offers:
                    return offers, ref
        if ref.product_id and ref.search_query:
            # The page token expired or the product page was empty: find the product
            # again by id and retry once with its fresh token.
            for result in self.search(ref.search_query, limit=MAX_RESULTS):
                if result.product_id == ref.product_id and result.page_token and result.page_token != ref.page_token:
                    fresh = ProductRef(ref.product_id, result.page_token, ref.search_query, ref.title)
                    data = self._get({"engine": "google_immersive_product", "page_token": fresh.page_token})
                    if data is not None:
                        return _immersive_offers(data, self._locale), fresh
                    break
        return [], ref

    # -- transport --------------------------------------------------------

    def _get(self, params: dict[str, Any]) -> dict[str, Any] | None:
        if not self._api_key:
            raise NotConfigured("SerpApi is not configured (missing SERPAPI_API_KEY).")
        if self._before_call is not None:
            self._before_call(NAME)
        self.calls += 1
        query = {**{k: v for k, v in self._locale.items() if v}, **params, "api_key": self._api_key}
        if params.get("engine") != "google_shopping":
            query.pop("location", None)
        data = request_json("SerpApi", "GET", ENDPOINT, sender=self._sender, params=query, timeout=self._timeout)
        if not isinstance(data, dict):
            raise Unavailable("SerpApi returned an unexpected response shape.")
        error = data.get("error")
        if error:
            if any(marker in str(error).lower() for marker in _NO_RESULTS_MARKERS):
                return None
            if "run out of searches" in str(error).lower():
                raise QuotaExceeded("SerpApi plan has run out of searches.")
            raise Unavailable(f"SerpApi error: {clean_text(error, 160)}")
        return data


def _dollar_default(locale: dict[str, str]) -> str | None:
    return {"nz": "NZD", "au": "AUD", "us": "USD"}.get((locale.get("gl") or "").lower())


def _shopping_result(item: Any, locale: dict[str, str]) -> ShoppingResult | None:
    if not isinstance(item, dict):
        return None
    title = clean_text(item.get("title"))
    link = item.get("product_link") or item.get("link")
    if not title or not is_http_url(link):
        return None
    price, currency = parse_price(item.get("price"), item.get("extracted_price"), default_dollar=_dollar_default(locale))
    condition = clean_text(item.get("second_hand_condition"), 40)
    return ShoppingResult(
        title=title,
        link=str(link),
        provider=NAME,
        merchant=clean_text(item.get("source"), 80),
        price=price,
        currency=currency,
        product_id=clean_text(item.get("product_id"), 80),
        page_token=item.get("immersive_product_page_token") if isinstance(item.get("immersive_product_page_token"), str) else None,
        thumbnail=item.get("thumbnail") if is_http_url(item.get("thumbnail")) else None,
        condition=detect_condition(condition, title) or ("used" if condition else None),
        snippet=clean_text(item.get("delivery") or item.get("snippet"), 200),
    )


_OUT_OF_STOCK = ("out of stock", "sold out", "unavailable", "currently unavailable")


def _offer(merchant: Any, title: Any, raw_price: Any, extracted: Any, link: Any, condition_text: Any, locale) -> Offer | None:
    name = clean_text(merchant, 80)
    if not name:
        return None
    price, currency = parse_price(raw_price, extracted, default_dollar=_dollar_default(locale))
    details = str(condition_text or "").lower()
    return Offer(
        merchant=name,
        price=price,
        currency=currency,
        link=str(link) if is_http_url(link) else None,
        provider=NAME,
        title=clean_text(title),
        condition=detect_condition(condition_text, title),
        # Google Shopping lists offers a merchant currently sells; an explicit
        # out-of-stock note is the only availability signal it carries.
        in_stock=not any(marker in details for marker in _OUT_OF_STOCK),
    )


def _immersive_offers(data: dict[str, Any], locale) -> list[Offer]:
    product = data.get("product_results") or {}
    stores = product.get("stores") if isinstance(product, dict) else None
    title = product.get("title") if isinstance(product, dict) else None
    offers = []
    for store in stores or []:
        if not isinstance(store, dict):
            continue
        details = " ".join(str(d) for d in store.get("details_and_offers") or [] if isinstance(d, (str, int, float)))
        offer = _offer(
            store.get("name"), store.get("title") or title, store.get("price"), store.get("extracted_price"),
            store.get("link"), " ".join(filter(None, [details, str(store.get("tag") or "")])), locale,
        )
        if offer:
            offers.append(offer)
    return offers


def _product_offers(data: dict[str, Any], locale) -> list[Offer]:
    title = (data.get("product_results") or {}).get("title") if isinstance(data.get("product_results"), dict) else None
    sellers = (data.get("sellers_results") or {}).get("online_sellers") if isinstance(data.get("sellers_results"), dict) else None
    offers = []
    for seller in sellers or []:
        if not isinstance(seller, dict):
            continue
        raw = seller.get("base_price") or seller.get("total_price") or seller.get("price")
        offer = _offer(
            seller.get("name"), seller.get("title") or title, raw, seller.get("extracted_price"),
            seller.get("link") or seller.get("direct_link"), seller.get("details_and_offers") or seller.get("condition"), locale,
        )
        if offer:
            offers.append(offer)
    return offers

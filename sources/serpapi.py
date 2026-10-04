"""SerpApi Google Shopping provider: product search and per-product seller offers.

Search uses ``engine=google_shopping``; each result is one seller's listing with a
``product_id`` and an ``immersive_product_page_token``. Offers for one product use
``engine=google_immersive_product`` with that token (Google retired the classic
``google_product`` engine). When the token no longer works, the original query is
searched again and the product is matched by id; if Google then shows no store list,
the listing itself is the single offer.

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
    ProviderError,
    QuotaExceeded,
    Sender,
    ShoppingResult,
    Unavailable,
    clean_text,
    detect_condition,
    is_http_url,
    listing_offer,
    parse_price,
    request_json,
)

logger = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search.json"
NAME = "serpapi"
MAX_RESULTS = 40

#: SerpApi reports "no results" as HTTP 200 with an ``error`` field.
_NO_RESULTS_MARKERS = ("hasn't returned any results", "has not returned any results", "no results")
_OUT_OF_STOCK = ("out of stock", "sold out", "currently unavailable", "unavailable online")


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
        items = list(data.get("shopping_results") or []) + list(data.get("inline_shopping_results") or [])
        for group in data.get("categorized_shopping_results") or []:
            if isinstance(group, dict):
                items.extend(group.get("shopping_results") or [])
        results: list[ShoppingResult] = []
        seen: set[str] = set()
        for item in items:
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
        """Every seller's current offer for one product, plus a refreshed ref."""
        if ref.page_token:
            offers = self._immersive(ref.page_token)
            if offers:
                return offers, ref
        if not (ref.product_id and ref.search_query):
            return [], ref
        # The token expired or listed no stores: find the product again by id.
        for result in self.search(ref.search_query, limit=MAX_RESULTS):
            if result.product_id != ref.product_id:
                continue
            fresh = ProductRef(ref.product_id, result.page_token or ref.page_token, ref.search_query, ref.title)
            if result.page_token and result.page_token != ref.page_token:
                offers = self._immersive(result.page_token)
                if offers:
                    return offers, fresh
            listing = listing_offer(result)
            return ([listing] if listing else []), fresh
        return [], ref

    # -- transport --------------------------------------------------------

    def _immersive(self, page_token: str) -> list[Offer]:
        try:
            data = self._get({"engine": "google_immersive_product", "page_token": page_token})
        except QuotaExceeded:
            raise
        except ProviderError as exc:
            if isinstance(exc, NotConfigured):
                raise
            logger.info("Immersive product lookup failed: %s", exc)
            return []
        return _immersive_offers(data, self._locale) if data else []

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
            lowered = str(error).lower()
            if any(marker in lowered for marker in _NO_RESULTS_MARKERS):
                return None
            if "run out of searches" in lowered:
                raise QuotaExceeded("SerpApi plan has run out of searches.")
            raise Unavailable(f"SerpApi error: {clean_text(error, 160)}")
        return data


def _dollar_default(locale: dict[str, str]) -> str | None:
    return {"nz": "NZD", "au": "AUD", "us": "USD"}.get((locale.get("gl") or "").lower())


def _conversion_note(item: dict[str, Any]) -> str | None:
    alt = item.get("alternative_price")
    if isinstance(alt, dict) and alt.get("price"):
        return f"converted from {clean_text(alt.get('price'), 30)}"
    return None


def _shopping_result(item: Any, locale: dict[str, str]) -> ShoppingResult | None:
    if not isinstance(item, dict):
        return None
    title = clean_text(item.get("title"))
    link = item.get("product_link") or item.get("link")
    if not title or not is_http_url(link):
        return None
    price, currency = parse_price(item.get("price"), item.get("extracted_price"), default_dollar=_dollar_default(locale))
    second_hand = clean_text(item.get("second_hand_condition"), 40)
    notes = [n for n in (_conversion_note(item), clean_text(item.get("delivery"), 80), clean_text(item.get("tag"), 30)) if n]
    token = item.get("immersive_product_page_token")
    return ShoppingResult(
        title=title,
        link=str(link),
        provider=NAME,
        merchant=clean_text(item.get("source"), 80),
        price=price,
        currency=currency,
        product_id=clean_text(item.get("product_id"), 80),
        page_token=token if isinstance(token, str) else None,
        thumbnail=item.get("thumbnail") if is_http_url(item.get("thumbnail")) else None,
        condition=detect_condition(second_hand, title) or ("used" if second_hand else None),
        snippet="; ".join(notes) or None,
    )


def _offer(merchant: Any, title: Any, raw_price: Any, extracted: Any, link: Any, details: Any, locale,
           shipping: Any = None) -> Offer | None:
    name = clean_text(merchant, 80)
    if not name:
        return None
    price, currency = parse_price(raw_price, extracted, default_dollar=_dollar_default(locale))
    detail_text = str(details or "").lower()
    ship = None
    if isinstance(shipping, (int, float)) and not isinstance(shipping, bool):
        ship = f"{float(shipping):.2f}"
    elif "free delivery" in detail_text or "free shipping" in detail_text:
        ship = "0.00"
    return Offer(
        merchant=name,
        price=price,
        currency=currency,
        link=str(link) if is_http_url(link) else None,
        provider=NAME,
        title=clean_text(title),
        condition=detect_condition(details, title),
        # Google Shopping lists offers a merchant currently sells; an explicit
        # out-of-stock note is the only availability signal it carries.
        in_stock=not any(marker in detail_text for marker in _OUT_OF_STOCK),
        shipping=ship,
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
            shipping=store.get("shipping_extracted"),
        )
        if offer:
            offers.append(offer)
    return offers


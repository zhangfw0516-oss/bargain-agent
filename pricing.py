"""Price observation shared by discovery and monitoring.

- :func:`observe_source` turns one monitored source into quotes: every seller of a
  Google Shopping product (``serpapi_product``) or one page read through the
  page-extractor chain (``url``).
- :func:`quota_guard` enforces the daily SerpApi budget before each billable call.
- :func:`merchant_trust` decides whether a merchant is on the trusted list.
- :func:`is_safe_public_url` screens user-pasted URLs before any provider sees them.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import config
import storage
from agents import LLMOptions
from agents import price_extractor
from sources.base import PageText, ProductRef, ProviderError, QuotaExceeded

logger = logging.getLogger(__name__)

#: Consecutive empty offer lists after which a product source is reported as gone.
EMPTY_CHECKS_BEFORE_WARNING = 3


class ObservationError(RuntimeError):
    """A source produced no usable observation; the scheduler records it as an error."""


def quota_guard(db_path: Path | None = None) -> Callable[[str], None]:
    """Count every billable provider call; refuse SerpApi calls over the daily cap."""

    def guard(provider: str) -> None:
        limit = config.serpapi_daily_max_calls() if provider == "serpapi" else None
        if not storage.count_api_call(provider, limit, db_path=db_path):
            raise QuotaExceeded(f"Daily {provider} call limit ({limit}) reached; try again tomorrow.")

    return guard


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def observe_source(
    source: dict[str, Any],
    task: dict[str, Any],
    *,
    providers: Any,
    db_path: Path | None = None,
    llm_opts: LLMOptions | None = None,
) -> list[dict[str, Any]]:
    """Quotes for one source. Raises :class:`ObservationError` when nothing usable came back."""
    kind = source.get("source_kind") or storage.SOURCE_URL
    if kind == storage.SOURCE_SERPAPI:
        return _observe_product(source, task, providers=providers, db_path=db_path)
    return [_observe_page(source, task, providers=providers, llm_opts=llm_opts)]


def _observe_product(source, task, *, providers, db_path) -> list[dict[str, Any]]:
    if providers.offers is None:
        raise ObservationError("Google Shopping offers are unavailable (SERPAPI_API_KEY not set).")
    ref = ProductRef(source.get("product_id"), source.get("page_token"), source.get("search_query"), task.get("product_name"))
    try:
        offers, fresh = providers.offers.offers(ref)
    except ProviderError as exc:
        raise ObservationError(str(exc)) from None
    if fresh.page_token and fresh.page_token != ref.page_token:
        storage.update_source_ref(source["id"], page_token=fresh.page_token, db_path=db_path)
    empty_count = storage.record_source_empty(source["id"], not offers, db_path=db_path)
    if not offers:
        if empty_count >= EMPTY_CHECKS_BEFORE_WARNING:
            raise ObservationError(
                f"No sellers listed for {empty_count} checks in a row; the product may have been removed. Run Find again."
            )
        raise ObservationError("Google Shopping listed no sellers for this product right now.")
    fetched_at = _now()
    quotes = []
    for offer in offers[: config.monitor_max_offers()]:
        quotes.append(
            {
                "product_name": offer.title or task.get("product_name"),
                "retailer": offer.merchant,
                "url": offer.link or source["url"],
                "price": offer.price,
                "currency": offer.currency,
                "in_stock": offer.in_stock,
                "condition": offer.condition,
                "fetched_at": fetched_at,
                "trusted": merchant_trust(offer.merchant, offer.link),
                "error": None,
            }
        )
    return quotes


def _observe_page(source, task, *, providers, llm_opts) -> dict[str, Any]:
    if not providers.extractors:
        raise ObservationError("No page extractor is configured (set TAVILY_API_KEY or FIRECRAWL_API_KEY).")
    errors = []
    for extractor in providers.extractors:
        try:
            page = extractor.extract(source["url"])
            if isinstance(page, PageText):
                extracted = price_extractor.run(page.text, opts=llm_opts)
            else:
                extracted = page
        except (ProviderError, price_extractor.PriceExtractionError, ValueError) as exc:
            errors.append(f"{extractor.name}: {exc}")
            continue
        variant = " ".join(v for v in (extracted.get("colour"), extracted.get("capacity")) if v) or None
        return {
            "product_name": extracted["product_name"],
            "brand": extracted.get("brand"),
            "variant": variant,
            "retailer": source.get("retailer") or host_of(source["url"]),
            "url": source["url"],
            "price": extracted["price"],
            "currency": extracted["currency"],
            "in_stock": extracted["in_stock"],
            "condition": None,
            "fetched_at": _now(),
            # The user chose this exact page, so it is trusted by definition.
            "trusted": True,
            "error": None,
        }
    raise ObservationError("; ".join(errors) or "The page could not be read.")


def default_observer(db_path: Path | None = None) -> Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]:
    from sources.registry import build_providers

    providers = build_providers(quota_guard(db_path))
    return lambda source, task: observe_source(source, task, providers=providers, db_path=db_path)


def _norm_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def host_of(url: str | None) -> str:
    host = (urlsplit(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def merchant_trust(merchant: str | None, link: str | None = None, trusted: list[str] | None = None) -> bool:
    """True when the merchant name or the link's domain is on the trusted list."""
    entries = trusted if trusted is not None else config.trusted_merchants()
    host = host_of(link)
    name = _norm_name(merchant or "")
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if "." in entry and " " not in entry:
            domain = entry.lower().removeprefix("www.")
            if host and (host == domain or host.endswith("." + domain)):
                return True
            if name and name == _norm_name(entry):
                return True
        elif name and name == _norm_name(entry):
            return True
    return False


def is_safe_public_url(url: str) -> bool:
    """http(s), a dotted public hostname, no credentials, default ports, no private IPs."""
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return False
    if port not in (None, 80, 443):
        return False
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan")):
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "." in host
    return address.is_global

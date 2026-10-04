"""Provider contracts shared by every external data source.

A provider is anything that finds products, lists a product's offers, or reads a
product page. New services plug in by implementing one of the protocols below and
being registered in :mod:`sources.registry`; nothing else in the app changes.

Safety rules every provider follows:

- API keys never appear in exception messages or logs. Transport exceptions are
  rebuilt from their type name only, because ``requests`` exceptions embed the URL
  and SerpApi carries its key in the query string.
- Timeouts and response-size caps are fixed by configuration, not by callers.
- Prices come only from structured provider data or a validated page extraction.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

import requests

MAX_RESPONSE_BYTES = 4_000_000


class ProviderError(RuntimeError):
    """A provider call could not be completed."""


class NotConfigured(ProviderError):
    """The provider has no API key; it is skipped."""


class AuthError(ProviderError):
    """Credentials were rejected."""


class RateLimited(ProviderError):
    """The provider throttled the request or the plan quota is exhausted."""


class QuotaExceeded(ProviderError):
    """The local daily call budget for this provider is used up."""


class ProviderTimeout(ProviderError):
    """The request exceeded its timeout."""


class Unavailable(ProviderError):
    """Any other failure: network error, unexpected status, malformed body."""


@dataclass
class ShoppingResult:
    """One product listing found by a search provider."""

    title: str
    link: str
    provider: str
    merchant: str | None = None
    price: str | None = None
    currency: str | None = None
    product_id: str | None = None
    page_token: str | None = None
    thumbnail: str | None = None
    condition: str | None = None
    snippet: str | None = None
    #: Page content returned by the search itself (Firecrawl); request-scoped only.
    page_text: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("page_text", None)
        return data


@dataclass
class Offer:
    """One seller's current price for a product."""

    merchant: str
    price: str | None
    currency: str | None
    link: str | None
    provider: str
    title: str | None = None
    condition: str | None = None
    in_stock: bool | None = None
    #: Shipping cost in the offer's currency when the provider states it.
    shipping: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProductRef:
    """Everything needed to look a product's offers up again later."""

    product_id: str | None = None
    page_token: str | None = None
    search_query: str | None = None
    title: str | None = None


@dataclass
class PageText:
    url: str
    text: str
    provider: str
    title: str | None = None


class ShoppingSearch(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 20) -> list[ShoppingResult]: ...


class OfferSource(Protocol):
    name: str

    def offers(self, ref: ProductRef) -> tuple[list[Offer], ProductRef]: ...


class PageExtractor(Protocol):
    """Reads a product page. Returns page text (for the price agent) or a dict that
    already matches :func:`schemas.validate_price_extraction`."""

    name: str

    def extract(self, url: str) -> PageText | dict[str, Any]: ...


# --- HTTP plumbing ------------------------------------------------------------

#: ``(method, url, *, params, json, headers, timeout) -> (status, body_bytes)``
Sender = Callable[..., tuple[int, bytes]]


def default_sender(method: str, url: str, *, params=None, json=None, headers=None, timeout=20.0):
    response = requests.request(
        method, url, params=params, json=json, headers=headers, timeout=(timeout, timeout), stream=True
    )
    try:
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                break
        return response.status_code, bytes(body)
    finally:
        response.close()


def request_raw(
    provider: str,
    method: str,
    url: str,
    *,
    sender: Sender | None = None,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> bytes:
    """Send one request and return the 200 body, mapping failures to provider errors."""
    do_send = sender or default_sender
    try:
        status, body = do_send(method, url, params=params, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        raise ProviderTimeout(f"{provider} request timed out.") from None
    except requests.exceptions.RequestException as exc:
        raise Unavailable(f"{provider} request failed ({type(exc).__name__}).") from None
    if not isinstance(status, int) or not isinstance(body, (bytes, bytearray)):
        raise Unavailable(f"{provider} returned a malformed response.")
    if len(body) > MAX_RESPONSE_BYTES:
        raise Unavailable(f"{provider} response exceeds the allowed size.")
    if status in (401, 403):
        raise AuthError(f"{provider} rejected the credentials (HTTP {status}).")
    if status in (402, 429):
        raise RateLimited(f"{provider} quota exhausted or throttled (HTTP {status}).")
    if status != 200:
        raise Unavailable(f"{provider} request failed (HTTP {status}).")
    return bytes(body)


def request_json(provider: str, method: str, url: str, **kwargs: Any) -> Any:
    """Like :func:`request_raw`, returning parsed JSON. Error bodies stay unread."""
    do_send = kwargs.pop("sender", None) or default_sender
    captured: dict[str, Any] = {}

    def keep_body(*args: Any, **kw: Any) -> tuple[int, bytes]:
        status, body = do_send(*args, **kw)
        captured["status"], captured["body"] = status, body
        return status, body

    try:
        body = request_raw(provider, method, url, sender=keep_body, **kwargs)
    except Unavailable:
        # SerpApi reports some outcomes (e.g. no results) as JSON errors on non-200.
        if captured.get("status") in (400, 404) and isinstance(captured.get("body"), (bytes, bytearray)):
            try:
                data = json.loads(bytes(captured["body"]).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                data = None
            if isinstance(data, dict) and data.get("error"):
                return data
        raise
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Unavailable(f"{provider} returned a response that is not valid JSON.") from None


# --- Shared parsing helpers ---------------------------------------------------

_CURRENCY_PREFIXES = (
    ("NZ$", "NZD"), ("NZD", "NZD"), ("A$", "AUD"), ("AU$", "AUD"), ("AUD", "AUD"),
    ("US$", "USD"), ("USD", "USD"),
)
_AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")


def parse_price(raw: Any, extracted: Any = None, *, default_dollar: str | None = "NZD") -> tuple[str | None, str | None]:
    """Turn a provider price into ``(amount, currency)``.

    ``extracted`` is a provider-parsed number (SerpApi ``extracted_price``) and wins
    for the amount. The currency is only inferred from an explicit marker; a bare
    ``$`` maps to ``default_dollar`` (NZD for a New Zealand search). Anything else
    yields ``None`` so the price is shown but never compared.
    """
    text = str(raw or "").strip()
    currency: str | None = None
    upper = text.upper()
    for prefix, code in _CURRENCY_PREFIXES:
        if prefix in upper:
            currency = code
            break
    else:
        if "$" in text and not re.search(r"[A-Za-z]\$|\$[A-Za-z]", text):
            currency = default_dollar

    amount: Decimal | None = None
    if isinstance(extracted, (int, float)) and not isinstance(extracted, bool):
        try:
            amount = Decimal(str(extracted))
        except InvalidOperation:
            amount = None
    if amount is None:
        match = _AMOUNT_RE.search(text)
        if match:
            token = match.group(0)
            if "." not in token and re.search(r",\d{2}$", token):
                token = token[:-3].replace(",", "") + "." + token[-2:]  # decimal comma: 120,00
            try:
                amount = Decimal(token.replace(",", ""))
            except InvalidOperation:
                amount = None
    if amount is None or not amount.is_finite() or amount <= 0:
        return None, currency
    return str(amount.quantize(Decimal("0.01"))), currency


_SECOND_HAND = re.compile(r"\b(used|refurb\w*|renewed|pre[- ]?owned|second[- ]?hand|open[- ]?box|ex[- ]?demo)\b", re.I)


def detect_condition(*texts: Any) -> str | None:
    """``"used"``/``"refurbished"`` when any text marks the item as not new."""
    for text in texts:
        match = _SECOND_HAND.search(str(text or ""))
        if match:
            word = match.group(1).lower()
            return "refurbished" if word.startswith(("refurb", "renewed")) else "used"
    return None


_PRICE_HINT = re.compile(r"(NZ\$|US\$|A\$|NZD|AUD|USD|\$)\s?\d[\d,]*(\.\d{1,2})?", re.I)


def focus_text(text: str, limit: int, *, head: int = 1500, window: int = 700) -> str:
    """Fit a long page into ``limit`` characters without losing its prices.

    Product pages often put navigation before the price (a PB Tech page's first
    price sits ~40k characters in), so a plain cut would drop it. Keep the head
    (title) and windows around price-like text, in page order.
    """
    if len(text) <= limit:
        return text
    spans = [(0, min(head, len(text)))]
    for match in _PRICE_HINT.finditer(text, head):
        spans.append((max(0, match.start() - window), min(len(text), match.end() + window)))
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts, used = [], 0
    for start, end in merged:
        room = limit - used
        if room <= 20:
            break
        chunk = text[start:min(end, start + room)]
        parts.append(chunk)
        used += len(chunk) + 5
    return "\n…\n".join(parts)


def clean_text(value: Any, limit: int = 300) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] if text else None


def is_http_url(value: Any) -> bool:
    text = str(value or "").lower()
    return text.startswith("https://") or text.startswith("http://")

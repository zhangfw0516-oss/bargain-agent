"""Web scraping for product prices.

Fetches a product page, validates the URL and every redirect hop, and extracts
the current price from structured JSON-LD data. Falls back to OpenGraph meta tags
only when the page carries no JSON-LD product information at all — an ambiguous
JSON-LD product identity blocks the fetch instead of being papered over.

Security notes:
- Automatic redirects are disabled; the scraper follows Location headers manually
  so each hop can be validated before a request is sent.
- Both the URL hostname and its resolved IP addresses are checked against private
  network ranges. This is best-effort: DNS can change between the check and the
  actual connection. Only the validated hostname is requested, never a bare IP.
- A monotonic-clock deadline bounds the entire fetch, including DNS admission,
  DNS resolution and slow-stream reads. The OS resolver call cannot be cancelled,
  so DNS runs through a fixed-size pool where a still-running lookup keeps its slot
  instead of spawning a replacement thread. That makes the number of live resolver
  threads a hard bound; when the pool is saturated, further lookups fail inside their
  deadline rather than accumulating. A stuck lookup therefore consumes capacity for
  as long as the OS resolver takes, which is a deliberate trade-off against unbounded
  thread growth.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)


class ScraperError(RuntimeError):
    """Raised when a page cannot be fetched or parsed safely."""


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

MAX_REDIRECT_HOPS = 5

# Product selection outcomes.
SELECTION_NONE = "none"
SELECTION_UNIQUE = "unique"
SELECTION_AMBIGUOUS = "ambiguous"

# Condition values M40 cannot verify against a scraped page.
UNSUPPORTED_CONDITIONS = ("new", "used", "refurbished")


def _hostname(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _normalise_domain(hostname: str | None) -> str | None:
    if not hostname:
        return None
    host = hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_allowed_domain(hostname: str | None, allowed: list[str]) -> bool:
    if not hostname:
        return False
    host = _normalise_domain(hostname)
    return any(host == allowed_domain or host.endswith("." + allowed_domain) for allowed_domain in allowed)


def _is_global_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # not an IP, checked elsewhere
    return address.is_global


class BoundedDnsResolver:
    """Fixed-size DNS resolution pool.

    `socket.getaddrinfo` cannot be cancelled, so a slot is released by the worker
    that is still running, not by the caller that gave up waiting. Consequences:

    - the number of live resolver threads never exceeds `max_concurrent`;
    - a stuck lookup consumes capacity instead of being replaced, so repeated
      timeouts cannot accumulate threads;
    - once the pool is saturated, further lookups fail within their remaining
      deadline instead of queueing without bound.
    """

    def __init__(self, max_concurrent: int = 4) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self._semaphore = threading.Semaphore(self.max_concurrent)
        self._state_lock = threading.Lock()
        self._active = 0

    def active_count(self) -> int:
        """Number of resolver workers still running (including stuck ones)."""
        with self._state_lock:
            return self._active

    def resolve(self, hostname: str, deadline: float) -> list[Any] | None:
        """Resolve `hostname`, or return None if it cannot be done in budget."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # Admission wait is part of the budget, so a saturated pool cannot make a
        # caller wait longer than its deadline.
        if not self._semaphore.acquire(timeout=remaining):
            logger.warning("DNS resolver pool is saturated; failing %s within the deadline", hostname)
            return None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._semaphore.release()
            return None

        outcome: dict[str, Any] = {}

        def _worker() -> None:
            try:
                outcome["infos"] = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            except Exception as exc:  # reported through `outcome`, never raised across threads
                outcome["error"] = exc
            finally:
                with self._state_lock:
                    self._active -= 1
                # Slot ownership stays with the worker until it truly finishes.
                self._semaphore.release()

        with self._state_lock:
            self._active += 1
        worker = threading.Thread(target=_worker, name=f"dns-{hostname}", daemon=True)
        worker.start()
        worker.join(timeout=remaining)
        if worker.is_alive():
            logger.warning("DNS resolution for %s exceeded its deadline", hostname)
            return None

        if "error" in outcome:
            logger.warning("Could not resolve %s: %s", hostname, outcome["error"])
            return None
        return outcome.get("infos", [])


_DNS_RESOLVER = BoundedDnsResolver(max_concurrent=4)


def _resolved_ips_are_global(hostname: str, deadline: float) -> bool:
    """Return True if every resolved IP for the hostname is global."""
    if not hostname:
        return False
    if not _is_global_ip(hostname):
        return False
    records = _DNS_RESOLVER.resolve(hostname, deadline)
    if not records:
        return False
    for info in records:
        ip = info[4][0]
        if not _is_global_ip(ip):
            logger.warning("Hostname %s resolves to non-global IP %s", hostname, ip)
            return False
    return True


def _is_safe_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http"):
        return False
    if not host or parsed.username or parsed.password:
        return False
    if "." not in host or host.endswith((".local", ".localhost", ".internal")):
        return False
    if parsed.port not in (None, 80, 443):
        return False
    return True


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _validate_url(url: str, allowed_domains: list[str], deadline: float | None = None) -> None:
    if not isinstance(url, str) or not url.strip():
        raise ScraperError("URL is empty.")
    if not _is_safe_url(url):
        raise ScraperError("URL is not a public HTTP/HTTPS address.")
    if not _is_allowed_domain(_hostname(url), allowed_domains):
        raise ScraperError(f"Domain not in allowed list: {allowed_domains}")
    effective_deadline = deadline if deadline is not None else time.monotonic() + config.scraper_timeout()
    if effective_deadline - time.monotonic() <= 0:
        raise ScraperError("Fetch deadline exceeded before DNS resolution.")
    if not _resolved_ips_are_global(_hostname(url) or "", effective_deadline):
        raise ScraperError("URL resolves to a non-public IP address.")


def _follow_redirects(
    session: requests.Session,
    start_url: str,
    allowed_domains: list[str],
    deadline: float,
    max_hops: int = MAX_REDIRECT_HOPS,
) -> requests.Response:
    """Manually follow redirects, validating each hop before sending a request."""
    url = start_url
    for _hop in range(max_hops + 1):
        remaining = _remaining(deadline)
        if remaining is None or remaining <= 0:
            raise ScraperError("Fetch deadline exceeded before redirect hop.")
        _validate_url(url, allowed_domains, deadline)
        remaining = _remaining(deadline) or 0.0
        if remaining <= 0:
            raise ScraperError("Fetch deadline exceeded before request.")
        response = session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=(min(config.scraper_timeout(), remaining), min(config.scraper_timeout(), remaining)),
            allow_redirects=False,
            stream=True,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ScraperError("Redirect response missing Location header.")
            url = urljoin(url, location)
            if not _is_safe_url(url):
                raise ScraperError("Redirect target is not a public HTTP/HTTPS address.")
            if not _is_allowed_domain(_hostname(url), allowed_domains):
                raise ScraperError("Redirect target is not in the allowed domain list.")
            continue
        if response.ok:
            return response
        response.close()
        response.raise_for_status()
        break
    raise ScraperError("Too many redirect hops.")


def _read_limited(response: requests.Response, max_bytes: int, deadline: float) -> bytes:
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        remaining = _remaining(deadline)
        if remaining is None or remaining <= 0:
            raise ScraperError("Fetch deadline exceeded while reading response.")
        if chunk:
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ScraperError("Response exceeds maximum allowed size.")
    return b"".join(chunks)


def _extract_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    products = []
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text(" ")
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            products.extend(data)
        else:
            products.append(data)
    return products


def _normalise_price(value: Any) -> Decimal:
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise ScraperError(f"Unexpected price type: {type(value)}")
    cleaned = value.replace(",", "").replace("$", "").replace(" ", "").strip()
    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        raise ScraperError(f"Unparseable price: {value!r}")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ScraperError(f"Invalid price: {value!r}") from exc


def _availability_in_stock(value: str | None) -> bool | None:
    if not value:
        return None
    low = value.lower()
    if "instock" in low or "in stock" in low or "https://schema.org/instock" in low:
        return True
    if "outofstock" in low or "out of stock" in low or "https://schema.org/outofstock" in low:
        return False
    if "preorder" in low:
        return False
    return None


# Offer fields that describe presentation rather than the offer itself.
_OFFER_IGNORED_FIELDS = frozenset(
    {"@context", "@id", "url", "pricevaliduntil", "potentialaction", "description"}
)


def _canonical(value: Any) -> Any:
    """Recursively normalise a JSON-LD value so comparison is order-insensitive.

    Text is trimmed and case-folded; dicts are sorted by key; lists keep their order
    because a reordering can be meaningful; numbers are compared as text so 319 and
    "319" do not silently disagree.
    """
    if isinstance(value, str):
        return value.strip().casefold()
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return tuple(_canonical(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((str(key).casefold(), _canonical(item)) for key, item in value.items()))
    return str(value)


def _offer_identity(offer: dict[str, Any]) -> tuple:
    """Fingerprint everything an offer says about itself.

    Comparing a hand-picked whitelist silently ignores every difference the list does
    not know about — a `size`, a nested `itemOffered` variant, or a field only one of
    the offers carries. Rather than enumerating "interesting" fields, every field
    except pure presentation metadata is folded in, so an unknown difference makes two
    offers non-equivalent instead of invisible. Offers are equivalent only when their
    fingerprints are identical.
    """
    return tuple(
        sorted(
            (str(key).casefold(), _canonical(value))
            for key, value in offer.items()
            if str(key).casefold() not in _OFFER_IGNORED_FIELDS
        )
    )


def _select_offer(product: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    """Select the offer for a chosen product.

    Returns (offer, ambiguous). A single offer is unambiguous. Several offers are
    accepted only when every identity-relevant field matches, i.e. they are
    provably duplicate descriptions of the same offer. Anything else — including a
    price/currency match with differing stock, condition, seller or SKU — is a
    guess, so the caller must refuse.
    """
    offers = product.get("offers")
    if not offers:
        return None, False
    if isinstance(offers, dict):
        return offers, False
    if not isinstance(offers, list) or not offers:
        return None, False

    usable = [o for o in offers if isinstance(o, dict) and "price" in o]
    if not usable:
        return None, False
    if len(usable) == 1:
        return usable[0], False

    identities = {_offer_identity(o) for o in usable}
    if len(identities) == 1:
        return usable[0], False
    return None, True


def _extract_variant(product: dict[str, Any]) -> str | None:
    """Best-effort variant label (capacity/colour/model) from JSON-LD."""
    for key in ("size", "model", "color", "colour"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    props = product.get("additionalProperty")
    if isinstance(props, list):
        wanted = {"storage", "capacity", "size", "colour", "color", "model"}
        for prop in props:
            if isinstance(prop, dict) and str(prop.get("name", "")).lower() in wanted:
                value = prop.get("value")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _structured_brand(product: dict[str, Any]) -> str | None:
    """The brand name from the selected product's own JSON-LD ``brand`` field.

    schema.org allows a string, a ``Brand`` object, or a list of either. Only the
    already-selected product is read — a page-level or OpenGraph brand is never
    consulted, so this cannot splice another product's identity onto this quote.
    """
    value = product.get("brand")
    if isinstance(value, dict):
        value = value.get("name")
    elif isinstance(value, list):
        names: list[Any] = []
        for item in value:
            names.append(item.get("name") if isinstance(item, dict) else item)
        value = next((n for n in names if n), None)
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())
    return text or None


def _meta_content(soup: BeautifulSoup, property_name: str) -> str | None:
    tag = soup.find("meta", property=property_name) or soup.find("meta", attrs={"name": property_name})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def _product_identity_score(product: dict[str, Any], url: str) -> int:
    """Score how likely a JSON-LD Product is the target of the requested URL."""
    score = 0
    url_lower = url.lower()
    sku = str(product.get("sku", "")).lower()
    mpn = str(product.get("mpn", "")).lower()
    name = str(product.get("name", "")).lower()
    if sku and sku in url_lower:
        score += 30
    if mpn and mpn in url_lower:
        score += 25
    if name:
        tokens = set(re.findall(r"[a-z0-9]+", name))
        url_tokens = set(re.findall(r"[a-z0-9]+", url_lower))
        overlap = tokens & url_tokens
        if overlap:
            score += min(20, len(overlap) * 4)
    at_id = str(product.get("@id", "")).lower()
    if at_id and (url_lower in at_id or at_id in url_lower):
        score += 50
    return score


def _select_product(products: list[dict[str, Any]], url: str) -> tuple[str, dict[str, Any] | None]:
    """Choose the JSON-LD Product that matches the requested URL.

    Returns one of:
      (SELECTION_NONE, None)      — the page carries no JSON-LD Product
      (SELECTION_UNIQUE, product) — exactly one product can be identified
      (SELECTION_AMBIGUOUS, None) — several products match equally, or none can
                                    be identified among several candidates

    "Ambiguous" must never be downgraded to a fallback: guessing a product is how
    a cheap accessory's price ends up attached to a different laptop's name.
    """
    candidates = [p for p in products if isinstance(p, dict) and p.get("@type") == "Product"]
    if not candidates:
        return SELECTION_NONE, None
    if len(candidates) == 1:
        return SELECTION_UNIQUE, candidates[0]

    scored = [(p, _product_identity_score(p, url)) for p in candidates]
    best = max(score for _, score in scored)
    if best <= 0:
        # Several products and no identity signal at all: cannot choose.
        return SELECTION_AMBIGUOUS, None
    top = [p for p, score in scored if score == best]
    if len(top) > 1:
        # A tie is still ambiguous; selection order must not decide.
        return SELECTION_AMBIGUOUS, None
    return SELECTION_UNIQUE, top[0]


def _parse_page(url: str, html: bytes, final_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    products = _extract_jsonld(soup)
    kind, product = _select_product(products, final_url)

    if kind == SELECTION_AMBIGUOUS:
        raise ScraperError(
            "Ambiguous product identity: several JSON-LD products match this page equally; "
            "refusing to guess a price."
        )

    if kind == SELECTION_UNIQUE and product is not None:
        return _quote_from_jsonld(product, final_url)
    return _quote_from_opengraph(soup, final_url)


def _quote_from_jsonld(product: dict[str, Any], final_url: str) -> dict[str, Any]:
    """Build a quote exclusively from one selected JSON-LD product.

    OpenGraph metadata is deliberately not consulted: if the chosen product lacks
    a required field, nothing on the page proves that a value found elsewhere
    belongs to *this* product, so the fetch fails instead of emitting a spliced
    quote. Unknown stock stays unknown.
    """
    from schemas import validate_quote

    product_name = str(product.get("name") or "").strip() or None
    variant = _extract_variant(product)

    offer, offer_ambiguous = _select_offer(product)
    if offer_ambiguous:
        raise ScraperError(
            "Ambiguous offers: this product lists several offers that are not provably "
            "equivalent; refusing to pick one."
        )

    price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None
    if offer is not None:
        if offer.get("price") is not None:
            price = _normalise_price(offer["price"])
        if offer.get("priceCurrency"):
            currency = str(offer["priceCurrency"]).upper()
        in_stock = _availability_in_stock(offer.get("availability"))

    missing = [
        label
        for label, value in (("product name", product_name), ("price", price), ("currency", currency))
        if value is None
    ]
    if missing:
        raise ScraperError(
            "Selected JSON-LD product is missing " + ", ".join(missing)
            + "; refusing to fill it in from unrelated page metadata."
        )

    return validate_quote({
        "product_name": product_name,
        "brand": _structured_brand(product),
        "variant": variant,
        "url": final_url,
        "retailer": _hostname(final_url),
        "price": str(price.quantize(Decimal("0.01"))),
        "currency": currency.upper(),
        "in_stock": in_stock,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "shipping_cost": None,
        "error": None,
    })


def _quote_from_opengraph(soup: BeautifulSoup, final_url: str) -> dict[str, Any]:
    """Self-contained fallback for pages with no JSON-LD product at all.

    Stock is reported as unknown: OpenGraph carries no availability signal, so a
    page whose stock cannot be verified must not become alert-eligible.
    """
    from schemas import validate_quote

    product_name = _meta_content(soup, "og:title") or (soup.title.string if soup.title else None)
    amount = _meta_content(soup, "og:price:amount") or _meta_content(soup, "price")
    price = _normalise_price(amount) if amount else None
    currency = _meta_content(soup, "og:price:currency")

    if price is None:
        raise ScraperError("Could not find a price on the page.")
    if currency is None:
        raise ScraperError("Could not determine currency.")
    if product_name is None or not product_name.strip():
        raise ScraperError("Could not determine the product identity on the page.")

    return validate_quote({
        "product_name": product_name.strip(),
        # OpenGraph carries no structured brand/variant, so none is claimed here.
        "brand": None,
        "variant": None,
        "url": final_url,
        "retailer": _hostname(final_url),
        "price": str(price.quantize(Decimal("0.01"))),
        "currency": currency.upper(),
        "in_stock": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "shipping_cost": None,
        "error": None,
    })


def extract_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Public helper: every JSON-LD dict found on a page.

    Retailer adapters reuse this instead of writing their own script scraping.
    """
    return _extract_jsonld(soup)


def _host_allowlist_for_web(url: str) -> list[str]:
    """The allow-list for a web-search candidate: only its own public host.

    The candidate's host (www-stripped) is the entire allow-list, so a redirect to
    any *other* host fails the per-hop domain check — the host boundary is kept on
    every hop instead of "any configured shop".
    """
    host = _hostname(url)
    if not host:
        raise ScraperError("URL has no host.")
    return [_normalise_domain(host)]


def fetch_html(url: str, allowed_domains: list[str] | None = None) -> tuple[str, bytes]:
    """Fetch a page safely and return ``(final_url, body)``.

    Applies exactly the same rules as `fetch_quote`: the domain allow-list, per-hop
    redirect validation, resolved-IP checks, the response size cap and a monotonic
    deadline covering DNS admission and the download. Retailer adapters use this so
    they never re-implement — or accidentally weaken — the fetch safety rules.

    ``allowed_domains`` narrows (never widens) the boundary. ``None`` keeps the
    configured allow-list; the web-search path passes the candidate's own host so a
    page found on the open web can only be read from that one host.
    """
    allowed = allowed_domains if allowed_domains is not None else config.allowed_scraper_domains()
    # Monotonic clock: wall-clock jumps must not extend or shorten the budget,
    # and the deadline covers DNS resolution as well as the download.
    deadline = time.monotonic() + config.scraper_timeout()
    _validate_url(url, allowed, deadline)

    session = requests.Session()
    try:
        response = _follow_redirects(session, url, allowed, deadline)
        try:
            return response.url, _read_limited(response, config.scraper_max_response_bytes(), deadline)
        finally:
            response.close()
    except requests.exceptions.Timeout as exc:
        raise ScraperError("Request timed out.") from exc
    except requests.exceptions.RequestException as exc:
        raise ScraperError(f"Request failed: {exc}") from exc
    finally:
        session.close()


def fetch_quote(url: str, *, allowed_domains: list[str] | None = None) -> dict[str, Any]:
    """Fetch and parse a product page into a normalised quote dict.

    ``allowed_domains`` narrows the fetch boundary (see :func:`fetch_html`).
    """
    final_url, html = fetch_html(url, allowed_domains)
    quote = _parse_page(url, html, final_url)
    logger.info("Fetched quote for %s: %s %s (in_stock=%s)", url, quote["currency"], quote["price"], quote["in_stock"])
    return quote


def fetch_quote_on_host(url: str) -> dict[str, Any]:
    """Fetch a product page **found by web search**, restricted to its own host.

    Difference from the configured-shop path:

    - the URL must be public **HTTPS** (web candidates come from the open web, so
      the transport is never downgraded);
    - the only allowed domain is the candidate's own host — a redirect that leaves
      that host fails instead of silently following;
    - every other rule (public-IP admission, per-hop validation, size cap, deadline)
      is the shared implementation, not a weaker copy.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ScraperError("Web-search product pages must be fetched over HTTPS.")
    if not _hostname(url):
        raise ScraperError("URL has no host.")
    return fetch_quote(url, allowed_domains=_host_allowlist_for_web(url))


get_product_price = fetch_quote


#: The LLM price-extraction fallback sends only this many characters of visible
#: page text. Page text is untrusted data passed to the model, never interpreted
#: locally beyond this length bound.
MAX_PAGE_TEXT_CHARS = 8000


def _page_text(html: bytes) -> str:
    """Visible text of a page, scripts/styles removed and length-bounded."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe", "form"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()[:MAX_PAGE_TEXT_CHARS]


def _quote_from_llm(html: bytes, final_url: str, *, client=None, model=None) -> dict[str, Any]:
    """Build a validated quote from the LLM's reading of the page text."""
    from agent import extract_price_from_page_text
    from schemas import validate_quote

    extracted = extract_price_from_page_text(_page_text(html), client=client, model=model)
    # The LLM may state identity attributes (brand / colour / capacity) from the
    # page text. They travel on the quote so `discovery._detail_identity` can confirm
    # the candidate as the same product when the page's name omits the spec; they are
    # never used to turn a `different` verdict into `same`.
    colour = extracted.get("colour")
    capacity = extracted.get("capacity")
    variant = " ".join(v for v in (colour, capacity) if v).strip() or None
    return validate_quote({
        "product_name": extracted["product_name"],
        "brand": extracted.get("brand"),
        "variant": variant,
        "url": final_url,
        "retailer": _hostname(final_url),
        "price": extracted["price"],
        "currency": extracted["currency"],
        "in_stock": extracted["in_stock"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "shipping_cost": None,
        "error": None,
    })


def fetch_quote_on_host_llm(url: str, *, client=None, model=None) -> dict[str, Any]:
    """Fetch a web-search product page and price it, with an LLM fallback.

    Structured JSON-LD/OpenGraph is tried first. Only when the page was fetched
    successfully but carries no machine-readable price is the LLM asked to read the
    visible text; fetch-level failures (403, redirect, private IP, oversized page)
    already raised by ``fetch_html`` and are never retried with the model.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ScraperError("Web-search product pages must be fetched over HTTPS.")
    if not _hostname(url):
        raise ScraperError("URL has no host.")
    allowed = _host_allowlist_for_web(url)
    final_url, html = fetch_html(url, allowed_domains=allowed)
    try:
        return _parse_page(url, html, final_url)
    except ScraperError as structured_exc:
        logger.info("Structured price extraction failed for %s; trying LLM: %s", url, structured_exc)
        try:
            return _quote_from_llm(html, final_url, client=client, model=model)
        except Exception:
            logger.warning("LLM price extraction failed for %s", url, exc_info=True)
            raise structured_exc

"""Brave Web Search API client (``GET /res/v1/web/search``).

Scope per the architecture task (``docs/architecture/BROAD_PRODUCT_DISCOVERY.md``):

- One page, at most 10 results, ``country=NZ``, ``safesearch=moderate``; no paging.
- The endpoint, timeout and response-size cap are fixed; the caller cannot widen them.
- Only the product name / specs from the structured search request reach this module.
  User-constructed search operators (``site:``, ``inurl:``, ``filetype:``, …) are
  rejected before a request is built.
- HTTP 401/403, 429, timeouts and other failures are classified as distinct error
  types so the caller can report "why web search is unavailable" honestly.
- The API key travels only in the ``X-Subscription-Token`` header — never in the
  URL, never in a log line, never inside an exception message.
- Results are candidate URLs/titles + the query time. The snippet is returned for
  this request only; nothing here persists the response. Brave reserves rights over
  stored search results, so callers must not persist raw responses or snippets
  beyond this request's lifetime.

When ``BRAVE_SEARCH_API_KEY`` is missing the provider reports "not configured"
(:class:`BraveNotConfigured`); the built-in retailer path keeps working unchanged.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
COUNTRY = "NZ"
SAFESEARCH = "moderate"
MAX_COUNT = 10
#: Short timeout: discovery must not hang on a slow search provider.
DEFAULT_TIMEOUT_SECONDS = 10.0
#: Response size cap for the JSON search payload.
MAX_RESPONSE_BYTES = 2_000_000

#: Search operators that must never pass through: the query comes from the
#: structured request's product name/specs, not from free-form user operators.
_OPERATOR_MARKERS = (
    "site:", "inurl:", "intitle:", "intext:", "inanchor:",
    "filetype:", "ext:", "related:", "cache:", "link:", "before:", "after:",
)


class BraveSearchError(RuntimeError):
    """Base class: web search could not be completed."""


class BraveNotConfigured(BraveSearchError):
    """No API key is configured; web search stays off."""


class BraveAuthError(BraveSearchError):
    """The API rejected the credentials (HTTP 401/403)."""


class BraveRateLimited(BraveSearchError):
    """The API quota is exhausted or throttled (HTTP 429)."""


class BraveTimeout(BraveSearchError):
    """The search request exceeded its timeout."""


class BraveUnavailable(BraveSearchError):
    """Any other failure (unexpected status, network error, oversized body)."""


def api_key() -> str:
    """The configured key, or ``""``. Never logged, never echoed in errors."""
    return os.getenv("BRAVE_SEARCH_API_KEY", "").strip()


def is_configured() -> bool:
    return bool(api_key())


def _timeout_seconds() -> float:
    raw = os.getenv("BRAVE_SEARCH_TIMEOUT", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if 0 < value <= 60 else DEFAULT_TIMEOUT_SECONDS


def sanitize_query(query: str) -> str:
    """Reject search operators; keep only the product-name text.

    Raises ``ValueError`` for operator-looking input so an operator never leaks
    into the request URL. Quote characters are stripped (they are operators too).
    """
    text = " ".join(str(query or "").replace('"', "'").split())
    if not text:
        raise ValueError("Search query is empty.")
    lowered = text.lower()
    for marker in _OPERATOR_MARKERS:
        if marker in lowered:
            raise ValueError(
                "The search query contains a search operator, which is not allowed."
            )
    return text


def search(
    query: str,
    count: int = MAX_COUNT,
    *,
    sender: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> list[dict[str, Any]]:
    """Return web-search candidates for the structured request's product text.

    Each candidate is ``{"url", "title", "source", "snippet", "queried_at"}``.
    ``source`` is always ``"web_search"``. The snippet is context for this request
    only — never persisted, never a price.

    ``sender`` is injectable so the whole client is testable offline; the default
    performs the real HTTPS request.
    """
    clean = sanitize_query(query)
    key = api_key()
    if not key:
        raise BraveNotConfigured(
            "Web search is not configured (missing BRAVE_SEARCH_API_KEY); "
            "only the built-in shops were searched."
        )
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = MAX_COUNT
    n = max(1, min(n, MAX_COUNT))

    url = f"{ENDPOINT}?q={quote_plus(clean)}&count={n}&country={COUNTRY}&safesearch={SAFESEARCH}"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": key,
    }
    timeout = _timeout_seconds()
    do_send = sender if sender is not None else _default_sender

    status, body = _send(do_send, url, headers, timeout)
    if status in (401, 403):
        raise BraveAuthError(
            f"Web search rejected the credentials (HTTP {status}); check the "
            "BRAVE_SEARCH_API_KEY configuration and its usage rights."
        )
    if status == 429:
        raise BraveRateLimited("Web search quota is exhausted or throttled (HTTP 429).")
    if status != 200:
        raise BraveUnavailable(f"Web search failed (HTTP {status}).")

    queried_at = (now or (lambda: datetime.now(timezone.utc)))().isoformat()
    return _parse_results(body, queried_at)


def _send(
    do_send: Callable[[str, dict[str, str], float], tuple[int, bytes]],
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    try:
        status, body = do_send(url, headers, timeout)
    except requests.exceptions.Timeout as exc:
        raise BraveTimeout("Web search request timed out.") from exc
    except requests.exceptions.RequestException as exc:
        # The exception text may embed the request URL (query text only — the key
        # is in a header), so it is safe to keep the type name but not the body.
        raise BraveUnavailable(f"Web search request failed: {type(exc).__name__}.") from exc
    if not isinstance(status, int):
        raise BraveUnavailable("Web search returned a malformed status.")
    if not isinstance(body, (bytes, bytearray)):
        raise BraveUnavailable("Web search returned a malformed body.")
    if len(body) > MAX_RESPONSE_BYTES:
        raise BraveUnavailable("Web search response exceeds the allowed size.")
    return status, bytes(body)


def _default_sender(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    response = requests.get(url, headers=headers, timeout=(timeout, timeout), stream=True)
    try:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    break
        return response.status_code, b"".join(chunks)
    finally:
        response.close()


def _parse_results(body: bytes, queried_at: str) -> list[dict[str, Any]]:
    import json

    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BraveUnavailable("Web search returned a response that is not valid JSON.") from None
    if not isinstance(data, dict):
        raise BraveUnavailable("Web search response has an unexpected shape.")
    web = data.get("web")
    results = web.get("results") if isinstance(web, dict) else None
    if not isinstance(results, list):
        return []

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = " ".join(str(item.get("title") or "").split())
        if not url or not title:
            continue
        lowered = url.lower()
        if not (lowered.startswith("https://") or lowered.startswith("http://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        snippet = " ".join(str(item.get("description") or "").split())
        candidates.append(
            {
                "url": url,
                "title": title,
                "source": "web_search",
                # Request-scoped context only: never persisted, never a price.
                "snippet": snippet,
                "queried_at": queried_at,
            }
        )
    logger.info("Brave web search returned %s candidate(s)", len(candidates))
    return candidates

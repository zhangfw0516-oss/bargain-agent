"""Tavily Search API client (``POST /search``).

A pluggable search backend for broad product discovery, alongside Brave. Tavily
is an AI-oriented web search: one call returns candidate product URLs/titles plus
request-scoped content. The same safety contract as the Brave provider applies:

- The API key travels only in the ``Authorization`` header - never in the URL,
  never in a log line, never inside an exception message.
- Only the product name / specs from the structured request reach this module.
  Free-form user search operators (``site:``, ``inurl:``, ...) are rejected before
  a request is built.
- Timeout and response-size cap are fixed; the caller cannot widen them.
- Results are candidate URLs/titles for this request only. The ``content`` snippet
  is context for this request only - never persisted, never a price, currency or
  stock claim. Prices always come from a fetched detail page downstream.

When ``TAVILY_API_KEY`` is missing the provider reports "not configured"
(:class:`TavilyNotConfigured`); the other search backends keep working unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.tavily.com/search"
MAX_RESULTS = 10
#: Short timeout: discovery must not hang on a slow search provider.
DEFAULT_TIMEOUT_SECONDS = 10.0
#: Response size cap for the JSON search payload.
MAX_RESPONSE_BYTES = 2_000_000

_OPERATOR_MARKERS = (
    "site:", "inurl:", "intitle:", "intext:", "inanchor:",
    "filetype:", "ext:", "related:", "cache:", "link:", "before:", "after:",
)


class TavilySearchError(RuntimeError):
    """Base class: the Tavily search could not be completed."""


class TavilyNotConfigured(TavilySearchError):
    """No API key is configured; this backend stays off."""


class TavilyAuthError(TavilySearchError):
    """The API rejected the credentials (HTTP 401/403)."""


class TavilyRateLimited(TavilySearchError):
    """The API quota is exhausted or throttled (HTTP 429)."""


class TavilyTimeout(TavilySearchError):
    """The search request exceeded its timeout."""


class TavilyUnavailable(TavilySearchError):
    """Any other failure (unexpected status, network error, oversized body)."""


def api_key() -> str:
    """The configured key, or ``""``. Never logged, never echoed in errors."""
    return os.getenv("TAVILY_API_KEY", "").strip()


def is_configured() -> bool:
    return bool(api_key())


def _timeout_seconds() -> float:
    raw = os.getenv("TAVILY_SEARCH_TIMEOUT", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if 0 < value <= 60 else DEFAULT_TIMEOUT_SECONDS


def sanitize_query(query: str) -> str:
    """Reject search operators; keep only the product-name text."""
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
    count: int = MAX_RESULTS,
    *,
    sender: Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, bytes]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> list[dict[str, Any]]:
    """Return web-search candidates for the structured request's product text.

    Each candidate is ``{"url", "title", "source", "snippet", "queried_at"}`` -
    the same shape as the Brave provider so the caller can merge backends.
    ``sender`` is injectable so the whole client is testable offline.
    """
    clean = sanitize_query(query)
    key = api_key()
    if not key:
        raise TavilyNotConfigured(
            "Tavily search is not configured (missing TAVILY_API_KEY); "
            "this backend was skipped."
        )
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = MAX_RESULTS
    n = max(1, min(n, MAX_RESULTS))

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    payload = {
        "query": clean,
        "max_results": n,
        "search_depth": "basic",
        "include_answer": False,
    }
    timeout = _timeout_seconds()
    do_send = sender if sender is not None else _default_sender

    status, body = _send(do_send, ENDPOINT, headers, payload, timeout)
    if status in (401, 403):
        raise TavilyAuthError(
            f"Tavily search rejected the credentials (HTTP {status}); check the "
            "TAVILY_API_KEY configuration."
        )
    if status == 429:
        raise TavilyRateLimited("Tavily search quota is exhausted or throttled (HTTP 429).")
    if status != 200:
        raise TavilyUnavailable(f"Tavily search failed (HTTP {status}).")

    queried_at = (now or (lambda: datetime.now(timezone.utc)))().isoformat()
    return _parse_results(body, queried_at)


def _send(
    do_send: Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, bytes]],
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, bytes]:
    try:
        status, body = do_send(url, headers, payload, timeout)
    except requests.exceptions.Timeout as exc:
        raise TavilyTimeout("Tavily search request timed out.") from exc
    except requests.exceptions.RequestException as exc:
        raise TavilyUnavailable(f"Tavily search request failed: {type(exc).__name__}.") from exc
    if not isinstance(status, int):
        raise TavilyUnavailable("Tavily search returned a malformed status.")
    if not isinstance(body, (bytes, bytearray)):
        raise TavilyUnavailable("Tavily search returned a malformed body.")
    if len(body) > MAX_RESPONSE_BYTES:
        raise TavilyUnavailable("Tavily search response exceeds the allowed size.")
    return status, bytes(body)


def _default_sender(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> tuple[int, bytes]:
    response = requests.post(url, json=payload, headers=headers, timeout=(timeout, timeout))
    try:
        return response.status_code, response.content[:MAX_RESPONSE_BYTES]
    finally:
        response.close()


def _parse_results(body: bytes, queried_at: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise TavilyUnavailable("Tavily search returned a response that is not valid JSON.") from None
    if not isinstance(data, dict):
        raise TavilyUnavailable("Tavily search response has an unexpected shape.")
    results = data.get("results")
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
        snippet = " ".join(str(item.get("content") or "").split())
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
    logger.info("Tavily web search returned %s candidate(s)", len(candidates))
    return candidates

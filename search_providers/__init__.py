"""External search providers for broad product discovery.

Stage B-extension (BROAD_PRODUCT_DISCOVERY.md): a web-search provider supplies
candidate product URLs only. It is never a price source - the snippet or any
search-page text must not become a price, a currency or a stock claim.

Multiple pluggable backends live here (Brave, Tavily); ``discovery`` merges the
configured ones and de-duplicates by URL. Each backend is optional and fails
independently.
"""

from __future__ import annotations

from .brave import (
    BraveAuthError,
    BraveNotConfigured,
    BraveRateLimited,
    BraveSearchError,
    BraveTimeout,
    BraveUnavailable,
    is_configured,
    search,
)

__all__ = [
    "BraveAuthError",
    "BraveNotConfigured",
    "BraveRateLimited",
    "BraveSearchError",
    "BraveTimeout",
    "BraveUnavailable",
    "is_configured",
    "search",
]

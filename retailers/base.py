"""Retailer adapter interface for product discovery.

Each supported retailer exposes one small, uniform surface so the scheduler, the LLM
layer and the CLI never need to know how a particular shop formats its pages:

- ``search(query, limit)`` -> candidate products found on that retailer
- ``fetch_offer(url)``     -> a validated quote dict for a single product page

All retailer-specific parsing lives in these adapters and nowhere else. A candidate
carries only what the search page reliably provides; price, currency and stock come
from the detail fetch, which reuses the already-verified product-page parser.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


class RetailerError(RuntimeError):
    """A retailer could not be searched, or its result could not be parsed."""


@dataclass
class Candidate:
    """A product found by a retailer search, before its detail page is read.

    Fields are deliberately nullable: a search page often shows no price, and an
    unknown value must stay unknown rather than being guessed.
    """

    retailer: str
    url: str
    title: str
    sku: str | None = None
    model: str | None = None
    variant: str | None = None
    price: str | None = None
    currency: str | None = None
    in_stock: bool | None = None
    condition: str | None = None
    queried_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Retailer(ABC):
    """Adapter for one shop."""

    name: str = ""
    #: Hosts this adapter may talk to. Must also be in config.allowed_scraper_domains().
    domains: tuple[str, ...] = ()

    @abstractmethod
    def search(self, query: str, *, limit: int = 10) -> list[Candidate]:
        """Return candidate products for a free-text query.

        Raises `RetailerError` when the search page cannot be fetched or parsed.
        Returning an empty list means "the search worked and found nothing".
        """

    @abstractmethod
    def fetch_offer(self, url: str) -> dict[str, Any]:
        """Return a validated quote dict for one product URL.

        Raises `RetailerError` when the page cannot be turned into a usable quote.
        """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"


def normalise_space(text: str) -> str:
    return " ".join((text or "").split())


def title_from_slug(slug: str) -> str:
    """Turn a URL slug into a readable fallback title."""
    cleaned = normalise_space(slug.replace("-", " ").replace("_", " "))
    return cleaned.strip()


def absolute_url(base: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return base.rstrip("/") + "/" + href.lstrip("/")

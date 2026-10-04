"""Assemble the configured providers.

Adding a service means implementing a protocol from :mod:`sources.base` and
registering it here. Providers without an API key are left out and reported as
unavailable, so the app degrades instead of failing.

Shopping search: the primary provider (``SHOPPING_PROVIDER``, Serper by default when
configured) with the other one as automatic fallback. SerpApi, when configured, is
also the *detail* source (immersive product pages: stores, shipping, store links).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import config
from sources.base import Offer, OfferSource, PageExtractor, ProductRef, ProviderError, ShoppingResult, ShoppingSearch
from sources.firecrawl import Firecrawl
from sources.scrapedo import ScrapeDo
from sources.serpapi import SerpApi
from sources.serper import Serper
from sources.tavily import TavilyExtract

logger = logging.getLogger(__name__)

#: Called with a provider name before each billable call; raises QuotaExceeded.
QuotaGuard = Callable[[str], None]


class FallbackShopping:
    """Search/offers through the primary provider, the secondary one on failure."""

    def __init__(self, primary, secondary=None):
        self.primary, self.secondary = primary, secondary
        self.name = primary.name

    def search(self, query: str, *, limit: int = 20) -> list[ShoppingResult]:
        try:
            return self.primary.search(query, limit=limit)
        except ProviderError as exc:
            if self.secondary is None:
                raise
            logger.warning("%s search failed (%s); using %s", self.primary.name, exc, self.secondary.name)
            return self.secondary.search(query, limit=limit)

    def offers(self, ref: ProductRef) -> tuple[list[Offer], ProductRef]:
        try:
            return self.primary.offers(ref)
        except ProviderError as exc:
            if self.secondary is None:
                raise
            logger.warning("%s offers failed (%s); using %s", self.primary.name, exc, self.secondary.name)
            return self.secondary.offers(ref)


@dataclass
class Providers:
    shopping: ShoppingSearch | None = None
    supplement: ShoppingSearch | None = None
    offers: OfferSource | None = None
    #: SerpApi immersive pages (stores, shipping, store links), when configured.
    detail: OfferSource | None = None
    extractors: list[PageExtractor] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)

    def status(self) -> dict[str, str]:
        out = {}
        for role, provider in (("shopping", self.shopping), ("supplement", self.supplement),
                               ("offers", self.offers), ("detail", self.detail)):
            out[role] = provider.name if provider else "unavailable"
        out["extractors"] = ",".join(p.name for p in self.extractors) or "unavailable"
        return out


def build_providers(before_call: QuotaGuard | None = None) -> Providers:
    providers = Providers()

    serp = SerpApi(before_call=before_call)
    serper = Serper(before_call=before_call)
    if not serp.is_configured():
        providers.unavailable["serpapi"] = "missing SERPAPI_API_KEY"
    if not serper.is_configured():
        providers.unavailable["serper"] = "missing SERPER_API_KEY"
    configured = {p.name: p for p in (serper, serp) if p.is_configured()}
    preferred = config.shopping_provider()
    order = [preferred] + [n for n in ("serper", "serpapi") if n != preferred]
    chain = [configured[n] for n in order if n in configured]
    if chain:
        primary = FallbackShopping(chain[0], chain[1] if len(chain) > 1 else None)
        providers.shopping = primary
        providers.offers = primary
    if serp.is_configured():
        providers.detail = serp

    firecrawl = Firecrawl(before_call=before_call)
    if firecrawl.is_configured():
        providers.supplement = firecrawl
    else:
        providers.unavailable["firecrawl"] = "missing FIRECRAWL_API_KEY"

    candidates = {"tavily": TavilyExtract(before_call=before_call), "firecrawl": firecrawl,
                  "scrapedo": ScrapeDo(before_call=before_call)}
    for name in config.page_extractors():
        extractor = candidates[name]
        if extractor.is_configured():
            providers.extractors.append(extractor)
        elif name not in providers.unavailable:
            providers.unavailable[name] = f"missing {name.upper()}_API_KEY"
    return providers

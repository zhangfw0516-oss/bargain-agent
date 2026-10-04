"""Assemble the configured providers.

Adding a service means implementing a protocol from :mod:`sources.base` and
registering it here. Providers without an API key are left out and reported as
unavailable, so the app degrades instead of failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import config
from sources.base import OfferSource, PageExtractor, ShoppingSearch
from sources.firecrawl import Firecrawl
from sources.serpapi import SerpApi
from sources.tavily import TavilyExtract

#: Called with a provider name before each billable call; raises QuotaExceeded.
QuotaGuard = Callable[[str], None]


@dataclass
class Providers:
    shopping: ShoppingSearch | None = None
    supplement: ShoppingSearch | None = None
    offers: OfferSource | None = None
    extractors: list[PageExtractor] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)

    def status(self) -> dict[str, str]:
        out = {}
        for role, provider in (("shopping", self.shopping), ("supplement", self.supplement), ("offers", self.offers)):
            out[role] = provider.name if provider else "unavailable"
        out["extractors"] = ",".join(p.name for p in self.extractors) or "unavailable"
        return out


def build_providers(before_call: QuotaGuard | None = None) -> Providers:
    providers = Providers()

    serp = SerpApi(before_call=before_call)
    if serp.is_configured():
        providers.shopping = serp
        providers.offers = serp
    else:
        providers.unavailable["serpapi"] = "missing SERPAPI_API_KEY"

    firecrawl = Firecrawl(before_call=before_call)
    if firecrawl.is_configured():
        providers.supplement = firecrawl
    else:
        providers.unavailable["firecrawl"] = "missing FIRECRAWL_API_KEY"

    candidates = {"tavily": TavilyExtract(before_call=before_call), "firecrawl": firecrawl}
    for name in config.page_extractors():
        extractor = candidates[name]
        if extractor.is_configured():
            providers.extractors.append(extractor)
        elif name not in providers.unavailable:
            providers.unavailable[name] = f"missing {name.upper()}_API_KEY"
    return providers

"""Supported retailer adapters.

Keeping the registry here means the rest of the system asks for "the supported
retailers" instead of importing shop-specific modules directly.
"""

from __future__ import annotations

from .base import Candidate, Retailer, RetailerError, absolute_url, normalise_space, title_from_slug
from .jbhifi import JbHiFiRetailer
from .mightyape import MightyApeRetailer
from .pbtech import PbTechRetailer

#: Retailers the project intends to support in Stage B.
#:
#: Order matters only for reporting: shops are searched in this order. Mighty Ape stays
#: registered even though it cannot be reached from this environment (DataDome 403), so
#: the UI keeps reporting it as a source that was not searched instead of silently
#: pretending the shop does not exist.
DEFAULT_RETAILERS: tuple[Retailer, ...] = (PbTechRetailer(), MightyApeRetailer(), JbHiFiRetailer())


def all_retailers() -> tuple[Retailer, ...]:
    return DEFAULT_RETAILERS


def get_retailer(name: str) -> Retailer:
    for retailer in DEFAULT_RETAILERS:
        if retailer.name.lower() == str(name).lower():
            return retailer
    raise KeyError(f"Unknown retailer: {name}")


def domain_allowed(host: str | None, allowed_domains: list[str]) -> bool:
    if not host:
        return False
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


def enabled_retailers(allowed_domains: list[str] | None = None) -> tuple[Retailer, ...]:
    """Retailers whose domains are permitted by the scraper allow-list.

    A retailer outside the allow-list is skipped rather than bypassing the check, so
    the fixed "supported shops" boundary is always enforced by configuration.
    """
    if allowed_domains is None:
        import config

        allowed_domains = config.allowed_scraper_domains()
    return tuple(r for r in DEFAULT_RETAILERS if domain_allowed(r.domains[0], allowed_domains))


__all__ = [
    "Candidate",
    "Retailer",
    "RetailerError",
    "JbHiFiRetailer",
    "MightyApeRetailer",
    "PbTechRetailer",
    "DEFAULT_RETAILERS",
    "all_retailers",
    "enabled_retailers",
    "get_retailer",
    "domain_allowed",
    "absolute_url",
    "normalise_space",
    "title_from_slug",
]

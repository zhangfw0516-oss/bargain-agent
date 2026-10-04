"""Price observation helpers shared by discovery and monitoring.

- :func:`merchant_trust` decides whether a merchant is on the trusted list.
- :func:`is_safe_public_url` screens user-pasted URLs before any provider sees them.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

import config


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

"""Scrape.do provider: fetch one product page's HTML for the price agent.

``GET https://api.scrape.do/?token=...&url=...`` returns the target page. The HTML is
reduced to visible text plus any JSON-LD blocks (which often carry the structured
product price) and handed to :mod:`agents.price_extractor`. The token travels in the
query string, so errors are rebuilt without the URL and the token is never logged.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Callable

import config
from schemas import validate_price_extraction
from sources.base import NotConfigured, PageText, Sender, Unavailable, is_http_url, request_raw

ENDPOINT = "https://api.scrape.do/"
NAME = "scrapedo"
MAX_PAGE_CHARS = 60_000


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    _BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "section", "article", "td"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.jsonld: list[str] = []
        self._skip = 0
        self._in_jsonld = False
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
        elif tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_jsonld = False
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld.append(data.strip())
        elif self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(html: str) -> tuple[str, str]:
    """``(title, text)`` with JSON-LD product data first, then visible text."""
    title, text, _blocks = _parse(html)
    return title, text


def _parse(html: str) -> tuple[str, str, list[str]]:
    parser = _TextExtractor()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    visible = "\n".join(line for line in lines if line)
    structured = "\n".join(block for block in parser.jsonld if '"price' in block.lower() or "product" in block.lower())
    text = (f"Structured data:\n{structured[:8000]}\n\nPage text:\n" if structured else "") + visible
    return " ".join(parser.title.split()), text, parser.jsonld


def _walk(node: Any):
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemOffered"):
            if key in node:
                yield from _walk(node[key])


def _types(node: dict) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).lower() for v in values if v}


_AVAILABILITY = {"instock": True, "onlineonly": True, "limitedavailability": True, "instoreonly": None,
                 "outofstock": False, "soldout": False, "discontinued": False, "preorder": None, "backorder": None}


def jsonld_price(blocks: list[str]) -> dict[str, Any] | None:
    """The page's own structured price when exactly one product offer is stated.

    JSON-LD is what the merchant publishes for search engines, so it is preferred
    over reading visible text (which can render "$448.00" as "$ 448 00"). Several
    products or differing offers are ambiguous and return None.
    """
    found: set[tuple] = set()
    for block in blocks:
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        for node in _walk(data):
            if "product" not in _types(node):
                continue
            offers = node.get("offers")
            for offer in _walk(offers) if offers else []:
                if not ({"offer"} & _types(offer)) and "price" not in offer:
                    continue
                price, currency = offer.get("price"), offer.get("priceCurrency")
                if price in (None, "") or not currency:
                    continue
                availability = str(offer.get("availability") or "").rstrip("/").rsplit("/", 1)[-1].lower()
                found.add((str(node.get("name") or "").strip(), str(price), str(currency).upper(),
                           _AVAILABILITY.get(availability)))
    if len({(p, c) for _, p, c, _ in found}) != 1:
        return None
    name, price, currency, in_stock = sorted(found)[0]
    try:
        return validate_price_extraction({"product_name": name or "Product", "price": f"{Decimal(price):.2f}",
                                          "currency": currency, "in_stock": in_stock})
    except (ValueError, InvalidOperation):
        return None


class ScrapeDo:
    name = NAME

    def __init__(
        self,
        api_key: str | None = None,
        *,
        sender: Sender | None = None,
        timeout: float | None = None,
        before_call: Callable[[str], None] | None = None,
    ):
        self._api_key = api_key if api_key is not None else config.scrapedo_api_key()
        self._sender = sender
        self._timeout = timeout or config.scrapedo_timeout()
        self._before_call = before_call

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def extract(self, url: str) -> PageText | dict[str, Any]:
        if not self._api_key:
            raise NotConfigured("Scrape.do is not configured (missing SCRAPEDO_API_KEY).")
        if not is_http_url(url):
            raise ValueError("Only http(s) URLs can be extracted.")
        if self._before_call is not None:
            self._before_call(NAME)
        body = request_raw(
            "Scrape.do", "GET", ENDPOINT, sender=self._sender,
            params={"token": self._api_key, "url": url, "geoCode": "nz"}, timeout=self._timeout,
        )
        html = body.decode("utf-8", errors="replace")
        title, text, blocks = _parse(html)
        structured = jsonld_price(blocks)
        if structured is not None:
            return structured
        if not text.strip():
            raise Unavailable("Scrape.do returned a page without readable text.")
        return PageText(url=url, text=text[:MAX_PAGE_CHARS], provider=NAME, title=title or None)

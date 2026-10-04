"""Price-extractor agent: one product page's text -> a validated price.

The page text is untrusted; the prompt treats it as data. A page that states no
clear price yields :class:`PriceExtractionError` rather than a guess.
"""

from __future__ import annotations

from typing import Any

import llm
from agents import LLMOptions
from schemas import validate_price_extraction
from sources.base import focus_text, price_on_page

NAME = "price_extractor"
PROMPT = "extract_price_from_page.md"
MAX_PAGE_CHARS = 12_000


class PriceExtractionError(RuntimeError):
    """The page could not be turned into a validated price."""


def _validator(page: str):
    def validate(data: Any) -> dict | None:
        # A deliberate "no price on this page" answer is final, not a format error to retry.
        if isinstance(data, dict) and data.get("price") is None:
            return None
        result = validate_price_extraction(data)
        if not price_on_page(result["price"], page):
            raise ValueError(f"the price {result['price']} is not written on the page; copy the selling price as shown")
        return result

    return validate


def run(page_text: str, *, url: str | None = None, opts: LLMOptions | None = None) -> dict:
    """``url`` (when known) lets the agent read a bare "$" on a .nz site as NZD."""
    opts = opts or LLMOptions()
    text = str(page_text or "").strip()
    if not text:
        raise PriceExtractionError("Page text is empty.")
    page = focus_text(text, MAX_PAGE_CHARS)
    if url:
        page = f"Page URL: {url}\n{page}"
    try:
        result = llm.call_json(PROMPT, page, validate=_validator(page), agent=NAME, client=opts.client, model=opts.model)
    except llm.LLMError as exc:
        raise PriceExtractionError(str(exc)) from None
    if result is None:
        raise PriceExtractionError("The page does not state a clear current price.")
    return result

"""Price-extractor agent: one product page's text -> a validated price.

The page text is untrusted; the prompt treats it as data. A page that states no
clear price yields :class:`PriceExtractionError` rather than a guess.
"""

from __future__ import annotations

from typing import Any

import llm
from agents import LLMOptions
from schemas import validate_price_extraction
from sources.base import focus_text

NAME = "price_extractor"
PROMPT = "extract_price_from_page.md"
MAX_PAGE_CHARS = 12_000


class PriceExtractionError(RuntimeError):
    """The page could not be turned into a validated price."""


def _validate(data: Any) -> dict | None:
    # A deliberate "no price on this page" answer is final, not a format error to retry.
    if isinstance(data, dict) and data.get("price") is None:
        return None
    return validate_price_extraction(data)


def run(page_text: str, *, opts: LLMOptions | None = None) -> dict:
    opts = opts or LLMOptions()
    text = str(page_text or "").strip()
    if not text:
        raise PriceExtractionError("Page text is empty.")
    try:
        result = llm.call_json(
            PROMPT, focus_text(text, MAX_PAGE_CHARS), validate=_validate, agent=NAME, client=opts.client, model=opts.model
        )
    except llm.LLMError as exc:
        raise PriceExtractionError(str(exc)) from None
    if result is None:
        raise PriceExtractionError("The page does not state a clear current price.")
    return result

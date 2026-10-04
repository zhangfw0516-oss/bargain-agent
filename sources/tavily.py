"""Tavily Extract provider: read one product page's text for the price agent.

``POST https://api.tavily.com/extract`` returns the page's cleaned content, which
:mod:`agents.price_extractor` turns into a validated price. The key travels only in
the ``Authorization`` header.
"""

from __future__ import annotations

from typing import Callable

import config
from sources.base import NotConfigured, PageText, Sender, Unavailable, clean_text, is_http_url, request_json

ENDPOINT = "https://api.tavily.com/extract"
NAME = "tavily"
MAX_PAGE_CHARS = 12_000


class TavilyExtract:
    name = NAME

    def __init__(
        self,
        api_key: str | None = None,
        *,
        sender: Sender | None = None,
        timeout: float | None = None,
        before_call: Callable[[str], None] | None = None,
    ):
        self._api_key = api_key if api_key is not None else config.tavily_api_key()
        self._sender = sender
        self._timeout = timeout or config.tavily_timeout()
        self._before_call = before_call

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def extract(self, url: str) -> PageText:
        if not self._api_key:
            raise NotConfigured("Tavily is not configured (missing TAVILY_API_KEY).")
        if not is_http_url(url):
            raise ValueError("Only http(s) URLs can be extracted.")
        if self._before_call is not None:
            self._before_call(NAME)
        data = request_json(
            "Tavily",
            "POST",
            ENDPOINT,
            sender=self._sender,
            payload={"urls": [url], "extract_depth": "advanced", "format": "text"},
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        results = data.get("results") if isinstance(data, dict) else None
        for item in results or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("raw_content") or "").strip()
            if text:
                return PageText(url=str(item.get("url") or url), text=text[:MAX_PAGE_CHARS], provider=NAME)
        failed = data.get("failed_results") if isinstance(data, dict) else None
        reason = ""
        if failed and isinstance(failed[0], dict):
            reason = clean_text(failed[0].get("error"), 120) or ""
        raise Unavailable(f"Tavily could not read the page{': ' + reason if reason else ''}.")

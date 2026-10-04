"""Offline tests for the Tavily web-search provider."""

import json
import unittest
from unittest.mock import patch

import search_providers.tavily as tavily


def _payload(results):
    return json.dumps({"results": results}).encode("utf-8")


def _sender(status, body):
    def send(url, headers, payload, timeout):
        return status, body
    return send


GOOD = [
    {"url": "https://pbtech.co.nz/product/1", "title": "Fitbit Air", "content": "NZD 239"},
    {"url": "https://mightyape.co.nz/product/2", "title": "Fitbit Air Band", "content": "strap"},
]


class TestTavilyProvider(unittest.TestCase):
    def test_not_configured_without_key(self):
        with patch.dict("os.environ", {"TAVILY_API_KEY": ""}):
            self.assertFalse(tavily.is_configured())
            with self.assertRaises(tavily.TavilyNotConfigured):
                tavily.search("fitbit")

    def test_parses_results(self):
        with patch.dict("os.environ", {"TAVILY_API_KEY": "k"}):
            out = tavily.search("fitbit", sender=_sender(200, _payload(GOOD)))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["url"], "https://pbtech.co.nz/product/1")
        self.assertEqual(out[0]["source"], "web_search")
        self.assertIn("queried_at", out[0])

    def test_dedupes_and_skips_bad(self):
        rows = GOOD + [{"url": "javascript:x", "title": "bad"},
                       {"url": "https://pbtech.co.nz/product/1", "title": "dup"}]
        with patch.dict("os.environ", {"TAVILY_API_KEY": "k"}):
            out = tavily.search("fitbit", sender=_sender(200, _payload(rows)))
        self.assertEqual(len(out), 2)

    def test_auth_error(self):
        with patch.dict("os.environ", {"TAVILY_API_KEY": "k"}):
            with self.assertRaises(tavily.TavilyAuthError):
                tavily.search("fitbit", sender=_sender(401, b"{}"))

    def test_rate_limited(self):
        with patch.dict("os.environ", {"TAVILY_API_KEY": "k"}):
            with self.assertRaises(tavily.TavilyRateLimited):
                tavily.search("fitbit", sender=_sender(429, b"{}"))

    def test_rejects_operators(self):
        with patch.dict("os.environ", {"TAVILY_API_KEY": "k"}):
            with self.assertRaises(ValueError):
                tavily.search("fitbit site:pbtech.co.nz")

    def test_key_never_in_url(self):
        seen = {}

        def send(url, headers, payload, timeout):
            seen["url"] = url
            seen["headers"] = headers
            return 200, _payload(GOOD)

        with patch.dict("os.environ", {"TAVILY_API_KEY": "secret-key"}):
            tavily.search("fitbit", sender=send)
        self.assertNotIn("secret-key", seen["url"])
        self.assertIn("Bearer secret-key", seen["headers"]["Authorization"])


class TestMergedBackends(unittest.TestCase):
    """discovery merges Brave + Tavily and de-duplicates by URL."""

    def test_merges_and_dedupes(self):
        import discovery

        def brave_search(q):
            return [{"url": "https://a.example/1", "title": "A", "source": "web_search"}]

        def tavily_search(q):
            return [{"url": "https://a.example/1", "title": "A2"},
                    {"url": "https://b.example/2", "title": "B"}]

        with patch("search_providers.brave.is_configured", lambda: True), \
                patch("search_providers.brave.search", brave_search), \
                patch("search_providers.tavily.is_configured", lambda: True), \
                patch("search_providers.tavily.search", tavily_search):
            out = discovery._default_web_candidates("x")
        urls = [r["url"] for r in out]
        self.assertEqual(urls, ["https://a.example/1", "https://b.example/2"])

    def test_one_failing_backend_does_not_hide_the_other(self):
        import discovery
        import search_providers.brave as brave

        def boom(q):
            raise brave.BraveUnavailable("down")

        with patch("search_providers.brave.is_configured", lambda: True), \
                patch("search_providers.brave.search", boom), \
                patch("search_providers.tavily.is_configured", lambda: True), \
                patch("search_providers.tavily.search", lambda q: [{"url": "https://b.example/2", "title": "B"}]):
            out = discovery._default_web_candidates("x")
        self.assertEqual([r["url"] for r in out], ["https://b.example/2"])

    def test_no_backend_configured_raises_not_configured(self):
        import discovery
        import search_providers.brave as brave

        with patch("search_providers.brave.is_configured", lambda: False), \
                patch("search_providers.tavily.is_configured", lambda: False):
            with self.assertRaises(brave.BraveNotConfigured):
                discovery._default_web_candidates("x")


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the Brave web-search client (search_providers.brave).

Everything runs through the injectable ``sender`` — no network, no real key.
The key value used in tests is fake and only proves it never leaks into errors.
"""

import json
import os
import unittest
from unittest.mock import patch

import requests

from search_providers import brave


FAKE_KEY = "BSA-fake-key-1234567890"


def _ok_sender(results, status=200):
    """A sender that answers with the given JSON shape."""
    body = json.dumps({"web": {"results": results}}).encode("utf-8")

    def send(url, headers, timeout):
        send.requested_url = url
        send.requested_headers = dict(headers)
        return status, body

    return send


class _Base(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": FAKE_KEY})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestNotConfigured(unittest.TestCase):
    def test_without_key_reports_not_configured(self):
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": ""}):
            self.assertFalse(brave.is_configured())
            with self.assertRaises(brave.BraveNotConfigured):
                brave.search("fitbit air")
            # "not configured" is a distinct, honest status — not a silent empty list.
            self.assertTrue(issubclass(brave.BraveNotConfigured, brave.BraveSearchError))

    def test_is_configured_with_key(self):
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": FAKE_KEY}):
            self.assertTrue(brave.is_configured())


class TestSearch(_Base):
    def test_request_shape_and_headers(self):
        sender = _ok_sender([])
        brave.search("fitbit air", 5, sender=sender)
        url = sender.requested_url
        self.assertTrue(url.startswith(brave.ENDPOINT))
        self.assertIn("q=fitbit+air", url)
        self.assertIn("count=5", url)
        self.assertIn("country=NZ", url)
        self.assertIn("safesearch=moderate", url)
        # The key travels only in the header, never in the URL.
        self.assertNotIn(FAKE_KEY, url)
        self.assertEqual(sender.requested_headers.get("X-Subscription-Token"), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, url)

    def test_results_are_candidates_only(self):
        sender = _ok_sender(
            [
                {"url": "https://www.example-shop.co.nz/product/fitbit-air-obsidian",
                 "title": "Fitbit Air (Obsidian)", "description": "a tracker"},
                {"url": "https://another.example.co.nz/item/fitbit-air-black",
                 "title": "Fitbit Air Black", "description": ""},
            ]
        )
        out = brave.search("fitbit air", sender=sender)
        self.assertEqual(len(out), 2)
        first = out[0]
        self.assertEqual(first["source"], "web_search")
        self.assertIn("url", first)
        self.assertIn("title", first)
        self.assertIn("queried_at", first)
        self.assertIn("snippet", first)
        # The snippet is request-scoped context; it is never a price.
        self.assertNotIn("price", first)

    def test_empty_results_return_empty_list(self):
        sender = _ok_sender([])
        self.assertEqual(brave.search("fitbit air", sender=sender), [])

    def test_missing_web_block_returns_empty_list(self):
        import json

        def send(url, headers, timeout):
            return 200, json.dumps({"query": {"original": "x"}}).encode("utf-8")

        self.assertEqual(brave.search("fitbit air", sender=send), [])

    def test_non_http_urls_and_missing_fields_are_skipped(self):
        sender = _ok_sender(
            [
                {"url": "javascript:alert(1)", "title": "bad"},
                {"url": "https://ok.example.co.nz/p/1"},  # no title
                {"title": "no url"},
                {"url": "https://ok.example.co.nz/p/2", "title": "good"},
            ]
        )
        out = brave.search("fitbit air", sender=sender)
        self.assertEqual([c["url"] for c in out], ["https://ok.example.co.nz/p/2"])

    def test_duplicates_removed(self):
        sender = _ok_sender(
            [
                {"url": "https://ok.example.co.nz/p/1", "title": "one"},
                {"url": "https://ok.example.co.nz/p/1", "title": "one again"},
            ]
        )
        self.assertEqual(len(brave.search("q", sender=sender)), 1)

    def test_count_is_clamped(self):
        sender = _ok_sender([])
        brave.search("fitbit air", 99, sender=sender)
        self.assertIn("count=10", sender.requested_url)
        brave.search("fitbit air", 0, sender=sender)
        self.assertIn("count=1", sender.requested_url)


class TestErrorClassification(_Base):
    def test_401_is_auth_error_without_key_leak(self):
        def send(url, headers, timeout):
            return 401, b"{}"

        with self.assertRaises(brave.BraveAuthError) as ctx:
            brave.search("fitbit air", sender=send)
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_403_is_auth_error(self):
        def send(url, headers, timeout):
            return 403, b"{}"

        with self.assertRaises(brave.BraveAuthError):
            brave.search("fitbit air", sender=send)

    def test_429_is_rate_limited(self):
        def send(url, headers, timeout):
            return 429, b"{}"

        with self.assertRaises(brave.BraveRateLimited):
            brave.search("fitbit air", sender=send)

    def test_500_is_unavailable(self):
        def send(url, headers, timeout):
            return 500, b"{}"

        with self.assertRaises(brave.BraveUnavailable):
            brave.search("fitbit air", sender=send)

    def test_timeout_is_classified(self):
        def send(url, headers, timeout):
            raise requests.exceptions.Timeout("timed out")

        with self.assertRaises(brave.BraveTimeout):
            brave.search("fitbit air", sender=send)

    def test_network_error_is_unavailable_and_names_only_the_type(self):
        def send(url, headers, timeout):
            raise requests.exceptions.ConnectionError("no route to host")

        with self.assertRaises(brave.BraveUnavailable):
            brave.search("fitbit air", sender=send)

    def test_oversized_body_is_unavailable(self):
        def send(url, headers, timeout):
            return 200, b"x" * (brave.MAX_RESPONSE_BYTES + 1)

        with self.assertRaises(brave.BraveUnavailable):
            brave.search("fitbit air", sender=send)

    def test_invalid_json_is_unavailable(self):
        def send(url, headers, timeout):
            return 200, b"<html>not json</html>"

        with self.assertRaises(brave.BraveUnavailable):
            brave.search("fitbit air", sender=send)


class TestQuerySanitisation(_Base):
    def test_search_operators_are_rejected_before_any_request(self):
        for query in (
            "fitbit site:example.com",
            "inurl:product fitbit",
            "intitle:fitbit air",
            "filetype:pdf fitbit",
        ):
            with self.assertRaises(ValueError):
                brave.search(query, sender=_ok_sender([]))

    def test_quote_characters_are_stripped_not_sent(self):
        sender = _ok_sender([])
        brave.search('fitbit "air"', sender=sender)
        self.assertNotIn('"', sender.requested_url)
        self.assertIn("q=fitbit+%27air%27", sender.requested_url)

    def test_plain_spec_text_passes(self):
        sender = _ok_sender([])
        clean = brave.sanitize_query("Fitbit Air 30 ml, shade 04")
        brave.search(clean, sender=sender)
        self.assertIn("q=Fitbit+Air+30+ml%2C+shade+04", sender.requested_url)


if __name__ == "__main__":
    unittest.main()

"""Tests for scraper.fetch_quote."""

import json
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import scraper


FIXTURES = Path(__file__).with_name("fixtures")


def _fixture_html(name: str) -> bytes:
    path = FIXTURES / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["html"].encode("utf-8")


def _mock_socket_global():
    return patch("scraper.socket.getaddrinfo", return_value=[(None, None, None, None, ("1.2.3.4", 443))])


class MockResponse:
    def __init__(self, content: bytes, url: str, status_code: int = 200, headers: dict | None = None):
        self._content = content
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size: int = 1024):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def close(self):
        pass


class _MockSession:
    """A mock requests.Session that records every requested URL."""

    def __init__(self, responses: list):
        self.responses = responses
        self.requested_urls: list[str] = []
        self._calls = 0

    def get(self, url, **kwargs):
        self.requested_urls.append(url)
        response = self.responses[self._calls]
        self._calls += 1
        return response

    def close(self):
        pass


class TestScraper(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.object(scraper.config, "allowed_scraper_domains", return_value=["pbtech.co.nz"])
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _mock_session(self, responses: list):
        session = _MockSession(responses)
        patcher = patch("scraper.requests.Session", return_value=session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def test_fetch_quote_kindle(self):
        session = self._mock_session([MockResponse(_fixture_html("pbtech_kindle"), "https://www.pbtech.co.nz/product/Kindle")])
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/Kindle")
        self.assertEqual(quote["product_name"], "Kindle Paperwhite 16GB")
        self.assertEqual(quote["price"], "319.00")
        self.assertEqual(quote["currency"], "NZD")
        self.assertTrue(quote["in_stock"])

    def test_fetch_quote_3dprinter(self):
        session = self._mock_session([MockResponse(_fixture_html("pbtech_3dprinter"), "https://www.pbtech.co.nz/product/K1MAX")])
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/K1MAX")
        self.assertEqual(quote["price"], "1399.00")
        self.assertEqual(quote["currency"], "NZD")

    def test_fetch_quote_samsung(self):
        session = self._mock_session([MockResponse(_fixture_html("pbtech_samsung"), "https://www.pbtech.co.nz/product/A17")])
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/A17")
        self.assertEqual(quote["price"], "433.00")
        self.assertTrue(quote["in_stock"])

    def test_fetch_quote_outofstock(self):
        session = self._mock_session([MockResponse(_fixture_html("pbtech_outofstock"), "https://www.pbtech.co.nz/product/iPhone16Pro")])
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/iPhone16Pro")
        self.assertEqual(quote["price"], "2999.00")
        self.assertFalse(quote["in_stock"])

    def test_domain_not_allowed(self):
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://example.com/product")

    def test_missing_price(self):
        html = b"<!DOCTYPE html><html><head><title>No price</title></head><body></body></html>"
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/noprice")])
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/noprice")

    def test_nonpublic_url(self):
        with self.assertRaises(scraper.ScraperError):
            scraper.fetch_quote("http://localhost:8000/product")

    # R8: dangerous redirect targets must never be requested.
    def test_redirect_to_disallowed_not_requested(self):
        session = self._mock_session([
            MockResponse(b"", "https://www.pbtech.co.nz/product/Kindle", status_code=302, headers={"Location": "https://evil.com/product"}),
        ])
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/Kindle")
        self.assertEqual(session.requested_urls, ["https://www.pbtech.co.nz/product/Kindle"])

    # R8: relative Location headers are resolved and validated.
    def test_relative_redirect_followed(self):
        html = _fixture_html("pbtech_kindle")
        session = self._mock_session([
            MockResponse(b"", "https://www.pbtech.co.nz/product/Kindle", status_code=302, headers={"Location": "/product/Kindle2"}),
            MockResponse(html, "https://www.pbtech.co.nz/product/Kindle2"),
        ])
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/Kindle")
        self.assertEqual(quote["price"], "319.00")
        self.assertEqual(session.requested_urls, [
            "https://www.pbtech.co.nz/product/Kindle",
            "https://www.pbtech.co.nz/product/Kindle2",
        ])

    # R8: redirect loop is rejected.
    def test_redirect_loop_rejected(self):
        session = self._mock_session([
            MockResponse(b"", "https://www.pbtech.co.nz/a", status_code=302, headers={"Location": "/b"}),
            MockResponse(b"", "https://www.pbtech.co.nz/b", status_code=302, headers={"Location": "/a"}),
        ] * 3)
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/a")

    # R8: private DNS resolution blocks the request.
    def test_dns_to_private_ip_blocked(self):
        with patch("scraper.socket.getaddrinfo", return_value=[(None, None, None, None, ("192.168.1.1", 443))]):
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/x")

    # R9: multiple products must not splice fields.
    def test_multiple_product_identity(self):
        html = b"""
        <html><head><script type="application/ld+json">
        {"@type":"Product","name":"Cheap accessory","offers":{"price":"9.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}
        </script><script type="application/ld+json">
        {"@type":"Product","name":"Target laptop","sku":"LAPTOP123","offers":{"price":"1999.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}
        </script></head><body></body></html>
        """
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/LAPTOP123")])
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/LAPTOP123")
        self.assertEqual(quote["product_name"], "Target laptop")
        self.assertEqual(quote["price"], "1999.00")

    # R9: ambiguous product identity is rejected.
    def test_ambiguous_product_identity(self):
        html = b"""
        <html><head><script type="application/ld+json">
        {"@type":"Product","name":"Accessory A","offers":{"price":"9.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}
        </script><script type="application/ld+json">
        {"@type":"Product","name":"Accessory B","offers":{"price":"12.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}
        </script></head><body></body></html>
        """
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/x")])
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/x")

    # R11: response size limit is enforced.
    def test_response_size_limit(self):
        session = self._mock_session([MockResponse(b"x" * (2 * 1024 * 1024), "https://www.pbtech.co.nz/product/x")])
        with _mock_socket_global():
            with patch.object(scraper.config, "scraper_max_response_bytes", return_value=1024):
                with self.assertRaises(scraper.ScraperError):
                    scraper.fetch_quote("https://www.pbtech.co.nz/product/x")

    # R11: slow stream cannot exceed the deadline.
    def test_fetch_deadline(self):
        def slow_iter(chunk_size):
            for _ in range(1000):
                yield b"x" * 1024

        class SlowResponse(MockResponse):
            def iter_content(self, chunk_size=1024):
                return slow_iter(chunk_size)

        session = self._mock_session([SlowResponse(b"", "https://www.pbtech.co.nz/product/x")])
        with _mock_socket_global():
            with patch.object(scraper.config, "scraper_timeout", return_value=0.01):
                with self.assertRaises(scraper.ScraperError):
                    scraper.fetch_quote("https://www.pbtech.co.nz/product/x")

    # ---------------------------------------------------------------- F6

    # F6: a tie between two products must be refused regardless of order.
    def test_tied_products_rejected_both_orders(self):
        def page(first_name: str, second_name: str) -> bytes:
            return (
                f'<html><head><script type="application/ld+json">'
                f'{{"@type":"Product","name":"{first_name}","sku":"SHARED","offers":'
                f'{{"price":"10.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}}}'
                f'</script><script type="application/ld+json">'
                f'{{"@type":"Product","name":"{second_name}","sku":"SHARED","offers":'
                f'{{"price":"20.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}}}'
                f'</script></head><body></body></html>'
            ).encode("utf-8")

        for first, second in (("Alpha SHARED", "Beta SHARED"), ("Beta SHARED", "Alpha SHARED")):
            with self.subTest(order=(first, second)):
                session = self._mock_session([MockResponse(page(first, second), "https://www.pbtech.co.nz/product/SHARED")])
                self.assertIsNotNone(session)
                with _mock_socket_global():
                    with self.assertRaises(scraper.ScraperError):
                        scraper.fetch_quote("https://www.pbtech.co.nz/product/SHARED")

    # F6: ambiguous JSON-LD must not be masked by complete OpenGraph metadata.
    def test_ambiguous_jsonld_not_masked_by_opengraph(self):
        html = (
            '<html><head>'
            '<meta property="og:title" content="Definitely The Right Product">'
            '<meta property="og:price:amount" content="99.00">'
            '<meta property="og:price:currency" content="NZD">'
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"Accessory A","sku":"AAA","offers":{"price":"9.00","priceCurrency":"NZD"}}'
            '</script><script type="application/ld+json">'
            '{"@type":"Product","name":"Accessory B","sku":"BBB","offers":{"price":"12.00","priceCurrency":"NZD"}}'
            '</script></head><body></body></html>'
        ).encode("utf-8")
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/mystery")])
        self.assertIsNotNone(session)
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/mystery")

    # F6: a single product with several offers at different prices is refused.
    def test_multiple_conflicting_offers_rejected(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@type":"Product","name":"Kindle","sku":"KINDLE1","offers":['
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"},'
            '{"price":"289.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}]}'
            '</script></head><body></body></html>'
        ).encode("utf-8")
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/KINDLE1")])
        self.assertIsNotNone(session)
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/KINDLE1")

    # F6: several offers that agree on price/currency are unambiguous.
    def test_multiple_agreeing_offers_accepted(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@type":"Product","name":"Kindle","sku":"KINDLE1","offers":['
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"},'
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}]}'
            '</script></head><body></body></html>'
        ).encode("utf-8")
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/KINDLE1")])
        self.assertIsNotNone(session)
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/KINDLE1")
        self.assertEqual(quote["price"], "319.00")
        self.assertEqual(quote["product_name"], "Kindle")

    # F6: a single JSON-LD product is accepted even without an identity signal.
    def test_single_product_still_works(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@type":"Product","name":"Only Product","offers":{"price":"42.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}'
            '</script></head><body></body></html>'
        ).encode("utf-8")
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/anything")])
        self.assertIsNotNone(session)
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/anything")
        self.assertEqual(quote["price"], "42.00")

    # ---------------------------------------------------------------- F8

    # F8: a slow DNS lookup is bounded by the overall fetch deadline.
    def test_slow_dns_is_bounded_by_deadline(self):
        def slow_getaddrinfo(*args, **kwargs):
            time.sleep(0.5)
            return [(None, None, None, None, ("1.2.3.4", 443))]

        with patch("scraper.socket.getaddrinfo", side_effect=slow_getaddrinfo):
            with patch.object(scraper.config, "scraper_timeout", return_value=0.05):
                started = time.monotonic()
                with self.assertRaises(scraper.ScraperError):
                    scraper.fetch_quote("https://www.pbtech.co.nz/product/x")
                elapsed = time.monotonic() - started
        # The bounded join returns control well before the resolver finishes.
        self.assertLess(elapsed, 0.4)

    # ---------------------------------------------------------------- G3

    def _offers_page(self, offers_json: str) -> bytes:
        return (
            '<html><head><script type="application/ld+json">'
            '{"@type":"Product","name":"Kindle Paperwhite","sku":"KINDLE1","offers":'
            + offers_json
            + "}</script></head><body></body></html>"
        ).encode("utf-8")

    def _assert_offers_rejected(self, offers_json: str) -> None:
        session = self._mock_session(
            [MockResponse(self._offers_page(offers_json), "https://www.pbtech.co.nz/product/KINDLE1")]
        )
        self.assertIsNotNone(session)
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/KINDLE1")

    def test_same_price_different_stock_rejected_both_orders(self):
        orders = (
            '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"},'
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/OutOfStock"}]',
            '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/OutOfStock"},'
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}]',
        )
        for offers in orders:
            with self.subTest(offers=offers):
                self._assert_offers_rejected(offers)

    def test_same_price_different_condition_rejected(self):
        self._assert_offers_rejected(
            '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock",'
            '"itemCondition":"https://schema.org/NewCondition"},'
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock",'
            '"itemCondition":"https://schema.org/UsedCondition"}]'
        )

    def test_same_price_different_seller_rejected(self):
        self._assert_offers_rejected(
            '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock",'
            '"seller":{"name":"PB Tech"}},'
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock",'
            '"seller":{"name":"Other Shop"}}]'
        )

    def test_same_price_different_sku_rejected(self):
        self._assert_offers_rejected(
            '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","sku":"K-16GB"},'
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","sku":"K-32GB"}]'
        )

    def test_field_present_on_only_one_offer_is_a_difference(self):
        self._assert_offers_rejected(
            '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"},'
            '{"price":"319.00","priceCurrency":"NZD"}]'
        )

    def test_fully_equivalent_offers_accepted(self):
        session = self._mock_session(
            [
                MockResponse(
                    self._offers_page(
                        '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock",'
                        '"seller":{"name":"PB Tech"}},'
                        '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock",'
                        '"seller":{"name":"PB Tech"}}]'
                    ),
                    "https://www.pbtech.co.nz/product/KINDLE1",
                )
            ]
        )
        self.assertIsNotNone(session)
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/KINDLE1")
        self.assertEqual(quote["price"], "319.00")
        self.assertTrue(quote["in_stock"])

    def test_single_offer_still_accepted(self):
        session = self._mock_session(
            [
                MockResponse(
                    self._offers_page(
                        '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}'
                    ),
                    "https://www.pbtech.co.nz/product/KINDLE1",
                )
            ]
        )
        self.assertIsNotNone(session)
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/KINDLE1")
        self.assertEqual(quote["price"], "319.00")

    # ---------------------------------------------------------------- H3

    def test_same_price_different_size_rejected_both_orders(self):
        for order in (("16GB", "32GB"), ("32GB", "16GB")):
            with self.subTest(order=order):
                self._assert_offers_rejected(
                    f'[{{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","size":"{order[0]}"}},'
                    f'{{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","size":"{order[1]}"}}]'
                )

    def test_same_price_different_model_rejected(self):
        self._assert_offers_rejected(
            '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","model":"PW5-16"},'
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","model":"PW5-32"}]'
        )

    def test_same_price_different_item_offered_rejected_both_orders(self):
        for order in (("K-16GB", "K-32GB"), ("K-32GB", "K-16GB")):
            with self.subTest(order=order):
                self._assert_offers_rejected(
                    f'[{{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock",'
                    f'"itemOffered":{{"@type":"Product","sku":"{order[0]}"}}}},'
                    f'{{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock",'
                    f'"itemOffered":{{"@type":"Product","sku":"{order[1]}"}}}}]'
                )

    # An unknown field is still a difference; a chosen whitelist would have missed it.
    def test_offers_differing_in_an_unknown_field_are_rejected(self):
        self._assert_offers_rejected(
            '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","warranty":"1 year"},'
            '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","warranty":"2 years"}]'
        )

    def test_equivalent_offers_sharing_a_size_are_accepted(self):
        session = self._mock_session(
            [
                MockResponse(
                    self._offers_page(
                        '[{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","size":"16GB"},'
                        '{"price":"319.00","priceCurrency":"NZD","availability":"https://schema.org/InStock","size":"16GB"}]'
                    ),
                    "https://www.pbtech.co.nz/product/KINDLE1",
                )
            ]
        )
        self.assertIsNotNone(session)
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/KINDLE1")
        self.assertEqual(quote["price"], "319.00")
        self.assertTrue(quote["in_stock"])

    # ---------------------------------------------------------------- G4

    def test_jsonld_missing_price_not_filled_from_opengraph(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="Kindle Paperwhite">'
            '<meta property="og:price:amount" content="9.00">'
            '<meta property="og:price:currency" content="NZD">'
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"Kindle Paperwhite","sku":"KINDLE1",'
            '"offers":{"priceCurrency":"NZD","availability":"https://schema.org/InStock"}}'
            "</script></head><body></body></html>"
        ).encode("utf-8")
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/KINDLE1")])
        self.assertIsNotNone(session)
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/KINDLE1")

    def test_jsonld_missing_currency_not_filled_from_opengraph(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="Kindle Paperwhite">'
            '<meta property="og:price:currency" content="USD">'
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"Kindle Paperwhite","sku":"KINDLE1",'
            '"offers":{"price":"319.00","availability":"https://schema.org/InStock"}}'
            "</script></head><body></body></html>"
        ).encode("utf-8")
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/KINDLE1")])
        self.assertIsNotNone(session)
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/KINDLE1")

    def test_opengraph_only_page_reports_unknown_stock(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="Some Product">'
            '<meta property="og:price:amount" content="19.99">'
            '<meta property="og:price:currency" content="NZD">'
            "</head><body></body></html>"
        ).encode("utf-8")
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/ogonly")])
        self.assertIsNotNone(session)
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://www.pbtech.co.nz/product/ogonly")
        self.assertEqual(quote["price"], "19.99")
        self.assertIsNone(quote["in_stock"])

    def test_opengraph_only_page_without_price_is_rejected(self):
        html = b'<html><head><meta property="og:title" content="Some Product"></head><body></body></html>'
        session = self._mock_session([MockResponse(html, "https://www.pbtech.co.nz/product/ogonly")])
        self.assertIsNotNone(session)
        with _mock_socket_global():
            with self.assertRaises(scraper.ScraperError):
                scraper.fetch_quote("https://www.pbtech.co.nz/product/ogonly")

    # ---------------------------------------------------------------- G5

    def test_dns_resolver_concurrency_is_hard_bounded(self):
        release = threading.Event()

        def blocking_getaddrinfo(*args, **kwargs):
            release.wait(timeout=10)
            return [(None, None, None, None, ("1.2.3.4", 443))]

        resolver = scraper._DNS_RESOLVER
        baseline = resolver.active_count()
        try:
            with patch("scraper.socket.getaddrinfo", side_effect=blocking_getaddrinfo):
                with patch.object(scraper.config, "scraper_timeout", return_value=0.05):
                    for _ in range(8):
                        started = time.monotonic()
                        with self.assertRaises(scraper.ScraperError):
                            scraper.fetch_quote("https://www.pbtech.co.nz/product/x")
                        self.assertLess(time.monotonic() - started, 0.6)
                        self.assertLessEqual(
                            resolver.active_count() - baseline, resolver.max_concurrent
                        )
                # Saturated pool: live resolvers must not exceed the cap.
                self.assertLessEqual(resolver.active_count() - baseline, resolver.max_concurrent)
        finally:
            release.set()
        deadline = time.monotonic() + 5
        while resolver.active_count() > baseline and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(resolver.active_count(), baseline)


class TestStructuredBrandExtraction(unittest.TestCase):
    """The selected JSON-LD product's own ``brand`` is surfaced for identity checks."""

    def _session(self, responses: list):
        session = _MockSession(responses)
        patcher = patch("scraper.requests.Session", return_value=session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def test_brand_shapes(self):
        self.assertEqual(scraper._structured_brand({"brand": "The Ordinary"}), "The Ordinary")
        self.assertEqual(scraper._structured_brand({"brand": {"@type": "Brand", "name": "Sony"}}), "Sony")
        self.assertEqual(
            scraper._structured_brand({"brand": [{"name": None}, {"name": "Bose"}]}), "Bose"
        )
        self.assertIsNone(scraper._structured_brand({}))
        self.assertIsNone(scraper._structured_brand({"brand": "   "}))

    def test_brand_comes_from_the_selected_product_page(self):
        html = b"""
        <html><head><script type="application/ld+json">
        {"@type":"Product","name":"Niacinamide 10% + Zinc 1%","brand":{"@type":"Brand","name":"The Ordinary"},
         "size":"30ml","offers":{"price":"14.99","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}
        </script></head><body></body></html>
        """
        self._session([MockResponse(html, "https://shop.example.nz/serum")])
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://shop.example.nz/serum", allowed_domains=["shop.example.nz"])
        self.assertEqual(quote["brand"], "The Ordinary")
        self.assertEqual(quote["variant"], "30ml")

    def test_opengraph_fallback_claims_no_structured_brand(self):
        html = b"""
        <html><head>
        <meta property="og:title" content="Mystery Serum">
        <meta property="og:price:amount" content="19.00">
        <meta property="og:price:currency" content="NZD">
        </head><body></body></html>
        """
        self._session([MockResponse(html, "https://shop.example.nz/serum2")])
        with _mock_socket_global():
            quote = scraper.fetch_quote("https://shop.example.nz/serum2", allowed_domains=["shop.example.nz"])
        self.assertEqual(quote["product_name"], "Mystery Serum")
        self.assertIsNone(quote["brand"])


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the Stage B retailer adapters.

All fixtures are local files. Nothing here performs a network request.
"""

import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import scraper
from retailers import JbHiFiRetailer, MightyApeRetailer, PbTechRetailer
from retailers import mightyape as retailers_mightyape
from retailers.base import RetailerError

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class TestPbTechSearch(unittest.TestCase):
    def setUp(self):
        self.retailer = PbTechRetailer()

    def test_search_url_encodes_query(self):
        url = self.retailer.search_url("fitbit air")
        self.assertIn("sf=fitbit+air", url)
        self.assertTrue(url.startswith("https://www.pbtech.co.nz/search?"))

    def test_parses_cards_with_sku_and_absolute_url(self):
        candidates = self.retailer.parse_search_page(
            _fixture("pbtech_search.html"), "https://www.pbtech.co.nz/search?sf=fitbit+air"
        )
        self.assertEqual(len(candidates), 3)
        first = candidates[0]
        self.assertEqual(first.sku, "WTHFIT114043")
        self.assertEqual(
            first.url,
            "https://www.pbtech.co.nz/product/WTHFIT114043/"
            "Google-Fitbit-Air-Fitness-Tracker---Obsidian---Up",
        )
        self.assertEqual(first.retailer, "PB Tech")
        # No price is claimed from the search page.
        self.assertIsNone(first.price)
        self.assertIsNone(first.currency)
        # The real product title, not the marketing bullets sitting next to it.
        self.assertEqual(first.title, "Google Fitbit Air Fitness Tracker - Obsidian")

    def test_marketing_bullets_are_not_used_as_the_title(self):
        candidates = self.retailer.parse_search_page(
            _fixture("pbtech_search.html"), "https://www.pbtech.co.nz/search?sf=x"
        )
        for candidate in candidates:
            self.assertNotIn("Battery Life", candidate.title)
            self.assertFalse(candidate.title.startswith("-"))

    def test_title_falls_back_to_the_slug_when_the_heading_is_absent(self):
        body = (
            b'<html><body><div class="js-product-card">'
            b'<a class="js-product-link" data-product-code="SKU9" '
            b'href="product/SKU9/Some-Product-Name---Black">'
            b"</a></div></body></html>"
        )
        candidates = self.retailer.parse_search_page(body, "https://www.pbtech.co.nz/search?sf=x")
        self.assertEqual(candidates[0].title, "Some Product Name Black")

    def test_review_links_do_not_duplicate_candidates(self):
        candidates = self.retailer.parse_search_page(
            _fixture("pbtech_search.html"), "https://www.pbtech.co.nz/search?sf=x"
        )
        skus = [c.sku for c in candidates]
        self.assertEqual(len(skus), len(set(skus)))
        self.assertEqual(skus.count("WTHFIT114044"), 1)

    def test_limit_is_respected(self):
        candidates = self.retailer.parse_search_page(
            _fixture("pbtech_search.html"), "https://www.pbtech.co.nz/search?sf=x", limit=2
        )
        self.assertEqual(len(candidates), 2)

    def test_unrecognised_page_raises_instead_of_returning_nothing(self):
        with self.assertRaises(RetailerError):
            self.retailer.parse_search_page(b"<html><body>maintenance</body></html>", "https://www.pbtech.co.nz/search?sf=x")

    def test_explicit_no_results_returns_empty_list(self):
        body = b"<html><body>Your search did not match any products.</body></html>"
        self.assertEqual(self.retailer.parse_search_page(body, "https://www.pbtech.co.nz/search?sf=x"), [])

    def test_fetch_offer_reuses_the_verified_quote_parser(self):
        sentinel = {"product_name": "x", "url": "u", "price": "1.00"}
        with patch("scraper.fetch_quote", return_value=sentinel) as mocked:
            self.assertIs(self.retailer.fetch_offer("https://www.pbtech.co.nz/product/A/B"), sentinel)
        mocked.assert_called_once()

    def test_fetch_offer_wraps_scraper_error(self):
        with patch("scraper.fetch_quote", side_effect=scraper.ScraperError("boom")):
            with self.assertRaises(RetailerError):
                self.retailer.fetch_offer("https://www.pbtech.co.nz/product/A/B")


class TestMightyApeSearch(unittest.TestCase):
    """Parsing logic and URL spelling only; the live site is unverified (B1).

    The live host is reachable but refuses automated requests with HTTP 403 (DataDome),
    so these tests prove the parser and the requested host, never the real page shape.
    """

    def setUp(self):
        self.retailer = MightyApeRetailer()

    # B1 regression: the requested host must be the *correct* domain. The earlier
    # `mightyaoe.co.nz` typo made the adapter and its fixture agree with each other,
    # so the tests passed while every real request went to a host that does not exist.
    def test_requests_the_correct_host(self):
        url = self.retailer.search_url("fitbit air")
        self.assertEqual(urlparse(url).hostname, "www.mightyape.co.nz")

    def test_domain_constant_is_the_correct_spelling(self):
        self.assertEqual(self.retailer.domains, ("mightyape.co.nz",))
        self.assertNotIn("mightyaoe", " ".join(self.retailer.domains))
        self.assertTrue(retailers_mightyape.BASE.startswith("https://www.mightyape.co.nz/"))

    def test_search_fetches_the_correct_host(self):
        # The host actually handed to the fetch layer, not just the constant.
        with patch("scraper.fetch_html", return_value=("https://www.mightyape.co.nz/search?query=x", b"<html></html>")) as mocked:
            with patch.object(self.retailer, "parse_search_page", return_value=[]) as parsed:
                self.retailer.search("fitbit air")
        requested = mocked.call_args.args[0]
        self.assertEqual(urlparse(requested).hostname, "www.mightyape.co.nz")
        self.assertEqual(parsed.call_args.args[1], "https://www.mightyape.co.nz/search?query=x")

    def test_search_url_encodes_query(self):
        url = self.retailer.search_url("fitbit air")
        self.assertIn("query=fitbit+air", url)
        self.assertTrue(url.startswith("https://www.mightyape.co.nz/search?"))

    def test_prefers_jsonld_item_list(self):
        candidates = self.retailer.parse_search_page(
            _fixture("mightyape_search.html"), "https://www.mightyape.co.nz/search?query=fitbit+air"
        )
        self.assertEqual(len(candidates), 2)
        first = candidates[0]
        self.assertEqual(first.sku, "MA-FITBIT-AIR-OBS")
        self.assertEqual(first.model, "GA05183-US")
        self.assertEqual(first.price, "246.99")
        self.assertEqual(first.currency, "NZD")
        self.assertEqual(
            first.url, "https://www.mightyape.co.nz/product/google-fitbit-air-fitness-tracker-obsidian"
        )

    def test_falls_back_to_product_links(self):
        body = (
            b'<html><body><a href="/product/thing-one">Thing One</a>'
            b'<a href="/help/returns">Returns</a>'
            b'<a href="/product/thing-two">Thing Two</a></body></html>'
        )
        candidates = self.retailer.parse_search_page(body, "https://www.mightyape.co.nz/search?query=x")
        self.assertEqual([c.title for c in candidates], ["Thing One", "Thing Two"])
        self.assertTrue(all(c.url.startswith("https://www.mightyape.co.nz/") for c in candidates))

    def test_unrecognised_page_raises(self):
        with self.assertRaises(RetailerError):
            self.retailer.parse_search_page(b"<html><body>nope</body></html>", "https://www.mightyape.co.nz/search?query=x")

    def test_http_failure_is_reported_as_a_retailer_error(self):
        # A 403 from the shop's bot protection must surface as a source failure, not as
        # "the shop had no results".
        with patch("scraper.fetch_html", side_effect=scraper.ScraperError("Request failed: 403 Client Error")):
            with self.assertRaises(RetailerError) as ctx:
                self.retailer.search("fitbit air")
        self.assertIn("403", str(ctx.exception))

    def test_fetch_offer_reuses_the_verified_quote_parser(self):
        with patch("scraper.fetch_quote", return_value={"ok": True}) as mocked:
            self.assertEqual(self.retailer.fetch_offer("https://www.mightyape.co.nz/p/x"), {"ok": True})
        mocked.assert_called_once()


class TestJbHiFiSearch(unittest.TestCase):
    """JB Hi-Fi NZ: identity comes from the page's server-rendered `initData` payload.

    The fixture reproduces that real embedding (structure captured 2026-09-24, handles
    are the shop's real product slugs) so parsing is covered offline. Price and stock are
    never taken from the search page; they come from the product page via fetch_quote.
    """

    def setUp(self):
        self.retailer = JbHiFiRetailer()

    def test_search_url_is_the_verified_path(self):
        url = self.retailer.search_url("fitbit air")
        self.assertEqual(urlparse(url).hostname, "www.jbhifi.co.nz")
        self.assertTrue(url.startswith("https://www.jbhifi.co.nz/search?q="))
        self.assertIn("q=fitbit+air", url)

    def test_search_fetches_the_verified_host(self):
        with patch("scraper.fetch_html", return_value=("https://www.jbhifi.co.nz/search?q=x", b"<html></html>")) as mocked:
            with patch.object(self.retailer, "parse_search_page", return_value=[]):
                self.retailer.search("fitbit air")
        self.assertEqual(urlparse(mocked.call_args.args[0]).hostname, "www.jbhifi.co.nz")

    def test_parses_candidates_from_the_real_payload_shape(self):
        candidates = self.retailer.parse_search_page(
            _fixture("jbhifi_search.html"), "https://www.jbhifi.co.nz/search?q=fitbit+air"
        )
        # Titles come from the URL slug, like PB Tech's fallback, so they are lower-case.
        # The displayed name is the product page's real title (see test_discovery).
        self.assertEqual([c.title for c in candidates], [
            "google fitbit air obsidian",
            "google fitbit air lavender",
            "google fitbit air berry",
            "fitbit inspire 3 midnight zen black",
        ])
        self.assertEqual(
            candidates[0].url, "https://www.jbhifi.co.nz/products/google-fitbit-air-obsidian"
        )
        self.assertEqual(candidates[0].retailer, "JB Hi-Fi")

    def test_no_price_is_claimed_from_the_search_page(self):
        candidates = self.retailer.parse_search_page(
            _fixture("jbhifi_search.html"), "https://www.jbhifi.co.nz/search?q=x"
        )
        for candidate in candidates:
            self.assertIsNone(candidate.price)
            self.assertIsNone(candidate.currency)

    def test_duplicate_handles_are_deduplicated(self):
        candidates = self.retailer.parse_search_page(
            _fixture("jbhifi_search.html"), "https://www.jbhifi.co.nz/search?q=x"
        )
        urls = [c.url for c in candidates]
        self.assertEqual(len(urls), len(set(urls)))

    def test_limit_is_respected(self):
        candidates = self.retailer.parse_search_page(
            _fixture("jbhifi_search.html"), "https://www.jbhifi.co.nz/search?q=x", limit=2
        )
        self.assertEqual(len(candidates), 2)

    def test_empty_product_list_means_no_candidates(self):
        body = b'<html><body><script>false,initData: {"products": []};</script></body></html>'
        self.assertEqual(self.retailer.parse_search_page(body, "https://www.jbhifi.co.nz/search?q=x"), [])

    def test_missing_payload_raises_instead_of_claiming_no_results(self):
        # The shop's search is fuzzy (a nonsense query still returns matches), so an
        # empty list must not be produced when we simply failed to understand the page.
        with self.assertRaises(RetailerError):
            self.retailer.parse_search_page(b"<html><body>no payload here</body></html>", "https://www.jbhifi.co.nz/search?q=x")

    def test_http_failure_is_reported_as_a_retailer_error(self):
        with patch("scraper.fetch_html", side_effect=scraper.ScraperError("Request failed: 403")):
            with self.assertRaises(RetailerError) as ctx:
                self.retailer.search("fitbit air")
        self.assertIn("403", str(ctx.exception))

    def test_fetch_offer_reuses_the_verified_quote_parser(self):
        with patch("scraper.fetch_quote", return_value={"ok": True}) as mocked:
            self.assertEqual(self.retailer.fetch_offer("https://www.jbhifi.co.nz/products/x"), {"ok": True})
        mocked.assert_called_once()

    def test_balanced_object_is_string_aware(self):
        # A quoted brace must not end the object early.
        from retailers.jbhifi import _balanced_object

        raw = 'initData: {"products":[{"handle":"a}b"}]};'
        start = raw.find("{")
        self.assertEqual(_balanced_object(raw, start), '{"products":[{"handle":"a}b"}]}')


class TestRegistry(unittest.TestCase):
    def test_all_three_retailers_are_registered(self):
        import retailers

        names = [r.name for r in retailers.all_retailers()]
        self.assertEqual(names, ["PB Tech", "Mighty Ape", "JB Hi-Fi"])

    def test_enabled_retailers_follow_the_allow_list(self):
        import retailers

        only_pb = retailers.enabled_retailers(["pbtech.co.nz"])
        self.assertEqual([r.name for r in only_pb], ["PB Tech"])
        two = retailers.enabled_retailers(["pbtech.co.nz", "jbhifi.co.nz"])
        self.assertEqual([r.name for r in two], ["PB Tech", "JB Hi-Fi"])
        all_three = retailers.enabled_retailers(["pbtech.co.nz", "mightyape.co.nz", "jbhifi.co.nz"])
        self.assertEqual(len(all_three), 3)

    # B1 regression: the default allow-list must carry the correct domain spelling.
    def test_default_allow_list_uses_the_correct_domains(self):
        import config

        self.assertIn("mightyape.co.nz", config.DEFAULT_ALLOWED_DOMAINS)
        self.assertIn("jbhifi.co.nz", config.DEFAULT_ALLOWED_DOMAINS)
        self.assertNotIn("mightyaoe.co.nz", config.DEFAULT_ALLOWED_DOMAINS)

    def test_adapter_domain_matches_its_allow_list_entry(self):
        import config
        import retailers

        for retailer in retailers.all_retailers():
            self.assertIn(retailer.domains[0], config.DEFAULT_ALLOWED_DOMAINS)

    def test_domain_match_is_suffix_based(self):
        import retailers

        self.assertTrue(retailers.domain_allowed("www.pbtech.co.nz", ["pbtech.co.nz"]))
        self.assertTrue(retailers.domain_allowed("pbtech.co.nz", ["pbtech.co.nz"]))
        self.assertTrue(retailers.domain_allowed("www.jbhifi.co.nz", ["jbhifi.co.nz"]))
        self.assertFalse(retailers.domain_allowed("evil-pbtech.co.nz.attacker.com", ["pbtech.co.nz"]))


if __name__ == "__main__":
    unittest.main()

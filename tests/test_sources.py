import unittest
from unittest.mock import patch

import requests

from sources import base
from sources.base import ProductRef
from sources.firecrawl import Firecrawl
from sources.registry import build_providers
from sources.serpapi import SerpApi
from sources.tavily import TavilyExtract
from tests.fakes import FakeSender, load_fixture

NZ = {"gl": "nz", "hl": "en", "google_domain": "google.co.nz", "location": "New Zealand"}
KEY = "serp-secret-key-123"


def serp(*responses, **kw):
    sender = FakeSender(*responses)
    return SerpApi(KEY, sender=sender, locale=NZ, timeout=5, **kw), sender


class TestParsePrice(unittest.TestCase):
    def test_currency_markers(self):
        self.assertEqual(base.parse_price("NZ$1,299.00"), ("1299.00", "NZD"))
        self.assertEqual(base.parse_price("$229", 229.0), ("229.00", "NZD"))
        self.assertEqual(base.parse_price("US$150.00"), ("150.00", "USD"))
        self.assertEqual(base.parse_price("A$99"), ("99.00", "AUD"))

    def test_unknown_currency_is_none(self):
        self.assertEqual(base.parse_price("€120,00"), ("120.00", None))
        self.assertEqual(base.parse_price("$50", default_dollar=None), ("50.00", None))

    def test_invalid_amounts(self):
        self.assertEqual(base.parse_price("Call for price"), (None, None))
        self.assertEqual(base.parse_price("$0.00"), (None, "NZD"))

    def test_detect_condition(self):
        self.assertEqual(base.detect_condition("Fitbit (Renewed)"), "refurbished")
        self.assertEqual(base.detect_condition("pre-owned watch"), "used")
        self.assertIsNone(base.detect_condition("Brand new Fitbit"))


class TestSerpApiSearch(unittest.TestCase):
    def test_parses_and_dedupes_results(self):
        client, sender = serp(load_fixture("serpapi", "shopping_fitbit.json"))
        results = client.search("fitbit charge 6")
        self.assertEqual([r.product_id for r in results], ["111", "222", "333", None])
        first = results[0]
        self.assertEqual((first.merchant, first.price, first.currency), ("Noel Leeming", "229.00", "NZD"))
        self.assertEqual(first.page_token, "tok-111")
        self.assertEqual(results[2].condition, "refurbished")
        params = sender.calls[0]["params"]
        self.assertEqual((params["engine"], params["gl"], params["q"]), ("google_shopping", "nz", "fitbit charge 6"))
        self.assertEqual(client.calls, 1)

    def test_no_results_is_empty_list(self):
        client, _ = serp(load_fixture("serpapi", "no_results.json"))
        self.assertEqual(client.search("zzzz"), [])

    def test_out_of_searches_is_quota_error(self):
        client, _ = serp(load_fixture("serpapi", "out_of_searches.json"))
        with self.assertRaises(base.QuotaExceeded):
            client.search("x")

    def test_http_errors_map_to_types(self):
        for status, exc in ((401, base.AuthError), (429, base.RateLimited), (500, base.Unavailable)):
            client, _ = serp((status, {"error": "nope"}))
            with self.assertRaises(exc):
                client.search("x")

    def test_key_never_leaks_from_transport_errors(self):
        leak = requests.exceptions.ConnectionError(f"https://serpapi.com/search.json?api_key={KEY}")
        for error, exc in ((leak, base.Unavailable), (requests.exceptions.ReadTimeout(KEY), base.ProviderTimeout)):
            client, _ = serp(error)
            with self.assertRaises(exc) as ctx:
                client.search("x")
            self.assertNotIn(KEY, str(ctx.exception))
            self.assertIsNone(ctx.exception.__cause__)

    def test_not_configured(self):
        with self.assertRaises(base.NotConfigured):
            SerpApi("", sender=FakeSender(), locale=NZ).search("x")

    def test_quota_guard_runs_before_each_call(self):
        seen = []

        def guard(name):
            seen.append(name)
            if len(seen) > 1:
                raise base.QuotaExceeded("daily cap")

        client, sender = serp(load_fixture("serpapi", "no_results.json"), before_call=guard)
        client.search("a")
        with self.assertRaises(base.QuotaExceeded):
            client.search("b")
        self.assertEqual(len(sender.calls), 1)


class TestSerpApiOffers(unittest.TestCase):
    def test_immersive_offers(self):
        client, sender = serp(load_fixture("serpapi", "immersive_fitbit.json"))
        offers, ref = client.offers(ProductRef("111", "tok-111", "fitbit charge 6"))
        self.assertEqual([o.merchant for o in offers], ["Noel Leeming", "JB Hi-Fi", "Cheap Gadgets NZ", "Overseas Store"])
        self.assertEqual(offers[1].price, "219.00")
        self.assertEqual(offers[2].condition, "refurbished")
        self.assertEqual(offers[3].currency, "USD")
        self.assertEqual(sender.calls[0]["params"]["engine"], "google_immersive_product")
        self.assertNotIn("location", sender.calls[0]["params"])
        self.assertEqual(ref.page_token, "tok-111")

    def test_falls_back_to_product_id(self):
        client, sender = serp(load_fixture("serpapi", "product_fitbit.json"))
        offers, _ = client.offers(ProductRef(product_id="111"))
        self.assertEqual([(o.merchant, o.price) for o in offers], [("PB Tech", "239.00"), ("Harvey Norman", "235.00")])
        self.assertEqual(sender.calls[0]["params"]["engine"], "google_product")

    def test_stale_token_is_refreshed_by_research(self):
        shopping = load_fixture("serpapi", "shopping_fitbit.json")
        shopping["shopping_results"][0]["immersive_product_page_token"] = "tok-new"
        client, sender = serp(
            load_fixture("serpapi", "no_results.json"),  # stale token
            load_fixture("serpapi", "no_results.json"),  # google_product empty
            shopping,
            load_fixture("serpapi", "immersive_fitbit.json"),
        )
        offers, ref = client.offers(ProductRef("111", "tok-old", "fitbit charge 6"))
        self.assertEqual(len(offers), 4)
        self.assertEqual(ref.page_token, "tok-new")
        self.assertEqual(len(sender.calls), 4)

    def test_nothing_found(self):
        client, _ = serp(load_fixture("serpapi", "no_results.json"))
        self.assertEqual(client.offers(ProductRef(page_token="t"))[0], [])


class TestTavilyExtract(unittest.TestCase):
    def test_returns_page_text_with_bearer_auth(self):
        sender = FakeSender(load_fixture("tavily", "extract_ok.json"))
        page = TavilyExtract("tvly-x", sender=sender, timeout=5).extract("https://www.example-shop.co.nz/p/kindle")
        self.assertIn("$279.00", page.text)
        self.assertEqual(sender.calls[0]["headers"]["Authorization"], "Bearer tvly-x")
        self.assertEqual(sender.calls[0]["json"]["urls"], ["https://www.example-shop.co.nz/p/kindle"])

    def test_failed_page_raises_unavailable(self):
        sender = FakeSender(load_fixture("tavily", "extract_failed.json"))
        with self.assertRaisesRegex(base.Unavailable, "403"):
            TavilyExtract("k", sender=sender, timeout=5).extract("https://blocked.example.nz/p")

    def test_rejects_non_http(self):
        with self.assertRaises(ValueError):
            TavilyExtract("k", sender=FakeSender(), timeout=5).extract("file:///etc/passwd")


class TestFirecrawl(unittest.TestCase):
    def test_search_keeps_page_text_but_no_price(self):
        sender = FakeSender(load_fixture("firecrawl", "search.json"))
        results = Firecrawl("fc", sender=sender, timeout=5).search("fitbit charge 6")
        self.assertEqual(len(results), 2)
        self.assertIsNone(results[0].price)
        self.assertIn("NZ$209.00", results[0].page_text)
        self.assertNotIn("page_text", results[0].to_dict())

    def test_extract_validates_structured_price(self):
        sender = FakeSender(load_fixture("firecrawl", "scrape_ok.json"))
        quote = Firecrawl("fc", sender=sender, timeout=5).extract("https://shop.example.nz/kindle")
        self.assertEqual((quote["price"], quote["currency"], quote["in_stock"]), ("279.00", "NZD", True))
        self.assertEqual(sender.calls[0]["json"]["formats"], ["json"])

    def test_extract_without_price_fails_closed(self):
        sender = FakeSender(load_fixture("firecrawl", "scrape_noprice.json"))
        with self.assertRaises(base.Unavailable):
            Firecrawl("fc", sender=sender, timeout=5).extract("https://shop.example.nz/kindle")


class TestRegistry(unittest.TestCase):
    def test_missing_keys_are_reported_unavailable(self):
        with patch("config.serpapi_api_key", return_value=""), patch("config.firecrawl_api_key", return_value=""), \
                patch("config.tavily_api_key", return_value="t"), patch("config.page_extractors", return_value=["tavily", "firecrawl"]):
            providers = build_providers()
        self.assertIsNone(providers.shopping)
        self.assertEqual([p.name for p in providers.extractors], ["tavily"])
        self.assertIn("serpapi", providers.unavailable)
        self.assertEqual(providers.status()["extractors"], "tavily")

    def test_all_configured(self):
        with patch("config.serpapi_api_key", return_value="s"), patch("config.firecrawl_api_key", return_value="f"), \
                patch("config.tavily_api_key", return_value="t"), patch("config.page_extractors", return_value=["firecrawl", "tavily"]):
            providers = build_providers()
        self.assertEqual(providers.shopping.name, "serpapi")
        self.assertEqual(providers.supplement.name, "firecrawl")
        self.assertEqual([p.name for p in providers.extractors], ["firecrawl", "tavily"])


if __name__ == "__main__":
    unittest.main()

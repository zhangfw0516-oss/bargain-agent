"""Tests for the LLM price-extraction fallback (the "use AI everywhere" path).

The LLM no longer only parses the user's request; it can also read an arbitrary
product page's visible text when the page has no machine-readable JSON-LD/OpenGraph
price. These tests cover validation, anti-injection, text bounding, and the strict
fallback ordering: fetch failures are never retried through the model.
"""

import json
import unittest
from unittest.mock import patch

import agent
import schemas


class FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = type("Message", (), {"content": content})()
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [FakeChoice(content, finish_reason)]


class FakeClient:
    def __init__(self, responses):
        self.chat = self
        self.completions = self
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _extraction(product_name="Lumiere Serum 30ml", price="59.00", currency="NZD", in_stock=True):
    return {"product_name": product_name, "price": price, "currency": currency, "in_stock": in_stock}


class TestValidatePriceExtraction(unittest.TestCase):
    def test_valid_quote_is_normalised(self):
        out = schemas.validate_price_extraction(_extraction(price="59", currency="nzd"))
        self.assertEqual(out["price"], "59.00")
        self.assertEqual(out["currency"], "NZD")

    def test_missing_price_fails_closed(self):
        with self.assertRaises(ValueError):
            schemas.validate_price_extraction(_extraction(price=None))

    def test_invented_or_bad_price_fails(self):
        for bad in ("abc", "-5.00", "0.00", "12.345"):
            with self.assertRaises(ValueError):
                schemas.validate_price_extraction(_extraction(price=bad))

    def test_unsupported_currency_fails(self):
        with self.assertRaises(ValueError):
            schemas.validate_price_extraction(_extraction(currency="EUR"))

    def test_unknown_stock_is_null_ok(self):
        out = schemas.validate_price_extraction(_extraction(in_stock=None))
        self.assertIsNone(out["in_stock"])

    def test_bad_stock_fails(self):
        with self.assertRaises(ValueError):
            schemas.validate_price_extraction(_extraction(in_stock="yes"))

    def test_blank_product_name_fails(self):
        with self.assertRaises(ValueError):
            schemas.validate_price_extraction(_extraction(product_name="   "))


class TestExtractPriceFromPageText(unittest.TestCase):
    def test_round_trip(self):
        client = FakeClient([FakeResponse(json.dumps(_extraction()))])
        out = agent.extract_price_from_page_text(
            "Lumiere Serum 30ml - $59.00 NZD - In stock", client=client, model="test"
        )
        self.assertEqual(out["price"], "59.00")
        self.assertEqual(out["currency"], "NZD")
        # The prompt must tell the model the page text is data, not instructions.
        system = client.calls[0]["messages"][0]["content"]
        self.assertIn("DATA", system)

    def test_empty_text_is_refused(self):
        with self.assertRaises(agent.PriceExtractionError):
            agent.extract_price_from_page_text("   ", client=FakeClient([]), model="test")

    def test_invalid_json_fails_closed(self):
        client = FakeClient([FakeResponse(json.dumps({"price": "9.00", "currency": "NZD", "in_stock": True, "product_name": None}))])
        with self.assertRaises(agent.PriceExtractionError):
            agent.extract_price_from_page_text("x", client=client, model="test")

    def test_truncated_then_valid_retries(self):
        client = FakeClient([
            FakeResponse("", finish_reason="length"),
            FakeResponse(json.dumps(_extraction())),
        ])
        out = agent.extract_price_from_page_text("text", client=client, model="test")
        self.assertEqual(out["price"], "59.00")
        self.assertEqual(len(client.calls), 2)

    def test_client_error_does_not_leak_key(self):
        client = FakeClient([RuntimeError("boom")])
        with self.assertRaises(agent.PriceExtractionError) as cm:
            agent.extract_price_from_page_text("text", client=client, model="test")
        self.assertNotIn("BSA-", str(cm.exception))
        self.assertNotIn("api_key", str(cm.exception))


class TestPageTextAndFallbackOrdering(unittest.TestCase):
    def test_page_text_strips_scripts_and_styles(self):
        html = b"<html><head><script>bad()</script><style>a{}</style><title>T</title></head><body><h1>Lumiere Serum</h1><p>$59.00 NZD</p></body></html>"
        from scraper import _page_text
        text = _page_text(html)
        self.assertIn("Lumiere Serum", text)
        self.assertNotIn("bad()", text)

    def test_page_text_is_length_bounded(self):
        from scraper import _page_text, MAX_PAGE_TEXT_CHARS
        long_body = "word " * 100000
        text = _page_text(f"<html><body>{long_body}</body></html>".encode("utf-8"))
        self.assertLessEqual(len(text), MAX_PAGE_TEXT_CHARS)

    def test_structured_price_wins_no_llm_call(self):
        # A page with valid JSON-LD must never reach the LLM.
        import scraper
        ld = json.dumps([{
            "@type": "Product",
            "name": "Widget",
            "offers": {"price": "19.00", "priceCurrency": "NZD", "availability": "https://schema.org/InStock"},
        }])
        html = f"<html><head><script type='application/ld+json'>{ld}</script></head><body>x</body></html>".encode("utf-8")
        with patch.object(scraper, "_quote_from_llm") as llm_mock:
            # fetch_quote_on_host_llm needs a mocked fetch_html to avoid network.
            with patch.object(scraper, "fetch_html", return_value=("https://e.co.nz/p", html)):
                out = scraper.fetch_quote_on_host_llm("https://e.co.nz/p")
        self.assertEqual(out["price"], "19.00")
        llm_mock.assert_not_called()

    def test_no_structured_price_falls_back_to_llm(self):
        import scraper
        html = b"<html><head><title>Plain</title></head><body><h1>Serum</h1><p>$59.00 NZD In stock</p></body></html>"
        fake = {**{"product_name": "Serum", "price": "59.00", "currency": "NZD", "in_stock": True}, "url": "https://e.co.nz/p", "retailer": "e.co.nz", "fetched_at": "2026-01-01T00:00:00+00:00", "shipping_cost": None, "error": None, "brand": None, "variant": None}
        with patch.object(scraper, "fetch_html", return_value=("https://e.co.nz/p", html)),              patch.object(scraper, "_quote_from_llm", return_value=fake) as llm_mock:
            out = scraper.fetch_quote_on_host_llm("https://e.co.nz/p")
        self.assertEqual(out["price"], "59.00")
        llm_mock.assert_called_once()

    def test_fetch_failure_is_not_retried_through_llm(self):
        import scraper
        with patch.object(scraper, "fetch_html", side_effect=scraper.ScraperError("403")):
            with patch.object(scraper, "_quote_from_llm") as llm_mock:
                with self.assertRaises(scraper.ScraperError):
                    scraper.fetch_quote_on_host_llm("https://e.co.nz/p")
        llm_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

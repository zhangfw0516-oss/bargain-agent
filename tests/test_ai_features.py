"""Tests for AI feature additions: page-attribute extraction and deal-email summary.

Both features stay offline: every LLM call goes through a fake OpenAI-compatible
client, and every SMTP call is mocked. The attribute extractor is a superset of the
price extractor, so these tests focus on the *new* optional fields and on the rule
that the email summary is opt-in with a deterministic fallback.
"""

import json
import unittest
from unittest.mock import patch

import agent
import cli
import notifier
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


def _extraction(product_name="Lumiere Serum 30ml", price="59.00", currency="NZD",
                in_stock=True, brand="Lumiere", colour=None, capacity="30ml"):
    return {
        "product_name": product_name, "price": price, "currency": currency,
        "in_stock": in_stock, "brand": brand, "colour": colour, "capacity": capacity,
    }


class TestValidatePriceExtractionAttributes(unittest.TestCase):
    def test_optional_attributes_are_normalised(self):
        out = schemas.validate_price_extraction(_extraction(brand=" Lumiere ", colour="Obsidian", capacity="30ml"))
        self.assertEqual(out["brand"], "Lumiere")
        self.assertEqual(out["colour"], "Obsidian")
        self.assertEqual(out["capacity"], "30ml")

    def test_missing_optional_attributes_default_to_none(self):
        # A legacy 4-field response is still accepted.
        data = {"product_name": "X", "price": "10.00", "currency": "NZD", "in_stock": True}
        out = schemas.validate_price_extraction(data)
        self.assertIsNone(out["brand"])
        self.assertIsNone(out["colour"])
        self.assertIsNone(out["capacity"])

    def test_blank_optional_attribute_fails(self):
        with self.assertRaises(ValueError):
            schemas.validate_price_extraction(_extraction(brand="   "))

    def test_unknown_field_fails_closed(self):
        data = _extraction()
        data["evil"] = "x"
        with self.assertRaises(ValueError):
            schemas.validate_price_extraction(data)


class TestQuoteFromLlmCarriesAttributes(unittest.TestCase):
    def test_attributes_reach_the_quote(self):
        import scraper
        html = b"<html><body>Serum 30ml Black $59 NZD</body></html>"
        extracted = _extraction(product_name="Serum", colour="Black", capacity="30ml")
        with patch.object(agent, "extract_price_from_page_text", return_value=extracted):
            quote = scraper._quote_from_llm(html, "https://e.co.nz/p")
        self.assertEqual(quote["brand"], "Lumiere")
        self.assertEqual(quote["variant"], "Black 30ml")

    def test_variant_is_none_without_attributes(self):
        import scraper
        html = b"<html><body>Serum $59 NZD</body></html>"
        extracted = _extraction(product_name="Serum", brand=None, colour=None, capacity=None)
        with patch.object(agent, "extract_price_from_page_text", return_value=extracted):
            quote = scraper._quote_from_llm(html, "https://e.co.nz/p")
        self.assertIsNone(quote["variant"])
        self.assertIsNone(quote["brand"])


class TestSummariseDeal(unittest.TestCase):
    def test_summary_round_trip(self):
        client = FakeClient([FakeResponse("Kindle Paperwhite is $299.00 at PB Tech, below your $300 target.")])
        task = {"product_name": "Kindle", "target_price": "300.00", "currency": "NZD", "comparison": "lt"}
        quote = {"product_name": "Kindle Paperwhite", "price": "299.00", "currency": "NZD",
                 "retailer": "PB Tech", "in_stock": True, "other_sources": []}
        out = agent.summarise_deal(task, quote, client=client, model="test")
        self.assertIn("PB Tech", out)
        self.assertIn("DATA", client.calls[0]["messages"][0]["content"])

    def test_summary_bounded_to_2000_chars(self):
        client = FakeClient([FakeResponse("x" * 5000)])
        out = agent.summarise_deal({}, {}, client=client, model="test")
        self.assertLessEqual(len(out), 2000)

    def test_summary_client_error_raises_parser_error(self):
        client = FakeClient([RuntimeError("boom")])
        with self.assertRaises(agent.ParserError):
            agent.summarise_deal({}, {}, client=client, model="test")


class TestNotifyDealLlmSummary(unittest.TestCase):
    def setUp(self):
        self.config_patch = patch.multiple(
            notifier.config,
            smtp_host=lambda: "smtp.example.com",
            smtp_port=lambda: 465,
            smtp_user=lambda: "user@example.com",
            smtp_password=lambda: "secret",
            smtp_from=lambda: "user@example.com",
            smtp_timeout=lambda: 5.0,
            notification_max_retries=lambda: 2,
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()

    @patch("notifier.smtplib.SMTP_SSL")
    @patch("notifier.send_email_notification", return_value={"success": True, "attempts": 1, "error": None, "retryable": False})
    def test_off_by_default_uses_deterministic_body(self, mock_send, _ssl):
        with patch.object(notifier.config, "llm_email_summary", return_value=False):
            notifier.notify_deal("to@example.com",
                                 {"product_name": "K", "target_price": "10", "currency": "NZD", "comparison": "lt"},
                                 {"product_name": "K", "price": "9", "currency": "NZD", "retailer": "R",
                                  "url": "u", "in_stock": True, "fetched_at": "t"})
        body = mock_send.call_args.args[2]
        self.assertIn("A price match was found", body)

    @patch("notifier.smtplib.SMTP_SSL")
    @patch("notifier.send_email_notification", return_value={"success": True, "attempts": 1, "error": None, "retryable": False})
    def test_enabled_with_llm_success_uses_summary(self, mock_send, _ssl):
        with patch.object(notifier.config, "llm_email_summary", return_value=True), \
             patch.object(notifier, "_llm_deal_body", return_value="LLM summary here"):
            notifier.notify_deal("to@example.com",
                                 {"product_name": "K", "target_price": "10", "currency": "NZD", "comparison": "lt"},
                                 {"product_name": "K", "price": "9", "currency": "NZD", "retailer": "R",
                                  "url": "u", "in_stock": True, "fetched_at": "t"})
        body = mock_send.call_args.args[2]
        self.assertIn("LLM summary here", body)

    @patch("notifier.smtplib.SMTP_SSL")
    @patch("notifier.send_email_notification", return_value={"success": True, "attempts": 1, "error": None, "retryable": False})
    def test_enabled_with_llm_failure_falls_back(self, mock_send, _ssl):
        with patch.object(notifier.config, "llm_email_summary", return_value=True), \
             patch.object(notifier, "_llm_deal_body", return_value=None):
            notifier.notify_deal("to@example.com",
                                 {"product_name": "K", "target_price": "10", "currency": "NZD", "comparison": "lt"},
                                 {"product_name": "K", "price": "9", "currency": "NZD", "retailer": "R",
                                  "url": "u", "in_stock": True, "fetched_at": "t"})
        body = mock_send.call_args.args[2]
        self.assertIn("A price match was found", body)


if __name__ == "__main__":
    unittest.main()


class TestExplainResult(unittest.TestCase):
    def test_explain_result_round_trip(self):
        client = FakeClient([FakeResponse("The item could not be confirmed as exactly what you asked for because its size was not stated.")])
        out = agent.explain_result("needs_confirmation", ["size not stated"], client=client, model="test")
        self.assertIn("not stated", out)
        self.assertIn("DATA", client.calls[0]["messages"][0]["content"])

    def test_explain_result_rejects_other_kinds(self):
        with self.assertRaises(agent.ParserError):
            agent.explain_result("same", [], client=FakeClient([]), model="test")

    def test_explain_result_bounded_to_300_chars(self):
        client = FakeClient([FakeResponse("x" * 5000)])
        out = agent.explain_result("unavailable", ["denied"], client=client, model="test")
        self.assertLessEqual(len(out), 300)

    def test_explain_result_client_error_raises(self):
        client = FakeClient([RuntimeError("boom")])
        with self.assertRaises(agent.ParserError):
            agent.explain_result("unavailable", ["denied"], client=client, model="test")


class TestLlmExplainCliGate(unittest.TestCase):
    def test_llm_explain_disabled_returns_empty_without_call(self):
        with patch.object(cli.config, "llm_explain_results", return_value=False), \
             patch.object(cli.agent, "explain_result") as mock_explain:
            self.assertEqual(cli._llm_explain("needs_confirmation", ["size"]), "")
            mock_explain.assert_not_called()

    def test_llm_explain_failure_returns_empty(self):
        with patch.object(cli.config, "llm_explain_results", return_value=True), \
             patch.object(cli.agent, "explain_result", side_effect=agent.ParserError("x")):
            self.assertEqual(cli._llm_explain("unavailable", ["denied"]), "")

    def test_llm_explain_success_returns_text(self):
        with patch.object(cli.config, "llm_explain_results", return_value=True), \
             patch.object(cli.agent, "explain_result", return_value="a friendly reason"):
            self.assertEqual(cli._llm_explain("needs_confirmation", ["size"]), "a friendly reason")


class TestMatchProductAspect(unittest.TestCase):
    def test_colour_same(self):
        client = FakeClient([FakeResponse(json.dumps({"verdict": "same", "reason": "Obsidian is a marketing name for black."}))])
        verdict, reason = agent.match_product_aspect("colour", "black", "Fitbit Air - Obsidian", "Fitbit Air", client=client, model="test")
        self.assertEqual(verdict, "same")
        self.assertIn("Obsidian", reason)

    def test_colour_different(self):
        client = FakeClient([FakeResponse(json.dumps({"verdict": "different", "reason": "Lavender is a shade of purple, not black."}))])
        verdict, reason = agent.match_product_aspect("colour", "black", "Fitbit Air - Lavender", "Fitbit Air", client=client, model="test")
        self.assertEqual(verdict, "different")

    def test_accessory_same_when_user_asked_for_it(self):
        client = FakeClient([FakeResponse(json.dumps({"verdict": "same", "reason": "The user asked for a band, so this is the requested product."}))])
        verdict, reason = agent.match_product_aspect("accessory", None, "Fitbit Air Active Band", "Fitbit Air band", client=client, model="test")
        self.assertEqual(verdict, "same")

    def test_accessory_different(self):
        client = FakeClient([FakeResponse(json.dumps({"verdict": "different", "reason": "This is a protective case, not the Fitbit Air itself."}))])
        verdict, reason = agent.match_product_aspect("accessory", None, "Fitbit Air Case", "Fitbit Air", client=client, model="test")
        self.assertEqual(verdict, "different")

    def test_invalid_aspect_rejected(self):
        with self.assertRaises(agent.ParserError):
            agent.match_product_aspect("brand", None, "X", "X", client=FakeClient([]), model="test")

    def test_invalid_json_rejected(self):
        client = FakeClient([FakeResponse("not json")])
        with self.assertRaises(agent.ParserError):
            agent.match_product_aspect("colour", "black", "X", "X", client=client, model="test")

    def test_client_error_raises(self):
        client = FakeClient([RuntimeError("boom")])
        with self.assertRaises(agent.ParserError):
            agent.match_product_aspect("colour", "black", "X", "X", client=client, model="test")


class TestLlmMatchAssistGate(unittest.TestCase):
    def test_disabled_by_default(self):
        import config
        self.assertFalse(config.llm_match_assist())

    def test_discovery_accessory_fallback(self):
        # When the hardcoded keyword list finds nothing and the feature is off,
        # the deterministic result is kept (no LLM call).
        import discovery
        candidate = {"title": "Fitbit Air Smart Watch", "url": "https://e.co.nz/p"}
        request = {"product_name": "Fitbit Air", "query": "Fitbit Air"}
        status, reasons = discovery.match_candidate(request, candidate)
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_discovery_colour_fallback_deterministic(self):
        # Deterministic colour extraction still works when LLM assist is off.
        import discovery
        candidate = {"title": "Fitbit Air - Obsidian", "url": "https://e.co.nz/p"}
        request = {"product_name": "Fitbit Air", "query": "Fitbit Air", "colour": "black"}
        status, reasons = discovery.match_candidate(request, candidate)
        self.assertEqual(status, discovery.MATCH_SAME)


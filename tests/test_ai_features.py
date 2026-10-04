"""Price-extraction attributes and the opt-in deal-email summary.

Both features stay offline: every LLM call goes through a fake OpenAI-compatible
client, and every SMTP call is mocked. The attribute extractor is a superset of the
price extractor, so these tests focus on the *new* optional fields and on the rule
that the email summary is opt-in with a deterministic fallback.
"""

import json
import unittest
from unittest.mock import patch

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

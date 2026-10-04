"""Tests for schemas.validate_task and schemas.validate_quote."""

import unittest

from schemas import validate_quote, validate_search_request, validate_task


class TestValidateQuote(unittest.TestCase):
    def _quote(self, **overrides) -> dict:
        base = {
            "product_name": "Kindle",
            "url": "https://www.pbtech.co.nz/kindle",
            "retailer": "pbtech.co.nz",
            "price": "299.00",
            "currency": "NZD",
            "in_stock": True,
            "fetched_at": "2026-09-23T04:00:00+00:00",
        }
        base.update(overrides)
        return base

    def test_valid_quote(self):
        quote = validate_quote(self._quote())
        self.assertEqual(quote["price"], "299.00")
        self.assertEqual(quote["currency"], "NZD")

    def test_zero_price_rejected(self):
        with self.assertRaises(ValueError):
            validate_quote(self._quote(price="0.00"))

    def test_negative_price_rejected(self):
        with self.assertRaises(ValueError):
            validate_quote(self._quote(price="-5.00"))

    def test_non_finite_price_rejected(self):
        with self.assertRaises(ValueError):
            validate_quote(self._quote(price="Infinity"))

    def test_unsupported_currency_rejected(self):
        with self.assertRaises(ValueError):
            validate_quote(self._quote(currency="EUR"))

    def test_missing_fetched_at_rejected(self):
        with self.assertRaises(ValueError):
            validate_quote(self._quote(fetched_at=None))

    def test_invalid_in_stock_rejected(self):
        with self.assertRaises(ValueError):
            validate_quote(self._quote(in_stock="yes"))


class TestValidateTaskUrlExtraction(unittest.TestCase):
    def _task(self, url: str) -> dict:
        return {
            "schema_version": "1",
            "status": "ready",
            "product_name": "Kindle",
            "url": url,
            "target_price": "300.00",
            "currency": "NZD",
            "comparison": "lt",
            "frequency_minutes": 60,
            "condition": None,
            "missing_fields": [],
            "clarification_question": None,
        }

    # R10: CJK punctuation directly after a URL must not break extraction.
    def test_chinese_punctuation_after_url(self):
        instruction = "监控 Kindle（https://www.pbtech.co.nz/product/Kindle），价格低于 300 新西兰元"
        task = validate_task(self._task("https://www.pbtech.co.nz/product/Kindle"), instruction)
        self.assertEqual(task["url"], "https://www.pbtech.co.nz/product/Kindle")

    def test_english_comma_after_url(self):
        instruction = "Monitor https://www.pbtech.co.nz/x, notify me when below NZD 300"
        task = validate_task(self._task("https://www.pbtech.co.nz/x"), instruction)
        self.assertEqual(task["url"], "https://www.pbtech.co.nz/x")

    def test_url_not_in_instruction_rejected(self):
        with self.assertRaises(ValueError):
            validate_task(self._task("https://www.pbtech.co.nz/other"), "Monitor https://www.pbtech.co.nz/x below NZD 300")

    def test_missing_frequency_becomes_clarification(self):
        task = self._task("https://www.pbtech.co.nz/x")
        task["frequency_minutes"] = None
        result = validate_task(task, "Monitor https://www.pbtech.co.nz/x below NZD 300")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("frequency_minutes", result["missing_fields"])

    def test_null_currency_becomes_clarification(self):
        task = self._task("https://www.pbtech.co.nz/x")
        task["currency"] = None
        result = validate_task(task, "Monitor https://www.pbtech.co.nz/x for $300")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("currency", result["missing_fields"])


class TestValidateSearchRequest(unittest.TestCase):
    """Schema v2: a search request is a different shape from a monitor task."""

    def _request(self, **overrides) -> dict:
        base = {
            "schema_version": "2",
            "status": "ready",
            "product_name": "Fitbit Air",
            "url": None,
            "budget": "200.00",
            "currency": "NZD",
            "colour": None,
            "capacity": None,
            "condition": None,
            "comparison": "lte",
            "frequency_minutes": 60,
            "missing_fields": [],
            "clarification_question": None,
        }
        base.update(overrides)
        return base

    def test_ready_search_does_not_require_a_url(self):
        result = validate_search_request(self._request(), "找 Fitbit Air 预算 NZD200，每小时看一次")
        self.assertEqual(result["status"], "ready")
        self.assertIsNone(result["url"])
        self.assertEqual(result["budget"], "200.00")

    # Frequency became optional: an unstated cadence is no longer a reason to ask.
    def test_missing_frequency_defaults_to_daily(self):
        result = validate_search_request(
            self._request(frequency_minutes=None, missing_fields=[]),
            "找 Fitbit Air 预算 NZD200",
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["frequency_minutes"], 1440)
        self.assertEqual(result["missing_fields"], [])
        self.assertIsNone(result["clarification_question"])

    def test_stale_frequency_missing_field_is_dropped(self):
        # A model (or an older parser) that still reports frequency_minutes as missing
        # must not turn an otherwise ready search into a clarification.
        result = validate_search_request(
            self._request(frequency_minutes=None, missing_fields=["frequency_minutes"]),
            "找 Fitbit Air 预算 NZD200",
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["frequency_minutes"], 1440)
        self.assertNotIn("frequency_minutes", result["missing_fields"])

    def test_explicit_frequency_is_preserved(self):
        result = validate_search_request(
            self._request(frequency_minutes=60), "找 Fitbit Air 预算 NZD200，每小时看一次"
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["frequency_minutes"], 60)

    def test_frequency_below_range_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_search_request(
                self._request(frequency_minutes=3), "找 Fitbit Air，每 3 分钟看一次"
            )

    def test_frequency_above_range_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_search_request(
                self._request(frequency_minutes=525601), "找 Fitbit Air"
            )

    def test_budget_without_currency_asks_for_the_currency(self):
        result = validate_search_request(
            self._request(currency=None), "找 Fitbit Air 预算 200，每小时看一次"
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("currency", result["missing_fields"])

    def test_invented_colour_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_search_request(
                self._request(colour="black"), "找 Fitbit Air，每小时看一次"
            )

    def test_stated_colour_is_accepted(self):
        result = validate_search_request(
            self._request(colour="黑色"), "找 Fitbit Air 黑色，每小时看一次"
        )
        self.assertEqual(result["colour"], "黑色")

    def test_invented_url_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_search_request(
                self._request(url="https://www.pbtech.co.nz/product/X/Y"),
                "找 Fitbit Air，每小时看一次",
            )

    def test_unsupported_needs_an_explanation(self):
        with self.assertRaises(ValueError):
            validate_search_request(
                self._request(status="unsupported", clarification_question=None), "今天天气"
            )
        result = validate_search_request(
            self._request(status="unsupported", clarification_question="与购物无关"), "今天天气"
        )
        self.assertEqual(result["status"], "unsupported")

    def test_extra_fields_are_rejected(self):
        payload = self._request()
        payload["target_price"] = "1.00"
        with self.assertRaises(ValueError):
            validate_search_request(payload, "找 Fitbit Air，每小时看一次")

    def test_frequency_range_is_enforced(self):
        with self.assertRaises(ValueError):
            validate_search_request(self._request(frequency_minutes=1), "找 Fitbit Air，每分钟看一次")

    def test_negative_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_search_request(self._request(budget="-5.00"), "找 Fitbit Air 预算 -5")


if __name__ == "__main__":
    unittest.main()

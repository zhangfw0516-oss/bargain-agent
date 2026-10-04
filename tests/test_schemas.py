"""Tests for schemas.validate_task."""

import unittest

from schemas import validate_task


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


if __name__ == "__main__":
    unittest.main()

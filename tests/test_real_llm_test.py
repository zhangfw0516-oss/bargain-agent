"""Offline tests for the real-LLM evaluation logic (no API access)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import real_llm_test


def _actual(**overrides) -> dict:
    base = {
        "status": "ready",
        "product_name": "Kindle",
        "url": "https://www.pbtech.co.nz/product/Kindle",
        "target_price": "300.00",
        "currency": "NZD",
        "comparison": "lt",
        "frequency_minutes": 60,
        "missing_fields": [],
        "clarification_question": None,
    }
    base.update(overrides)
    return base


class TestEvaluateCase(unittest.TestCase):
    def _case(self, **expect_overrides) -> dict:
        expect = {
            "status": "ready",
            "url": "https://www.pbtech.co.nz/product/Kindle",
            "target_price": "300.00",
            "currency": "NZD",
            "comparison": "lt",
            "frequency_minutes": 60,
            "missing_fields": [],
        }
        expect.update(expect_overrides)
        return {"name": "case", "instruction": "x", "expect": expect}

    def test_matching_case_passes(self):
        self.assertEqual(real_llm_test.evaluate_case(self._case(), _actual()), [])

    def test_wrong_amount_fails(self):
        reasons = real_llm_test.evaluate_case(self._case(), _actual(target_price="30.00"))
        self.assertTrue(any("target_price" in r for r in reasons))

    def test_wrong_url_fails(self):
        reasons = real_llm_test.evaluate_case(self._case(), _actual(url="https://www.pbtech.co.nz/product/Other"))
        self.assertTrue(any("url" in r for r in reasons))

    def test_wrong_frequency_fails(self):
        reasons = real_llm_test.evaluate_case(self._case(), _actual(frequency_minutes=5))
        self.assertTrue(any("frequency_minutes" in r for r in reasons))

    def test_wrong_comparison_fails(self):
        reasons = real_llm_test.evaluate_case(self._case(), _actual(comparison="lte"))
        self.assertTrue(any("comparison" in r for r in reasons))

    def test_wrong_currency_fails(self):
        reasons = real_llm_test.evaluate_case(self._case(), _actual(currency="AUD"))
        self.assertTrue(any("currency" in r for r in reasons))

    def test_error_actual_fails(self):
        self.assertTrue(real_llm_test.evaluate_case(self._case(), None))

    def test_unexpected_clarification_fails(self):
        case = self._case(status="needs_clarification", currency=None)
        case["only_missing"] = ["currency"]
        actual = _actual(status="needs_clarification", currency=None, missing_fields=["currency", "product_name"])
        reasons = real_llm_test.evaluate_case(case, actual)
        self.assertTrue(any("unexpected clarification" in r for r in reasons))

    def test_unrelated_clarification_fails(self):
        # Actual asks about product_name while only currency was allowed.
        case = self._case(status="needs_clarification", currency=None)
        case["only_missing"] = ["currency"]
        actual = _actual(status="needs_clarification", currency=None, missing_fields=["product_name"])
        reasons = real_llm_test.evaluate_case(case, actual)
        self.assertTrue(any("unexpected clarification" in r for r in reasons))

    def test_allowed_clarification_passes(self):
        case = self._case(status="needs_clarification", currency=None)
        case["only_missing"] = ["currency"]
        actual = _actual(status="needs_clarification", currency=None, missing_fields=["currency"])
        self.assertEqual(real_llm_test.evaluate_case(case, actual), [])

    def test_accept_status_allows_listed_values(self):
        case = {"name": "c", "instruction": "x", "expect": {"comparison": "lte"}, "accept_status": ["ready", "needs_clarification"]}
        self.assertEqual(real_llm_test.evaluate_case(case, _actual(status="needs_clarification", comparison="lte")), [])


class TestRunCases(unittest.TestCase):
    def test_all_errors_are_failures_not_successes(self):
        def boom(instruction):
            raise RuntimeError("no network")

        _lines, passed, failed = real_llm_test.run_cases(real_llm_test.CASES, boom)
        self.assertEqual(passed, 0)
        self.assertEqual(failed, len(real_llm_test.CASES))

    def test_mismatching_results_are_failures(self):
        def wrong(instruction):
            return _actual(target_price="1.00")

        _lines, passed, failed = real_llm_test.run_cases(real_llm_test.CASES, wrong)
        self.assertEqual(passed, 0)
        self.assertEqual(failed, len(real_llm_test.CASES))


class TestMainExitCode(unittest.TestCase):
    def test_main_returns_nonzero_when_all_calls_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.txt"
            with patch.object(real_llm_test.agent, "parse_user_instruction", side_effect=RuntimeError("boom")):
                with patch.object(real_llm_test.config, "llm_model", return_value="test-model"):
                    rc = real_llm_test.main(["--output", str(out)])
            self.assertEqual(rc, 1)
            text = out.read_text(encoding="utf-8")
            self.assertIn("0 passed", text)

    def test_report_contains_metadata_and_no_secret(self):
        secret = "SUPER-SECRET-KEY-DO-NOT-LEAK"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.txt"
            with patch.object(real_llm_test.agent, "parse_user_instruction", side_effect=RuntimeError("boom")):
                with patch.object(real_llm_test.config, "llm_model", return_value="test-model"):
                    with patch.object(real_llm_test.config, "llm_api_key", return_value=secret):
                        real_llm_test.main(["--output", str(out)])
            text = out.read_text(encoding="utf-8")
            self.assertIn("Model: test-model", text)
            self.assertIn("Prompt file:", text)
            self.assertIn("API calls made:", text)
            self.assertIn("Generated (UTC):", text)
            self.assertNotIn(secret, text)

    def test_main_returns_zero_when_all_cases_match(self):
        def perfect(instruction):
            for case in real_llm_test.CASES:
                if case["instruction"] == instruction:
                    expect = case["expect"]
                    return {**{f: None for f in real_llm_test.REPORT_FIELDS}, **expect}
            raise AssertionError("unknown instruction")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.txt"
            with patch.object(real_llm_test.agent, "parse_user_instruction", side_effect=perfect):
                with patch.object(real_llm_test.config, "llm_model", return_value="test-model"):
                    rc = real_llm_test.main(["--output", str(out)])
            self.assertEqual(rc, 0)
            self.assertIn("9 passed", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

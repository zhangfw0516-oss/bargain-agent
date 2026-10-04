"""Tests for configuration validation."""

import unittest
from unittest.mock import patch

import config
import pricing


class TestConfigValidation(unittest.TestCase):
    def _env(self, **values):
        return patch.dict("os.environ", values, clear=False)

    def test_timeouts_reject_non_positive_and_non_finite(self):
        for raw in ("0", "-1", "nan", "inf", "abc"):
            with self._env(SERPAPI_TIMEOUT=raw), self.assertRaises(config.ConfigError):
                config.serpapi_timeout()
        with self._env(SERPAPI_TIMEOUT="12.5"):
            self.assertEqual(config.serpapi_timeout(), 12.5)

    def test_retries_range(self):
        for raw in ("-1", "100"):
            with self._env(NOTIFICATION_MAX_RETRIES=raw), self.assertRaises(config.ConfigError):
                config.notification_max_retries()

    def test_integer_limits_are_range_checked(self):
        with self._env(FIND_MAX_QUERIES="0"), self.assertRaises(config.ConfigError):
            config.find_max_queries()
        with self._env(MONITOR_MIN_FREQUENCY_MINUTES="2"), self.assertRaises(config.ConfigError):
            config.monitor_min_frequency_minutes()
        with self._env(SERPAPI_DAILY_MAX_CALLS="x"), self.assertRaises(config.ConfigError):
            config.serpapi_daily_max_calls()
        with self._env(FIND_MAX_QUERIES="", MONITOR_MIN_FREQUENCY_MINUTES=""):
            self.assertEqual((config.find_max_queries(), config.monitor_min_frequency_minutes()), (3, 360))

    def test_page_extractor_chain(self):
        with self._env(PAGE_EXTRACTORS="Firecrawl, tavily, firecrawl"):
            self.assertEqual(config.page_extractors(), ["firecrawl", "tavily"])
        with self._env(PAGE_EXTRACTORS="scrapingbee"), self.assertRaises(config.ConfigError):
            config.page_extractors()

    def test_locale_defaults_to_new_zealand(self):
        with self._env(SERPAPI_GL="", SERPAPI_HL="", SERPAPI_GOOGLE_DOMAIN="", SERPAPI_LOCATION=""):
            self.assertEqual(config.serpapi_locale()["gl"], "nz")
            self.assertEqual(config.serpapi_locale()["google_domain"], "google.co.nz")

    def test_booleans(self):
        with self._env(ALERT_TRUSTED_ONLY="false", LLM_OFFER_VERIFY="0"):
            self.assertFalse(config.alert_trusted_only())
            self.assertFalse(config.llm_offer_verify())
        with self._env(ALERT_TRUSTED_ONLY="", LLM_OFFER_VERIFY=""):
            self.assertTrue(config.alert_trusted_only())
            self.assertTrue(config.llm_offer_verify())


class TestMerchantTrust(unittest.TestCase):
    def test_names_and_domains(self):
        trusted = ["pbtech.co.nz", "JB Hi-Fi"]
        self.assertTrue(pricing.merchant_trust("PB Tech", "https://www.pbtech.co.nz/p/1", trusted))
        self.assertTrue(pricing.merchant_trust("jb hi-fi", None, trusted))
        self.assertTrue(pricing.merchant_trust("Anything", "https://shop.pbtech.co.nz/x", trusted))
        self.assertFalse(pricing.merchant_trust("PB Tech Deals", "https://pbtech.co.nz.evil.com/x", trusted))
        self.assertFalse(pricing.merchant_trust(None, None, trusted))

    def test_custom_list_from_env(self):
        with patch.dict("os.environ", {"TRUSTED_MERCHANTS": "Small Shop"}):
            self.assertTrue(pricing.merchant_trust("Small Shop"))
            self.assertFalse(pricing.merchant_trust("Noel Leeming"))


class TestSafeUrl(unittest.TestCase):
    def test_public_urls_only(self):
        self.assertTrue(pricing.is_safe_public_url("https://shop.example.nz/p?id=1"))
        for url in ("http://localhost/x", "http://127.0.0.1/x", "http://10.0.0.5/x", "https://a:b@shop.nz/x",
                    "https://shop.nz:8443/x", "file:///etc/passwd", "javascript:alert(1)", "https://printer.local/x",
                    "http://[::1]/x", "not a url"):
            self.assertFalse(pricing.is_safe_public_url(url), url)


if __name__ == "__main__":
    unittest.main()

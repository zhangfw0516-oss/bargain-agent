"""Tests for config validation of numeric and domain settings."""

import unittest
from unittest.mock import patch

import config


class TestConfigValidation(unittest.TestCase):
    def _patch_env(self, **values):
        return patch.dict("os.environ", values, clear=False)

    # R11: reject non-positive and non-finite timeouts.
    def test_timeout_rejects_zero(self):
        with self._patch_env(SCRAPER_TIMEOUT="0"):
            with self.assertRaises(config.ConfigError):
                config.scraper_timeout()

    def test_timeout_rejects_negative(self):
        with self._patch_env(SCRAPER_TIMEOUT="-1"):
            with self.assertRaises(config.ConfigError):
                config.scraper_timeout()

    def test_timeout_rejects_nan(self):
        with self._patch_env(SCRAPER_TIMEOUT="nan"):
            with self.assertRaises(config.ConfigError):
                config.scraper_timeout()

    def test_timeout_rejects_infinity(self):
        with self._patch_env(SCRAPER_TIMEOUT="inf"):
            with self.assertRaises(config.ConfigError):
                config.scraper_timeout()

    def test_timeout_accepts_positive(self):
        with self._patch_env(SCRAPER_TIMEOUT="12.5"):
            self.assertEqual(config.scraper_timeout(), 12.5)

    # R11: reject unreasonable response size limits.
    def test_response_bytes_rejects_zero(self):
        with self._patch_env(SCRAPER_MAX_RESPONSE_BYTES="0"):
            with self.assertRaises(config.ConfigError):
                config.scraper_max_response_bytes()

    def test_response_bytes_rejects_huge(self):
        with self._patch_env(SCRAPER_MAX_RESPONSE_BYTES="999999999999"):
            with self.assertRaises(config.ConfigError):
                config.scraper_max_response_bytes()

    # R11: reject out-of-range retry budgets.
    def test_retries_rejects_negative(self):
        with self._patch_env(NOTIFICATION_MAX_RETRIES="-1"):
            with self.assertRaises(config.ConfigError):
                config.notification_max_retries()

    def test_retries_rejects_huge(self):
        with self._patch_env(NOTIFICATION_MAX_RETRIES="100"):
            with self.assertRaises(config.ConfigError):
                config.notification_max_retries()

    # R8: domain normalisation must not strip arbitrary leading characters.
    def test_domain_normalisation(self):
        with self._patch_env(SCRAPER_ALLOWED_DOMAINS="www.pbtech.co.nz"):
            self.assertEqual(config.allowed_scraper_domains(), ["pbtech.co.nz"])

    def test_domain_normalisation_keeps_w_letters(self):
        # A domain that legitimately starts with 'w' must not be mangled.
        with self._patch_env(SCRAPER_ALLOWED_DOMAINS="walmart.com"):
            self.assertEqual(config.allowed_scraper_domains(), ["walmart.com"])


if __name__ == "__main__":
    unittest.main()

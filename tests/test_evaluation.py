"""Targeted, offline tests for the Stage E evaluation assets.

These tests do NOT re-run the whole project suite. They assert that the evaluation
runner is deterministic, that every metric has a denominator, that the identity track
reports same precision/recall and wrong-item false accepts, that the quote track checks
amount + currency + stock together, and that the runner never reads ``.env`` and never
touches a real outbound boundary (Brave / merchant fetch / SMTP / DeepSeek).

The whole module runs offline: the runner injects fake fetchers and senders and calls
only local deterministic code.
"""

import json
import unittest
from unittest.mock import patch

from evaluation import datasets, runner


class TestEvaluationRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = runner.run_all()

    def test_all_cases_pass(self):
        for name, track in self.report["tracks"].items():
            self.assertEqual([], track["failures"], f"{name} failures: {track['failures']}")

    def test_every_track_has_an_explicit_denominator(self):
        self.assertTrue(self.report["tracks"])
        for name, track in self.report["tracks"].items():
            self.assertGreater(track["n_cases"], 0, name)
            self.assertEqual(track["n_cases"], len(track["cases"]), name)
            self.assertTrue(
                any("accuracy" in key or key.endswith("_rate") for key in track["metrics"]),
                f"{name} has no accuracy/rate metric",
            )
            # A rate whose denominator is zero must surface as None, never as a
            # silent 1.00. Fail loudly here if any rate is undefined.
            for key, value in track["metrics"].items():
                if (
                    "accuracy" in key
                    or key.endswith("_rate")
                    or key.endswith("_precision")
                    or key.endswith("_recall")
                ):
                    self.assertIsNotNone(value, f"{name}.{key} is None (zero denominator)")
                    self.assertGreaterEqual(value, 0.0, f"{name}.{key}")
                    self.assertLessEqual(value, 1.0, f"{name}.{key}")
            for case in track["cases"]:
                self.assertIn("expected", case)
                self.assertIn("actual", case)
                self.assertIn("passed", case)

    def test_provenance_distinguishes_synthetic_from_real_fixtures(self):
        provenances = set()
        for track in self.report["tracks"].values():
            provenances.update(track["provenance"].keys())
        # Synthetic cases prove the rule; the other two tag real-derived evidence.
        self.assertIn(datasets.PROVENANCE_SYNTHETIC, provenances)
        self.assertIn(datasets.PROVENANCE_EXISTING_FIXTURE, provenances)
        self.assertIn(datasets.PROVENANCE_REAL_TRACE, provenances)

    def test_identity_reports_same_precision_recall_and_false_accepts(self):
        metrics = self.report["tracks"]["identity"]["metrics"]
        for key in (
            "n_expected_same", "same_true_positives", "same_false_positives",
            "same_false_negatives", "same_precision", "same_recall",
            "wrong_item_false_accepts", "unverifiable_false_accepts",
        ):
            self.assertIn(key, metrics)
        self.assertGreater(metrics["n_expected_same"], 0)
        self.assertEqual(0, metrics["wrong_item_false_accepts"])
        self.assertEqual(1.0, metrics["same_precision"])
        self.assertEqual(1.0, metrics["same_recall"])
        # All three verdict classes are exercised across all three decision levels.
        self.assertEqual({"candidate": 15, "detail": 7, "monitor": 8}, metrics["checked_by_level"])

    def test_quote_track_verifies_amount_currency_and_stock_together(self):
        metrics = self.report["tracks"]["quote_extraction"]["metrics"]
        n = metrics["n_extraction_cases"]
        self.assertGreater(n, 0)
        self.assertEqual(n, metrics["triple_exact"])
        self.assertEqual(n, metrics["price_field_ok"])
        self.assertEqual(n, metrics["currency_field_ok"])
        self.assertEqual(n, metrics["stock_field_ok"])
        self.assertGreater(metrics["n_refusal_cases"], 0)
        self.assertEqual(metrics["n_refusal_cases"], metrics["refused_correct"])

    def test_notification_dedup_sends_one_email_per_deal_window(self):
        track = self.report["tracks"]["notification_dedup"]
        metrics = track["metrics"]
        self.assertEqual(metrics["n_dedup_scenarios"], metrics["dedup_correct"])
        by_id = {c["id"]: c for c in track["cases"]}
        self.assertEqual(1, by_id["notif_both_deal_one_email"]["actual"]["send_calls"])
        self.assertEqual(1, by_id["notif_equal_price_one_email"]["actual"]["send_calls"])
        # The second identical check must not notify again.
        self.assertTrue(by_id["notif_both_deal_one_email"]["actual"]["check2_is_deal"])
        self.assertFalse(by_id["notif_both_deal_one_email"]["actual"]["check2_notified"])
        self.assertEqual(10, metrics["total_fake_emails_sent"])

    def test_a_failing_source_is_isolated(self):
        track = self.report["tracks"]["notification_dedup"]
        by_id = {c["id"]: c for c in track["cases"]}
        observed = by_id["notif_source_failure_isolated"]["actual"]
        self.assertTrue(observed["is_deal"])
        self.assertEqual(1, observed["sources_failed"])
        self.assertEqual(2, observed["sources_checked"])
        self.assertEqual(1, observed["send_calls"])
        # A failure must not be read as "the deal ended".
        self.assertFalse(by_id["notif_failure_keeps_episode_open"]["actual"]["event_reset"])
        self.assertTrue(by_id["notif_failure_keeps_episode_open"]["actual"]["event_reset_skipped"])
        # The isolation denominator must come from an explicit tag, not from whatever
        # keys a scenario happens to expect. Every tagged scenario injects a failure.
        isolation_ids = {
            s["id"] for s in datasets.NOTIFICATION_SCENARIOS
            if "failure_isolation" in s.get("tags", [])
        }
        self.assertEqual(
            {
                "notif_source_failure_isolated",
                "notif_failure_keeps_episode_open",
                "notif_send_retry_then_success",
                "notif_permanent_send_failure",
            },
            isolation_ids,
        )
        self.assertEqual(len(isolation_ids), track["metrics"]["n_isolation_scenarios"])
        self.assertEqual(
            track["metrics"]["n_isolation_scenarios"], track["metrics"]["isolation_correct"]
        )

    def test_runner_is_deterministic(self):
        second = runner.run_all()
        self.assertEqual(
            json.dumps(self.report, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False),
        )

    def test_runner_never_reads_config_or_env(self):
        import config

        with patch.object(config, "_load", side_effect=AssertionError("runner read .env")), \
             patch.object(config, "_get", side_effect=AssertionError("runner read config")):
            report = runner.run_all()
        self.assertTrue(report["offline"])
        self.assertGreater(sum(t["n_cases"] for t in report["tracks"].values()), 0)

    def test_runner_touches_no_real_outbound_boundary(self):
        import smtplib

        import requests
        import search_providers.brave as brave

        def boom(*_args, **_kwargs):
            raise AssertionError("evaluation touched a real outbound boundary")

        with patch.object(requests, "Session", boom), \
             patch.object(smtplib, "SMTP", boom), \
             patch.object(smtplib, "SMTP_SSL", boom), \
             patch.object(brave, "_default_sender", boom):
            report = runner.run_all()
        self.assertGreater(sum(t["n_cases"] for t in report["tracks"].values()), 0)

    def test_summary_formats_without_error(self):
        text = runner.format_summary(self.report)
        self.assertIn("instruction_schema", text)
        self.assertIn("identity", text)
        self.assertIn("quote_extraction", text)


if __name__ == "__main__":
    unittest.main()

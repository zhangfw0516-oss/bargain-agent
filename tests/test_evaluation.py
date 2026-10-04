"""Offline tests for the evaluation runner: every case passes, every rate has a
denominator, results are deterministic, and the replay mode never needs a model."""

import json
import unittest
from unittest.mock import patch

from evaluation import datasets, runner


class TestEvaluationRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = runner.run_all()

    def test_all_cases_pass_offline(self):
        for name, track in self.report["tracks"].items():
            self.assertEqual([], track["failures"], f"{name} failures")

    def test_tracks_cover_the_three_proposal_metrics(self):
        self.assertEqual(
            set(self.report["tracks"]),
            {"offer_price_parsing", "page_price_extraction", "instruction_parsing", "notification_trigger"},
        )

    def test_every_rate_has_a_denominator(self):
        for name, track in self.report["tracks"].items():
            self.assertGreater(track["n_cases"], 0, name)
            self.assertEqual(track["n_cases"], len(track["cases"]))
            for key, value in track["metrics"].items():
                if key.endswith(("accuracy", "_rate", "_precision", "_recall")):
                    self.assertIsNotNone(value, f"{name}.{key}")
                    self.assertTrue(0.0 <= value <= 1.0, f"{name}.{key}={value}")

    def test_headline_safety_numbers(self):
        tracks = self.report["tracks"]
        self.assertEqual(tracks["offer_price_parsing"]["metrics"]["false_price_rate"], 0.0)
        self.assertEqual(tracks["page_price_extraction"]["metrics"]["false_price_rate"], 0.0)
        self.assertEqual(tracks["notification_trigger"]["metrics"]["unexpected_send_rate"], 0.0)
        self.assertGreaterEqual(tracks["offer_price_parsing"]["metrics"]["merchants"], 3)

    def test_trusted_and_unverified_merchants_are_reported_separately(self):
        metrics = self.report["tracks"]["offer_price_parsing"]["metrics"]
        self.assertIsNotNone(metrics["trusted_accuracy"])
        self.assertIsNotNone(metrics["unverified_accuracy"])

    def test_cases_without_a_usable_price_exist(self):
        self.assertTrue(any(c["gold"] is None for c in datasets.PAGE_PRICE_CASES))
        self.assertTrue(any(c["gold"][0] is None for c in datasets.OFFER_PRICE_CASES))

    def test_deterministic(self):
        again = runner.run_all()
        self.assertEqual(json.dumps(self.report, sort_keys=True, default=str), json.dumps(again, sort_keys=True, default=str))

    def test_replay_mode_never_builds_a_real_client(self):
        with patch("llm.make_client", side_effect=AssertionError("real client built")):
            runner.run_page_price_track()
            runner.run_instruction_track()

    def test_summary_mentions_every_track(self):
        text = runner.format_summary(self.report)
        for name in self.report["tracks"]:
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()

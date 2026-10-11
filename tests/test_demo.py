"""Demo database: seeded history drives the chart and the 30-day insight."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pricing
import storage
import webapp
from demo import seed_demo

LISTINGS = {"products": {
    "Fitbit Charge 6": [
        {"title": "Fitbit Charge 6", "merchant": "PB Tech", "price": "198.00",
         "link": "https://www.pbtech.co.nz/product/x", "product_id": "111"},
        {"title": "Fitbit Charge 6", "merchant": "Harvey Norman", "price": "220.00",
         "link": "https://www.harveynorman.co.nz/x", "product_id": "222"},
    ],
    "Sony WH-1000XM5": [],
}}


class TestDemoSeed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "demo.db"
        patcher = patch.object(seed_demo, "DEMO_DB", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def test_seed_builds_history_ending_at_real_price(self):
        seed_demo.seed(LISTINGS)
        tasks = storage.list_tasks(db_path=self.db)
        self.assertEqual(len(tasks), 1)  # products without listings are skipped
        task = tasks[0]
        quotes = storage.list_quotes(task_id=task["id"], limit=100, db_path=self.db)
        self.assertEqual(len(quotes), 2 * (seed_demo.DAYS // seed_demo.EVERY_DAYS))
        latest = {q["retailer"]: q["price"] for q in quotes[:2]}
        self.assertEqual(latest, {"PB Tech": "198.00", "Harvey Norman": "220.00"})
        self.assertLess(float(task["target_price"]), 198)  # "Check now" must not send an email
        insight = pricing.price_insight(task, "198.00", db_path=self.db)
        self.assertIsNotNone(insight)
        self.assertEqual(insight["checks"], seed_demo.DAYS // seed_demo.EVERY_DAYS)

    def test_reset_gives_a_new_instance_id(self):
        # the page keys its saved Find results by this id, so a reset must change it
        seed_demo.seed(LISTINGS)
        first = webapp.api_tasks(webapp.AppState(db_path=self.db, demo=True))["instance"]
        self.assertEqual(first, storage.instance_id(self.db))  # stable while the file lives
        seed_demo.seed(LISTINGS)
        self.assertNotEqual(first, webapp.api_tasks(webapp.AppState(db_path=self.db, demo=True))["instance"])
        other = Path(self.dir.name) / "normal.db"
        storage.init_db(other)
        self.assertNotEqual(storage.instance_id(other), storage.instance_id(self.db))

    def test_demo_mode_creates_monitors_without_an_email(self):
        storage.init_db(self.db)
        with patch.dict("os.environ", {"DEFAULT_NOTIFY_EMAIL": ""}), \
                patch("config._get", side_effect=lambda name: "" if name == "DEFAULT_NOTIFY_EMAIL" else None):
            demo = webapp.AppState(db_path=self.db, demo=True)
            self.assertEqual(webapp._require_email(demo), webapp.DEMO_EMAIL)
            self.assertTrue(demo.email_warning().startswith("Demo mode"))
            with self.assertRaises(webapp.WebError) as ctx:
                webapp._require_email(webapp.AppState(db_path=self.db))
            self.assertIn("Settings", str(ctx.exception))

    def test_api_reports_demo_mode(self):
        seed_demo.seed(LISTINGS)
        state = webapp.AppState(db_path=self.db, demo=True)
        self.assertTrue(webapp.api_tasks(state)["demo"])
        self.assertFalse(webapp.api_tasks(webapp.AppState(db_path=self.db))["demo"])


if __name__ == "__main__":
    unittest.main()

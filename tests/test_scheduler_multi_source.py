"""Stage C tests: one task, many sources; aggregated checks; one alert per deal.

Everything here is offline. Fetching and sending are injected, so no network, LLM or
SMTP call happens.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
import scheduler
import pricing
import storage

PB_URL = "https://www.pbtech.co.nz/product/air"
JB_URL = "https://www.jbhifi.co.nz/products/air"


def quote_for(url: str, price: str = "239.00", *, in_stock: bool | None = True,
              currency: str = "NZD", retailer: str = "shop",
              product_name_override: str | None = None) -> dict:
    return {
        "product_name": product_name_override or "Google Fitbit Air (Obsidian)",
        "url": url,
        "retailer": retailer,
        "price": price,
        "currency": currency,
        "in_stock": in_stock,
        "fetched_at": "2026-09-24T00:00:00+00:00",
        "shipping_cost": None,
    }


class StageCTestCase(unittest.TestCase):
    def setUp(self):
        scheduler._active_scheduler = None
        scheduler._scheduler_has_run = False
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        storage.init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def make_task(self, target: str = "250.00", comparison: str = "lte") -> dict:
        task = {
            "product_name": "Google Fitbit Air (Obsidian)",
            "url": PB_URL,
            "target_price": target,
            "currency": "NZD",
            "comparison": comparison,
            "frequency_minutes": 60,
            "notify_email": "user@example.com",
            "status": "active",
        }
        task_id = storage.create_task_with_sources(
            task,
            [{"url": PB_URL, "retailer": "PB Tech"}, {"url": JB_URL, "retailer": "JB Hi-Fi"}],
            db_path=self.db_path,
        )
        task["id"] = task_id
        return storage.get_task(task_id, db_path=self.db_path)

    def make_single_task(self, target: str = "250.00") -> dict:
        task = {
            "product_name": "Google Fitbit Air (Obsidian)",
            "url": PB_URL,
            "target_price": target,
            "currency": "NZD",
            "comparison": "lte",
            "frequency_minutes": 60,
            "notify_email": "user@example.com",
            "status": "active",
        }
        task["id"] = storage.create_task(task, db_path=self.db_path)
        return storage.get_task(task["id"], db_path=self.db_path)

    @staticmethod
    def ok_send(*_args, **_kwargs):
        return {"success": True}


class TestMigration(StageCTestCase):
    def test_create_task_always_yields_one_source(self):
        task = self.make_single_task()
        sources = storage.list_task_sources(task["id"], db_path=self.db_path)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["url"], PB_URL)
        self.assertEqual(sources[0]["retailer"], "pbtech.co.nz")

    def test_multi_source_task_keeps_both(self):
        task = self.make_task()
        sources = storage.list_task_sources(task["id"], db_path=self.db_path)
        self.assertEqual([s["retailer"] for s in sources], ["PB Tech", "JB Hi-Fi"])

    def test_init_db_is_idempotent_and_does_not_duplicate_sources(self):
        task = self.make_task()
        for _ in range(3):
            storage.init_db(self.db_path)
        self.assertEqual(len(storage.list_task_sources(task["id"], db_path=self.db_path)), 2)

    def test_legacy_database_is_backfilled_once(self):
        """A DB written before the sources table must get exactly one source per task."""
        # Simulate a pre-Stage-C database: drop the new structures and re-migrate.
        import sqlite3

        task = self.make_single_task()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("DROP TABLE task_sources")
            conn.execute("DROP INDEX IF EXISTS idx_sources_task_id")
            conn.commit()
        finally:
            conn.close()

        storage.init_db(self.db_path)
        sources = storage.list_task_sources(task["id"], db_path=self.db_path)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["url"], PB_URL)

        storage.init_db(self.db_path)
        self.assertEqual(len(storage.list_task_sources(task["id"], db_path=self.db_path)), 1)

    def test_backfill_does_not_touch_multi_source_tasks(self):
        task = self.make_task()
        import sqlite3

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("DELETE FROM task_sources WHERE retailer = 'JB Hi-Fi'")
            conn.commit()
        finally:
            conn.close()
        # Only the task with *no* rows is backfilled; the surviving source is kept.
        storage.init_db(self.db_path)
        self.assertEqual(len(storage.list_task_sources(task["id"], db_path=self.db_path)), 1)

    def test_duplicate_source_url_is_ignored(self):
        task = self.make_task()
        before = len(storage.list_task_sources(task["id"], db_path=self.db_path))
        existing = storage.list_task_sources(task["id"], db_path=self.db_path)[1]
        new_id = storage.add_task_source(task["id"], JB_URL, db_path=self.db_path)
        self.assertEqual(new_id, existing["id"])
        self.assertEqual(len(storage.list_task_sources(task["id"], db_path=self.db_path)), before)

    def test_old_quotes_remain_readable_without_a_source(self):
        task = self.make_task()
        quote_id = storage.save_quote(task["id"], quote_for(PB_URL, "260.00"), db_path=self.db_path)
        stored = storage.get_quote(quote_id, db_path=self.db_path)
        self.assertEqual(stored["price"], "260.00")
        self.assertIsNone(stored["source_id"])


class TestAggregatedCheck(StageCTestCase):
    def test_both_sources_are_checked_and_recorded(self):
        task = self.make_task()
        seen: list[str] = []

        def fetcher(url):
            seen.append(url)
            return quote_for(url, "239.00", retailer="PB Tech" if "pbtech" in url else "JB Hi-Fi")

        result = scheduler.check_task(task, fetcher=fetcher, send_fn=self.ok_send, db_path=self.db_path)
        self.assertEqual(sorted(seen), sorted([PB_URL, JB_URL]))
        self.assertEqual(result["sources_checked"], 2)
        self.assertTrue(result["is_deal"])
        self.assertTrue(result["notified"])

        sources = storage.list_task_sources(task["id"], db_path=self.db_path)
        for source in sources:
            rows = storage.list_quotes(source_id=source["id"], db_path=self.db_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_id"], source["id"])

    def test_two_sources_both_matching_produce_one_notification(self):
        task = self.make_task()
        result = scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        self.assertTrue(result["notified"])
        events = storage.list_events(task_id=task["id"], db_path=self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "sent")

    def test_repeated_checks_in_one_episode_do_not_send_twice(self):
        task = self.make_task()
        calls = []

        def counting_send(email, task_row, quote):
            calls.append(quote)
            return {"success": True}

        for _ in range(3):
            scheduler.check_task(
                task, fetcher=lambda url: quote_for(url, "239.00"),
                send_fn=counting_send, db_path=self.db_path,
            )
        self.assertEqual(len(calls), 1)

    def test_cheapest_valid_offer_is_chosen(self):
        task = self.make_task()

        def fetcher(url):
            return quote_for(url, "245.00" if "pbtech" in url else "229.00",
                             retailer="PB Tech" if "pbtech" in url else "JB Hi-Fi")

        result = scheduler.check_task(task, fetcher=fetcher, send_fn=self.ok_send, db_path=self.db_path)
        self.assertEqual(result["chosen_price"], "229.00")
        self.assertEqual(result["chosen_retailer"], "JB Hi-Fi")

    def test_out_of_stock_is_not_the_best_offer(self):
        task = self.make_task()

        def fetcher(url):
            # Cheaper, but not buyable: it must not represent the task.
            if "pbtech" in url:
                return quote_for(url, "199.00", in_stock=False, retailer="PB Tech")
            return quote_for(url, "239.00", retailer="JB Hi-Fi")

        result = scheduler.check_task(task, fetcher=fetcher, send_fn=self.ok_send, db_path=self.db_path)
        self.assertEqual(result["chosen_price"], "239.00")
        self.assertTrue(result["is_deal"])

    def test_one_source_failing_does_not_block_the_other(self):
        task = self.make_task()

        def fetcher(url):
            if "pbtech" in url:
                raise pricing.ObservationError("connection refused")
            return quote_for(url, "239.00", retailer="JB Hi-Fi")

        result = scheduler.check_task(task, fetcher=fetcher, send_fn=self.ok_send, db_path=self.db_path)
        self.assertEqual(result["sources_checked"], 2)
        self.assertEqual(result["sources_failed"], 1)
        self.assertTrue(result["is_deal"])
        self.assertTrue(result["notified"])

    def test_one_source_failing_does_not_end_the_deal(self):
        task = self.make_task()
        scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        self.assertEqual(len(storage.list_events(task_id=task["id"], db_path=self.db_path)), 1)

        def broken_then_high(url):
            if "pbtech" in url:
                raise pricing.ObservationError("timeout")
            return quote_for(url, "400.00", retailer="JB Hi-Fi")

        result = scheduler.check_task(task, fetcher=broken_then_high, send_fn=self.ok_send, db_path=self.db_path)
        self.assertFalse(result.get("event_reset"))
        self.assertEqual(result.get("event_reset_reason"), "not_all_sources_comparable")
        events = storage.list_events(task_id=task["id"], db_path=self.db_path)
        self.assertTrue(any(e["status"] == "sent" for e in events))

    def test_all_sources_comparable_and_above_target_resets(self):
        task = self.make_task()
        scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        result = scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "400.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        self.assertTrue(result["event_reset"])
        self.assertEqual([e["status"] for e in storage.list_events(task_id=task["id"], db_path=self.db_path)], ["reset"])

    def test_deal_returning_after_reset_notifies_again(self):
        task = self.make_task()
        sends = []

        def counting_send(email, task_row, quote):
            sends.append(quote)
            return {"success": True}

        scheduler.check_task(task, fetcher=lambda url: quote_for(url, "239.00"),
                             send_fn=counting_send, db_path=self.db_path)
        scheduler.check_task(task, fetcher=lambda url: quote_for(url, "400.00"),
                             send_fn=counting_send, db_path=self.db_path)
        scheduler.check_task(task, fetcher=lambda url: quote_for(url, "239.00"),
                             send_fn=counting_send, db_path=self.db_path)
        self.assertEqual(len(sends), 2)
        statuses = [e["status"] for e in storage.list_events(task_id=task["id"], db_path=self.db_path)]
        self.assertEqual(statuses, ["sent", "reset"])

    def test_no_enabled_sources_is_reported_not_silently_skipped(self):
        task = self.make_task()
        for source in storage.list_task_sources(task["id"], db_path=self.db_path):
            storage.set_task_source_enabled(source["id"], False, db_path=self.db_path)
        result = scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "no_enabled_sources")

    def test_disabled_source_is_not_fetched(self):
        task = self.make_task()
        sources = storage.list_task_sources(task["id"], db_path=self.db_path)
        storage.set_task_source_enabled(sources[0]["id"], False, db_path=self.db_path)
        seen = []
        result = scheduler.check_task(
            task, fetcher=lambda url: seen.append(url) or quote_for(url, "239.00"),
            send_fn=self.ok_send, db_path=self.db_path,
        )
        self.assertEqual(result["sources_checked"], 1)
        self.assertEqual(seen, [JB_URL])

    def test_alert_reports_the_other_sources(self):
        task = self.make_task()
        captured = {}

        def capture_send(email, task_row, quote):
            captured["quote"] = quote
            return {"success": True}

        def fetcher(url):
            if "pbtech" in url:
                raise pricing.ObservationError("403 Forbidden")
            return quote_for(url, "239.00", retailer="JB Hi-Fi")

        scheduler.check_task(task, fetcher=fetcher, send_fn=capture_send, db_path=self.db_path)
        others = captured["quote"]["other_sources"]
        self.assertEqual(len(others), 1)
        self.assertIn("could not be checked", others[0]["state"])
        self.assertIn("403", others[0]["state"])

    def test_email_body_mentions_other_sources(self):
        import notifier

        task = self.make_task()
        quote = quote_for(JB_URL, "239.00", retailer="JB Hi-Fi")
        quote["other_sources"] = [
            {"retailer": "PB Tech", "url": PB_URL, "state": "could not be checked (403)"}
        ]
        with patch("notifier.send_email_notification", return_value={"success": True}) as send:
            notifier.notify_deal("user@example.com", task, quote)
        body = send.call_args.args[2]
        self.assertIn("Other monitored sources", body)
        self.assertIn("PB Tech", body)

    def test_old_single_url_task_still_checks(self):
        task = self.make_single_task()
        result = scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        self.assertTrue(result["is_deal"])
        self.assertEqual(result["sources_checked"], 1)

    def test_unknown_stock_does_not_reset_the_episode(self):
        task = self.make_task()
        scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        result = scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "400.00", in_stock=None),
            send_fn=self.ok_send, db_path=self.db_path,
        )
        self.assertFalse(result.get("event_reset"))


class TestSourceAndHistory(StageCTestCase):
    def test_history_is_recorded_per_source_over_time(self):
        task = self.make_task()
        for price in ("260.00", "250.00", "239.00"):
            scheduler.check_task(
                task, fetcher=lambda url, p=price: quote_for(url, p),
                send_fn=self.ok_send, db_path=self.db_path,
            )
        rows = storage.list_quotes(task_id=task["id"], db_path=self.db_path)
        # 3 rounds x 2 sources, newest first.
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["price"], "239.00")
        self.assertEqual(rows[0]["source_id"], rows[2]["source_id"])
        per_source = storage.list_quotes(source_id=rows[0]["source_id"], db_path=self.db_path)
        self.assertEqual([r["price"] for r in per_source], ["239.00", "250.00", "260.00"])

    def test_record_source_check_stamps_time_and_error(self):
        task = self.make_task()
        source = storage.list_task_sources(task["id"], db_path=self.db_path)[0]

        def fetcher(url):
            if "pbtech" in url:
                raise pricing.ObservationError("boom")
            return quote_for(url, "239.00", retailer="JB Hi-Fi")

        scheduler.check_task(task, fetcher=fetcher, send_fn=self.ok_send, db_path=self.db_path)
        updated = storage.get_task_source(source["id"], db_path=self.db_path)
        self.assertIsNotNone(updated["last_checked_at"])
        self.assertIn("boom", updated["last_error"])
        good = storage.get_task_source(
            storage.list_task_sources(task["id"], db_path=self.db_path)[1]["id"], db_path=self.db_path
        )
        self.assertIsNone(good["last_error"])

    def test_enable_and_disable_round_trip(self):
        task = self.make_task()
        source = storage.list_task_sources(task["id"], db_path=self.db_path)[0]
        storage.set_task_source_enabled(source["id"], False, db_path=self.db_path)
        self.assertEqual(len(storage.list_task_sources(task["id"], db_path=self.db_path, enabled_only=True)), 1)
        storage.set_task_source_enabled(source["id"], True, db_path=self.db_path)
        self.assertEqual(len(storage.list_task_sources(task["id"], db_path=self.db_path, enabled_only=True)), 2)

    def test_missing_source_returns_none(self):
        self.assertIsNone(storage.set_task_source_enabled(999, True, db_path=self.db_path))


class TestModifyConditions(StageCTestCase):
    def test_changing_target_ends_the_current_episode(self):
        task = self.make_task()
        scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        updated = storage.update_task_conditions(task["id"], target_price="200.00", db_path=self.db_path)
        self.assertEqual(updated["target_price"], "200.00")
        reset = storage.reset_active_events(task["id"], db_path=self.db_path)
        self.assertEqual(reset, 1)
        self.assertEqual([e["status"] for e in storage.list_events(task_id=task["id"], db_path=self.db_path)], ["reset"])

    def test_new_threshold_is_used_by_the_next_check(self):
        task = self.make_task()
        scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        storage.update_task_conditions(task["id"], target_price="200.00", db_path=self.db_path)
        storage.reset_active_events(task["id"], db_path=self.db_path)
        # 239 is now above the new 200 threshold: no new deal, no new event.
        result = scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        self.assertFalse(result["is_deal"])
        self.assertEqual([e["status"] for e in storage.list_events(task_id=task["id"], db_path=self.db_path)], ["reset"])

    def test_frequency_change_does_not_create_an_event(self):
        task = self.make_task()
        scheduler.check_task(
            task, fetcher=lambda url: quote_for(url, "239.00"), send_fn=self.ok_send, db_path=self.db_path
        )
        updated = storage.update_task_conditions(task["id"], frequency_minutes=15, db_path=self.db_path)
        self.assertEqual(updated["frequency_minutes"], 15)
        events = storage.list_events(task_id=task["id"], db_path=self.db_path)
        self.assertEqual([e["status"] for e in events], ["sent"])

    def test_comparison_change_is_persisted(self):
        task = self.make_task()
        updated = storage.update_task_conditions(task["id"], comparison="lt", db_path=self.db_path)
        self.assertEqual(updated["comparison"], "lt")
        # 250 with lt is not a deal any more; 249 is.
        task_row = storage.get_task(task["id"], db_path=self.db_path)
        self.assertFalse(scheduler.is_deal(task_row, quote_for(PB_URL, "250.00")))
        self.assertTrue(scheduler.is_deal(task_row, quote_for(PB_URL, "249.00")))

    def test_no_field_supplied_changes_nothing(self):
        task = self.make_task()
        updated = storage.update_task_conditions(task["id"], db_path=self.db_path)
        self.assertEqual(updated["target_price"], "250.00")
        self.assertEqual(updated["frequency_minutes"], 60)


class TestSourceOwnership(StageCTestCase):
    """Review fix 1: --enable/--disable must verify ownership before writing."""

    def test_storage_scoped_toggle_updates_only_the_owning_task(self):
        first = self.make_task()
        second = self.make_task()
        foreign = storage.list_task_sources(second["id"], db_path=self.db_path)[0]

        outcome, row = storage.set_task_source_enabled_for_task(
            first["id"], foreign["id"], False, db_path=self.db_path
        )
        self.assertEqual(outcome, "foreign")
        # The foreign row is untouched.
        self.assertEqual(
            storage.get_task_source(foreign["id"], db_path=self.db_path)["enabled"], 1
        )

        outcome, row = storage.set_task_source_enabled_for_task(
            first["id"], foreign["id"] + 10000, False, db_path=self.db_path
        )
        self.assertEqual(outcome, "missing")
        self.assertIsNone(row)

        own = storage.list_task_sources(first["id"], db_path=self.db_path)[0]
        outcome, row = storage.set_task_source_enabled_for_task(
            first["id"], own["id"], False, db_path=self.db_path
        )
        self.assertEqual(outcome, "updated")
        self.assertEqual(
            storage.get_task_source(own["id"], db_path=self.db_path)["enabled"], 0
        )

    def test_cli_sources_with_a_foreign_source_changes_nothing(self):
        import cli

        first = self.make_task()
        second = self.make_task()
        foreign = storage.list_task_sources(second["id"], db_path=self.db_path)[0]
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(
                ["--db-path", str(self.db_path), "sources", str(first["id"]),
                 "--disable", str(foreign["id"])]
            )
        self.assertEqual(rc, 1)
        self.assertIn("belongs to task", stderr.getvalue())
        # The other task's source must still be enabled.
        self.assertEqual(
            storage.get_task_source(foreign["id"], db_path=self.db_path)["enabled"], 1
        )

    def test_cli_sources_with_a_missing_source_changes_nothing(self):
        import cli

        task = self.make_task()
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(
                ["--db-path", str(self.db_path), "sources", str(task["id"]),
                 "--disable", "424242"]
            )
        self.assertEqual(rc, 1)
        self.assertIn("not found", stderr.getvalue())
        self.assertEqual(
            len(storage.list_task_sources(task["id"], enabled_only=True, db_path=self.db_path)), 2
        )


class TestConditionChangeRace(StageCTestCase):
    """Review fix 3: an in-flight check must never send under old conditions."""

    def _two_source_task(self) -> dict:
        return self.make_task()

    def test_modify_during_fetch_uses_the_new_threshold(self):
        import threading

        task = self._two_source_task()
        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def blocking_fetch(url):
            fetch_started.set()
            release_fetch.wait(timeout=5)
            return quote_for(url, "239.00", retailer="PB Tech")

        sent = []
        outcome: dict = {}

        def run_check():
            outcome["result"] = scheduler.check_task(
                task,
                fetcher=blocking_fetch,
                send_fn=lambda *a: sent.append(1) or {"success": True},
                db_path=self.db_path,
            )

        worker = threading.Thread(target=run_check)
        worker.start()
        self.assertTrue(fetch_started.wait(timeout=5))
        # The threshold change commits while the check is still fetching.
        storage.update_task_conditions(
            task["id"], target_price="100.00", db_path=self.db_path, end_episode=True
        )
        release_fetch.set()
        worker.join(timeout=10)

        result = outcome["result"]
        self.assertFalse(result["is_deal"])   # 239 is not a deal under NZD 100.
        self.assertFalse(result["notified"])
        self.assertEqual(sent, [])

    def test_disable_chosen_source_during_fetch_denies_the_permit(self):
        import threading

        task = self._two_source_task()
        sources = storage.list_task_sources(task["id"], db_path=self.db_path)
        chosen = sources[0]  # PB Tech; its price will be the best offer.
        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def blocking_fetch(url):
            fetch_started.set()
            release_fetch.wait(timeout=5)
            return quote_for(url, "239.00", retailer="PB Tech")

        sent = []
        outcome: dict = {}

        def run_check():
            outcome["result"] = scheduler.check_task(
                task,
                fetcher=blocking_fetch,
                send_fn=lambda *a: sent.append(1) or {"success": True},
                db_path=self.db_path,
            )

        worker = threading.Thread(target=run_check)
        worker.start()
        self.assertTrue(fetch_started.wait(timeout=5))
        # The user disables the source whose price would trigger the alert.
        storage.set_task_source_enabled(chosen["id"], False, db_path=self.db_path)
        release_fetch.set()
        worker.join(timeout=10)

        result = outcome["result"]
        self.assertTrue(result["is_deal"])
        self.assertTrue(result["notification_cancelled"])
        self.assertEqual(result["notification_cancelled_reason"], "send_permit_denied")
        self.assertFalse(result["notified"])
        self.assertEqual(sent, [])

    def test_permit_config_version_guard(self):
        task = self.make_single_task()
        version = storage.get_task(task["id"], db_path=self.db_path)["config_version"]
        self.assertTrue(
            storage.try_begin_send(
                task["id"], "owner-a", db_path=self.db_path, config_version=version
            )
        )
        storage.end_send(task["id"], "owner-a", db_path=self.db_path)
        # A threshold change bumps the version, so the stale permit is denied.
        storage.update_task_conditions(
            task["id"], target_price="100.00", db_path=self.db_path, end_episode=True
        )
        self.assertFalse(
            storage.try_begin_send(
                task["id"], "owner-b", db_path=self.db_path, config_version=version
            )
        )
        new_version = storage.get_task(task["id"], db_path=self.db_path)["config_version"]
        self.assertGreater(new_version, version)
        self.assertTrue(
            storage.try_begin_send(
                task["id"], "owner-b", db_path=self.db_path, config_version=new_version
            )
        )
        storage.end_send(task["id"], "owner-b", db_path=self.db_path)

    def test_permit_source_guard(self):
        task = self.make_single_task()
        source = storage.list_task_sources(task["id"], db_path=self.db_path)[0]
        self.assertTrue(
            storage.try_begin_send(
                task["id"], "owner-a", db_path=self.db_path, source_id=source["id"]
            )
        )
        storage.end_send(task["id"], "owner-a", db_path=self.db_path)
        # Disabling the triggering source denies the permit.
        storage.set_task_source_enabled(source["id"], False, db_path=self.db_path)
        self.assertFalse(
            storage.try_begin_send(
                task["id"], "owner-b", db_path=self.db_path, source_id=source["id"]
            )
        )


if __name__ == "__main__":
    unittest.main()

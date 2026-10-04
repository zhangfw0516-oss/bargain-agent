"""Tests for scheduler.is_deal and check_task."""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import schedule

import scraper
import scheduler
import storage


class TestScheduler(unittest.TestCase):
    def setUp(self):
        # The scheduler is a once-per-process singleton; reset the module-level state
        # so each test starts from a clean process view.
        scheduler._active_scheduler = None
        scheduler._scheduler_has_run = False
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        storage.init_db(self.db_path)
        self.task = {
            "id": 1,
            "product_name": "Kindle",
            "url": "https://www.pbtech.co.nz/kindle",
            "target_price": "300.00",
            "currency": "NZD",
            "comparison": "lt",
            "frequency_minutes": 60,
            "notify_email": "user@example.com",
            "status": "active",
        }
        storage.create_task(self.task, db_path=self.db_path)
        self.task["id"] = 1

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _quote(self, price: str, in_stock: bool = True, currency: str = "NZD") -> dict:
        return {
            "product_name": "Kindle",
            "url": self.task["url"],
            "retailer": "pbtech.co.nz",
            "price": price,
            "currency": currency,
            "in_stock": in_stock,
            "fetched_at": "2026-09-23T04:00:00+00:00",
        }

    def test_is_deal_lt(self):
        self.assertTrue(scheduler.is_deal(self.task, self._quote("299.00")))

    def test_is_deal_lte(self):
        task = {**self.task, "comparison": "lte"}
        self.assertTrue(scheduler.is_deal(task, self._quote("300.00")))

    def test_not_deal_when_higher(self):
        self.assertFalse(scheduler.is_deal(self.task, self._quote("301.00")))

    def test_not_deal_when_out_of_stock(self):
        self.assertFalse(scheduler.is_deal(self.task, self._quote("299.00", in_stock=False)))

    def test_not_deal_currency_mismatch(self):
        self.assertFalse(scheduler.is_deal(self.task, self._quote("299.00", currency="AUD")))

    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_check_task_creates_quote(self, mock_retries):
        def fetcher(url):
            return self._quote("301.00")
        result = scheduler.check_task(self.task, fetcher=fetcher, send_fn=lambda *args: {"success": True}, db_path=self.db_path)
        self.assertIn("quote_id", result)
        quote = storage.get_quote(result["quote_id"], db_path=self.db_path)
        self.assertEqual(quote["price"], "301.00")

    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_check_task_sends_notification(self, mock_retries):
        def fetcher(url):
            return self._quote("299.00")
        sent = []
        def send_fn(email, task, quote):
            sent.append((email, task, quote))
            return {"success": True}
        result = scheduler.check_task(self.task, fetcher=fetcher, send_fn=send_fn, db_path=self.db_path)
        self.assertTrue(result["is_deal"])
        self.assertTrue(result["notified"])
        self.assertEqual(len(sent), 1)

    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_check_task_resets_event(self, mock_retries):
        def fetcher(url):
            return self._quote("299.00")
        scheduler.check_task(self.task, fetcher=fetcher, send_fn=lambda *args: {"success": True}, db_path=self.db_path)
        def fetcher_high(url):
            return self._quote("301.00")
        result = scheduler.check_task(self.task, fetcher=fetcher_high, send_fn=lambda *args: {"success": True}, db_path=self.db_path)
        self.assertFalse(result["is_deal"])
        self.assertTrue(result.get("event_reset"))

    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_reset_then_new_deal(self, mock_retries):
        def fetcher(url):
            return self._quote("299.00")
        scheduler.check_task(self.task, fetcher=fetcher, send_fn=lambda *args: {"success": True}, db_path=self.db_path)
        scheduler.check_task(self.task, fetcher=lambda url: self._quote("301.00"), send_fn=lambda *args: {"success": True}, db_path=self.db_path)
        scheduler.check_task(self.task, fetcher=fetcher, send_fn=lambda *args: {"success": True}, db_path=self.db_path)
        events = storage.list_events(task_id=self.task["id"], db_path=self.db_path)
        sent = [e for e in events if e["status"] == "sent"]
        reset = [e for e in events if e["status"] == "reset"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(reset), 1)

    # R1 regression: scraping failure after a deal must not reset the event and send again.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_scraper_failure_does_not_reset_deal_event(self, mock_retries):
        class Boom(scraper.ScraperError):
            pass

        def fetcher_deal(url):
            return self._quote("299.00")

        def fetcher_fail(url):
            raise scraper.ScraperError("network down")

        sent = []
        def send_fn(email, task, quote):
            sent.append(quote.get("price"))
            return {"success": True}

        scheduler.check_task(self.task, fetcher=fetcher_deal, send_fn=send_fn, db_path=self.db_path)
        self.assertEqual(len(sent), 1)

        scheduler.check_task(self.task, fetcher=fetcher_fail, send_fn=send_fn, db_path=self.db_path)
        # After failure, the next deal should reuse the same sent event, not send again.
        scheduler.check_task(self.task, fetcher=fetcher_deal, send_fn=send_fn, db_path=self.db_path)
        self.assertEqual(len(sent), 1)

        events = storage.list_events(task_id=self.task["id"], db_path=self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "sent")

    # R1 regression: missing price/currency/in_stock does not reset the active event.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_incomparable_quote_does_not_reset_event(self, mock_retries):
        def fetcher_deal(url):
            return self._quote("299.00")

        def fetcher_broken(url):
            return {
                "product_name": "Kindle",
                "url": url,
                "retailer": "pbtech.co.nz",
                "price": None,
                "currency": "NZD",
                "in_stock": True,
                "fetched_at": "2026-09-23T04:00:00+00:00",
            }

        scheduler.check_task(self.task, fetcher=fetcher_deal, send_fn=lambda *args: {"success": True}, db_path=self.db_path)
        scheduler.check_task(self.task, fetcher=fetcher_broken, send_fn=lambda *args: {"success": True}, db_path=self.db_path)
        events = storage.list_events(task_id=self.task["id"], db_path=self.db_path)
        self.assertEqual(events[0]["status"], "sent")

    # R2 regression: permanent notification failure exhausts budget; no new events.
    @patch.object(scheduler.config, "notification_max_retries", return_value=2)
    def test_permanent_failure_stops_immediately(self, mock_retries):
        sent = []
        def send_fn(email, task, quote):
            sent.append(1)
            return {"success": False, "retryable": False, "error": "auth failed"}

        for _ in range(4):
            scheduler.check_task(self.task, fetcher=lambda url: self._quote("299.00"), send_fn=send_fn, db_path=self.db_path)

        events = storage.list_events(task_id=self.task["id"], db_path=self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")
        # A permanent error stops after one SMTP attempt and stays failed.
        self.assertEqual(events[0]["attempts"], 1)
        self.assertEqual(len(sent), 1)

    # R2 regression: retryable failures consume the budget then stop.
    @patch.object(scheduler.config, "notification_max_retries", return_value=2)
    def test_retryable_failure_consumes_budget(self, mock_retries):
        sent = []
        def send_fn(email, task, quote):
            sent.append(1)
            return {"success": False, "retryable": True, "error": "timeout"}

        for _ in range(5):
            scheduler.check_task(self.task, fetcher=lambda url: self._quote("299.00"), send_fn=send_fn, db_path=self.db_path)

        events = storage.list_events(task_id=self.task["id"], db_path=self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["attempts"], 2)
        self.assertEqual(len(sent), 2)

    # R2 regression: after leaving deal band, a new deal creates a new event.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_new_event_after_reset(self, mock_retries):
        def fetcher_deal(url):
            return self._quote("299.00")
        def fetcher_high(url):
            return self._quote("301.00")

        sent = []
        def send_fn(email, task, quote):
            sent.append(quote.get("price"))
            return {"success": True}

        scheduler.check_task(self.task, fetcher=fetcher_deal, send_fn=send_fn, db_path=self.db_path)
        scheduler.check_task(self.task, fetcher=fetcher_high, send_fn=send_fn, db_path=self.db_path)
        scheduler.check_task(self.task, fetcher=fetcher_deal, send_fn=send_fn, db_path=self.db_path)

        events = storage.list_events(task_id=self.task["id"], db_path=self.db_path)
        sent_events = [e for e in events if e["status"] == "sent"]
        reset_events = [e for e in events if e["status"] == "reset"]
        self.assertEqual(len(events), 2)
        self.assertEqual(len(sent_events), 1)
        self.assertEqual(len(reset_events), 1)
        # send_fn is called on the first deal and the new deal after the reset.
        self.assertEqual(len(sent), 2)

    # R5 regression: concurrent check on the same task is skipped.
    def test_concurrent_check_skipped(self):
        started = threading.Event()
        proceed = threading.Event()

        def fetcher(url):
            started.set()
            proceed.wait()
            return self._quote("301.00")

        def run_check():
            return scheduler.check_task(self.task, fetcher=fetcher, send_fn=lambda *args: {"success": True}, db_path=self.db_path)

        t = threading.Thread(target=run_check)
        t.start()
        started.wait()
        # While the first check is blocked inside fetcher, a second check should be skipped.
        result = scheduler.check_task(self.task, fetcher=lambda url: self._quote("301.00"), send_fn=lambda *args: {"success": True}, db_path=self.db_path)
        self.assertTrue(result.get("skipped"))
        proceed.set()
        t.join()

    # R5 regression: scheduler restart reloads active event state.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_restart_preserves_failed_event(self, mock_retries):
        def send_fn(email, task, quote):
            return {"success": False, "retryable": False, "error": "auth failed"}

        scheduler.check_task(self.task, fetcher=lambda url: self._quote("299.00"), send_fn=send_fn, db_path=self.db_path)

        # Simulate restart: fresh scheduler instance, same DB.
        new_scheduler = scheduler.TaskScheduler(db_path=self.db_path)
        sent = []
        def send_fn2(email, task, quote):
            sent.append(1)
            return {"success": True}
        result = scheduler.check_task(self.task, fetcher=lambda url: self._quote("299.00"), send_fn=send_fn2, db_path=self.db_path)
        self.assertFalse(result["notified"])
        self.assertEqual(len(sent), 0)

    # R5 regression: run_once refuses non-active tasks.
    def test_run_once_requires_active(self):
        storage.update_task_status(self.task["id"], "stopped", db_path=self.db_path)
        sched = scheduler.TaskScheduler(db_path=self.db_path)
        with self.assertRaises(scheduler.SchedulerError):
            sched.run_once(self.task["id"])

    # R5 regression: scheduler background loop can be stopped quickly.
    # The task is pushed into the future so the loop never performs a real fetch,
    # and check_task is patched so no network or DB work outlives the test.
    def test_scheduler_stop(self):
        storage.update_next_check(self.task["id"], "2999-01-01T00:00:00+00:00", db_path=self.db_path)
        sched = scheduler.TaskScheduler(db_path=self.db_path)
        with patch.object(scheduler, "check_task"):
            t = threading.Thread(target=sched.run_loop)
            t.start()
            time.sleep(0.2)
            sched.stop()
            t.join(timeout=2)
        self.assertFalse(t.is_alive())

    # ---------------------------------------------------------------- F1

    # F1: an exhausted event is cleared by a verified exit from the deal band, so
    # a later deal gets a fresh event and a fresh budget.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_exhausted_event_cleared_after_valid_exit(self, mock_retries):
        failed_sends = []

        def send_fail(email, task, quote):
            failed_sends.append(1)
            return {"success": False, "retryable": False, "error": "auth"}

        def send_ok(email, task, quote):
            return {"success": True}

        scheduler.check_task(self.task, fetcher=lambda url: self._quote("299.00"), send_fn=send_fail, db_path=self.db_path)
        scheduler.check_task(self.task, fetcher=lambda url: self._quote("301.00"), send_fn=send_ok, db_path=self.db_path)
        result = scheduler.check_task(self.task, fetcher=lambda url: self._quote("299.00"), send_fn=send_ok, db_path=self.db_path)

        self.assertTrue(result["notified"])
        self.assertEqual(len(failed_sends), 1)
        events = storage.list_events(task_id=self.task["id"], db_path=self.db_path)
        statuses = [e["status"] for e in events]
        self.assertEqual(len(events), 2)
        self.assertEqual(statuses.count("sent"), 1)
        self.assertEqual(statuses.count("reset"), 1)

    # F1: an incomparable observation must NOT clear the exhausted state.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_exhausted_event_survives_incomparable_quote(self, mock_retries):
        send_fail = lambda email, task, quote: {"success": False, "retryable": False, "error": "auth"}

        def broken_quote(url):
            return {
                "product_name": "Kindle",
                "url": url,
                "retailer": "pbtech.co.nz",
                "price": None,
                "currency": "NZD",
                "in_stock": True,
                "fetched_at": "2026-09-23T04:00:00+00:00",
            }

        scheduler.check_task(self.task, fetcher=lambda url: self._quote("299.00"), send_fn=send_fail, db_path=self.db_path)
        scheduler.check_task(self.task, fetcher=broken_quote, send_fn=lambda *a: {"success": True}, db_path=self.db_path)
        result = scheduler.check_task(self.task, fetcher=lambda url: self._quote("299.00"), send_fn=lambda *a: {"success": True}, db_path=self.db_path)

        self.assertFalse(result["notified"])
        events = storage.list_events(task_id=self.task["id"], db_path=self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")

    # F1: the same holds after rebuilding the scheduler against the same database.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_exhausted_then_exit_survives_restart(self, mock_retries):
        send_fail = lambda email, task, quote: {"success": False, "retryable": False, "error": "auth"}
        scheduler.check_task(self.task, fetcher=lambda url: self._quote("299.00"), send_fn=send_fail, db_path=self.db_path)

        # Fresh scheduler instance reading the same database: exit the band.
        restarted = scheduler.TaskScheduler(db_path=self.db_path)
        self.assertIsNotNone(restarted.db_path)
        task = storage.get_task(self.task["id"], db_path=self.db_path)
        scheduler.check_task(task, fetcher=lambda url: self._quote("301.00"), send_fn=lambda *a: {"success": True}, db_path=self.db_path)
        result = scheduler.check_task(task, fetcher=lambda url: self._quote("299.00"), send_fn=lambda *a: {"success": True}, db_path=self.db_path)
        self.assertTrue(result["notified"])

    # ---------------------------------------------------------------- F2

    # F2: a task stopped while the fetch is in flight must not be notified.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_stop_during_fetch_cancels_notification(self, mock_retries):
        sent = []

        def fetcher(url):
            storage.update_task_status(self.task["id"], "stopped", db_path=self.db_path)
            return self._quote("299.00")

        def send_fn(email, task, quote):
            sent.append(1)
            return {"success": True}

        result = scheduler.check_task(self.task, fetcher=fetcher, send_fn=send_fn, db_path=self.db_path)
        self.assertTrue(result["is_deal"])
        self.assertFalse(result["notified"])
        self.assertTrue(result.get("notification_cancelled"))
        self.assertEqual(sent, [])
        self.assertEqual(storage.list_events(task_id=self.task["id"], db_path=self.db_path), [])

    # F2: a task paused before the check is skipped entirely (no fetch).
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_paused_task_is_not_fetched(self, mock_retries):
        storage.update_task_status(self.task["id"], "paused", db_path=self.db_path)
        called = []

        def fetcher(url):
            called.append(url)
            return self._quote("299.00")

        result = scheduler.check_task(self.task, fetcher=fetcher, send_fn=lambda *a: {"success": True}, db_path=self.db_path)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "task_paused")
        self.assertEqual(called, [])

    # F2: a second checker (another process) holding the DB lock is rejected.
    def test_lock_held_by_other_process_is_skipped(self):
        self.assertTrue(storage.acquire_task_lock(self.task["id"], "other-process", db_path=self.db_path))
        called = []
        result = scheduler.check_task(
            self.task,
            fetcher=lambda url: called.append(url) or self._quote("299.00"),
            send_fn=lambda *a: {"success": True},
            db_path=self.db_path,
        )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "lock_held")
        self.assertEqual(called, [])

    # F2: run_once surfaces a lock conflict instead of silently doing nothing.
    def test_run_once_reports_lock_conflict(self):
        storage.acquire_task_lock(self.task["id"], "other-process", db_path=self.db_path)
        sched = scheduler.TaskScheduler(db_path=self.db_path)
        with self.assertRaises(scheduler.SchedulerError):
            sched.run_once(self.task["id"])

    # ---------------------------------------------------------------- F3

    def _make_due_tasks(self, count: int) -> list[int]:
        ids = []
        for index in range(count):
            ids.append(
                storage.create_task(
                    {
                        **{k: v for k, v in self.task.items() if k != "id"},
                        "product_name": f"Product {index}",
                    },
                    db_path=self.db_path,
                )
            )
        for task_id in ids:
            storage.update_next_check(task_id, "2000-01-01T00:00:00+00:00", db_path=self.db_path)
        return ids

    # F3: repeated ticks must not queue duplicate work for the same task.
    def test_repeated_tick_does_not_duplicate(self):
        storage.update_next_check(self.task["id"], "2000-01-01T00:00:00+00:00", db_path=self.db_path)
        sched = scheduler.TaskScheduler(db_path=self.db_path, max_workers=1, max_backlog=4)
        sched._executor = ThreadPoolExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def fake_check(task, **kwargs):
            started.set()
            release.wait(timeout=5)

        try:
            with patch.object(scheduler, "check_task", side_effect=fake_check):
                sched._tick()
                self.assertTrue(started.wait(timeout=5))
                sched._tick()
                sched._tick()
                self.assertEqual(sched.pending_count(), 1)
        finally:
            release.set()
            sched._executor.shutdown(wait=True)
            sched._executor = None

    # F3: the pending backlog is bounded; extra due tasks are simply deferred.
    def test_backlog_is_bounded(self):
        self._make_due_tasks(4)
        sched = scheduler.TaskScheduler(db_path=self.db_path, max_workers=1, max_backlog=2)
        sched._executor = ThreadPoolExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def fake_check(task, **kwargs):
            started.set()
            release.wait(timeout=5)

        try:
            with patch.object(scheduler, "check_task", side_effect=fake_check):
                sched._tick()
                self.assertTrue(started.wait(timeout=5))
                self.assertLessEqual(sched.pending_count(), 2)
                # Drain the executor *inside* the patch: a second reserved task is
                # queued behind the blocked worker, and would otherwise run the real
                # `check_task` (a live fetch of the task's PB Tech URL) once the patch
                # is restored. The patch must outlive every queued worker.
                release.set()
                sched._executor.shutdown(wait=True)
        finally:
            release.set()
            sched._executor.shutdown(wait=True)
            sched._executor = None

    # F3: after stop(), queued work is not executed.
    def test_stopped_scheduler_does_not_run_queued_work(self):
        storage.update_next_check(self.task["id"], "2000-01-01T00:00:00+00:00", db_path=self.db_path)
        sched = scheduler.TaskScheduler(db_path=self.db_path, max_workers=1)
        sched._executor = ThreadPoolExecutor(max_workers=1)
        called = []
        try:
            with patch.object(scheduler, "check_task", side_effect=lambda task, **kwargs: called.append(task["id"])):
                sched.stop()
                sched._tick()
                time.sleep(0.2)
        finally:
            sched._executor.shutdown(wait=True, cancel_futures=True)
            sched._executor = None
        self.assertEqual(called, [])

    # H2: the scheduler runs once per process; an in-process restart is refused.
    def test_in_process_restart_is_rejected(self):
        storage.update_next_check(self.task["id"], "2999-01-01T00:00:00+00:00", db_path=self.db_path)
        with patch.object(scheduler, "check_task"):
            sched = scheduler.TaskScheduler(db_path=self.db_path)
            thread = threading.Thread(target=sched.run_loop, daemon=True)
            thread.start()
            time.sleep(0.3)
            self.assertEqual(len(schedule.get_jobs(scheduler.SCHEDULE_TAG)), 1)
            sched.stop()
            thread.join(timeout=3)

            # Same object.
            with self.assertRaises(scheduler.SchedulerError):
                sched.run_loop()
            # A fresh object in the same process.
            with self.assertRaises(scheduler.SchedulerError):
                scheduler.TaskScheduler(db_path=self.db_path).run_loop()
        # The stopped run cleaned up its own schedule job.
        self.assertEqual(len(schedule.get_jobs(scheduler.SCHEDULE_TAG)), 0)

    # H2: two threads starting the same object must not both succeed.
    def test_concurrent_start_of_same_object_is_rejected(self):
        storage.update_next_check(self.task["id"], "2999-01-01T00:00:00+00:00", db_path=self.db_path)
        sched = scheduler.TaskScheduler(db_path=self.db_path)
        gate = threading.Barrier(2, timeout=5)
        results: list[str] = []

        def start() -> None:
            gate.wait(timeout=5)
            try:
                sched.run_loop()
                results.append("ran")
            except scheduler.SchedulerError:
                results.append("rejected")

        with patch.object(scheduler, "check_task"):
            threads = [threading.Thread(target=start, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            time.sleep(0.4)
            sched.stop()
            for thread in threads:
                thread.join(timeout=3)

        self.assertEqual(results.count("ran"), 1)
        self.assertEqual(results.count("rejected"), 1)

    # H1: a permit left behind by a process that died mid-send is recovered by an
    # explicit human step; only then does the task send again.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_recovery_after_a_dying_sender(self, mock_retries):
        repo_root = Path(__file__).resolve().parent.parent
        child_code = (
            "from pathlib import Path\n"
            "import storage\n"
            f"db = Path({str(self.db_path)!r})\n"
            "storage.init_db(db)\n"
            f"assert storage.try_begin_send({self.task['id']}, 'dying-process', db_path=db)\n"
            "print('PERMIT_TAKEN', flush=True)\n"
            "raise SystemExit(1)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", child_code],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=30,
        )
        self.assertIn("PERMIT_TAKEN", proc.stdout, f"child stderr: {proc.stderr[:400]}")

        permit = storage.get_send_permit(self.task["id"], db_path=self.db_path)
        self.assertEqual(permit["send_in_flight"], 1)
        self.assertEqual(permit["send_owner"], "dying-process")

        # While the stale permit is held, the task cannot send.
        sent: list[int] = []
        blocked = scheduler.check_task(
            self.task,
            fetcher=lambda url: self._quote("299.00"),
            send_fn=lambda *a: sent.append(1) or {"success": True},
            db_path=self.db_path,
        )
        self.assertEqual(blocked["notification_cancelled_reason"], "send_permit_denied")
        self.assertEqual(sent, [])

        # Explicit recovery, only valid once the holder has actually exited.
        self.assertEqual(storage.clear_send_permit(self.task["id"], db_path=self.db_path), "cleared")

        recovered = scheduler.check_task(
            self.task,
            fetcher=lambda url: self._quote("299.00"),
            send_fn=lambda *a: sent.append(1) or {"success": True},
            db_path=self.db_path,
        )
        self.assertTrue(recovered["notified"])
        self.assertEqual(len(sent), 1)

    # ---------------------------------------------------------------- G1

    # G1: a stop that lands before the permit is granted must block the send.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_stop_wins_before_send_permit(self, mock_retries):
        sent = []
        real_begin = storage.try_begin_send

        def begin_after_stop(task_id, owner, db_path=None, **kwargs):
            # Simulates another process stopping the task in the window between the
            # last status read and the permit request.
            storage.set_task_status_atomic(task_id, "stopped", db_path=db_path, block_on_send=False)
            return real_begin(task_id, owner, db_path=db_path, **kwargs)

        with patch.object(storage, "try_begin_send", side_effect=begin_after_stop):
            result = scheduler.check_task(
                self.task,
                fetcher=lambda url: self._quote("299.00"),
                send_fn=lambda *a: sent.append(1) or {"success": True},
                db_path=self.db_path,
            )

        self.assertTrue(result["is_deal"])
        self.assertFalse(result["notified"])
        self.assertTrue(result["notification_cancelled"])
        self.assertEqual(result["notification_cancelled_reason"], "send_permit_denied")
        self.assertEqual(sent, [])
        self.assertEqual(storage.list_events(task_id=self.task["id"], db_path=self.db_path), [])

    # G1: if the send won, the stop still applies but the caller learns about it.
    def test_send_wins_then_stop_reports_in_flight(self):
        task_id = self.task["id"]
        self.assertTrue(storage.try_begin_send(task_id, "owner-a", db_path=self.db_path))
        outcome = storage.set_task_status_atomic(task_id, "stopped", db_path=self.db_path, block_on_send=True)
        self.assertEqual(outcome, "in_flight")
        self.assertEqual(storage.get_task(task_id, db_path=self.db_path)["status"], "stopped")
        storage.end_send(task_id, "owner-a", db_path=self.db_path)

    def test_stop_without_send_is_clean(self):
        outcome = storage.set_task_status_atomic(
            self.task["id"], "stopped", db_path=self.db_path, block_on_send=True
        )
        self.assertEqual(outcome, "changed")

    # G1: the permit is single-flight and released afterwards.
    def test_send_permit_is_single_flight(self):
        task_id = self.task["id"]
        self.assertTrue(storage.try_begin_send(task_id, "owner-a", db_path=self.db_path))
        self.assertFalse(storage.try_begin_send(task_id, "owner-b", db_path=self.db_path))
        storage.end_send(task_id, "owner-a", db_path=self.db_path)
        self.assertTrue(storage.try_begin_send(task_id, "owner-b", db_path=self.db_path))

    # G1: a permit granted by a completed send leaves nothing behind.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_send_permit_released_after_check(self, mock_retries):
        scheduler.check_task(
            self.task,
            fetcher=lambda url: self._quote("299.00"),
            send_fn=lambda *a: {"success": True},
            db_path=self.db_path,
        )
        task = storage.get_task(self.task["id"], db_path=self.db_path)
        self.assertEqual(task["send_in_flight"], 0)

    # G1: barrier test — the stop lands after the last status read but before the
    # permit is requested, so no send may start.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_barrier_stop_before_permit_blocks_send(self, mock_retries):
        sent = []
        real_begin = storage.try_begin_send
        permit_requested = threading.Event()
        stop_landed = threading.Event()

        def begin_synced(task_id, owner, db_path=None, **kwargs):
            # Barrier: hold the permit request until the concurrent stop has landed.
            permit_requested.set()
            stop_landed.wait(timeout=5)
            return real_begin(task_id, owner, db_path=db_path, **kwargs)

        def stopper():
            permit_requested.wait(timeout=5)
            storage.set_task_status_atomic(self.task["id"], "stopped", db_path=self.db_path, block_on_send=False)
            stop_landed.set()

        thread = threading.Thread(target=stopper)
        with patch.object(storage, "try_begin_send", side_effect=begin_synced):
            thread.start()
            result = scheduler.check_task(
                self.task,
                fetcher=lambda url: self._quote("299.00"),
                send_fn=lambda *a: sent.append(1) or {"success": True},
                db_path=self.db_path,
            )
            thread.join(timeout=5)

        self.assertTrue(result["is_deal"])
        self.assertFalse(result["notified"])
        self.assertEqual(result["notification_cancelled_reason"], "send_permit_denied")
        self.assertEqual(sent, [])

    # G1: barrier test — the stop lands after the permit is granted, so the send
    # completes and the stop report must say an email may already be on its way.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_barrier_stop_after_permit_reports_in_flight(self, mock_retries):
        sent = []
        real_begin = storage.try_begin_send
        permit_taken = threading.Event()
        release_send = threading.Event()
        stop_outcome: list[str] = []

        def begin_synced(task_id, owner, db_path=None, **kwargs):
            granted = real_begin(task_id, owner, db_path=db_path, **kwargs)
            permit_taken.set()
            return granted

        def slow_send(email, task, quote):
            release_send.wait(timeout=5)
            sent.append(1)
            return {"success": True}

        def stopper():
            permit_taken.wait(timeout=5)
            stop_outcome.append(
                storage.set_task_status_atomic(self.task["id"], "stopped", db_path=self.db_path, block_on_send=True)
            )
            release_send.set()

        thread = threading.Thread(target=stopper)
        with patch.object(storage, "try_begin_send", side_effect=begin_synced):
            thread.start()
            result = scheduler.check_task(
                self.task,
                fetcher=lambda url: self._quote("299.00"),
                send_fn=slow_send,
                db_path=self.db_path,
            )
            thread.join(timeout=5)

        self.assertEqual(stop_outcome, ["in_flight"])
        self.assertTrue(result["notified"])
        self.assertEqual(len(sent), 1)

    # G1: a genuinely separate process sharing this database must be excluded.
    def test_cross_process_task_lock_is_exclusive(self):
        repo_root = Path(__file__).resolve().parent.parent
        child_code = (
            "import time\n"
            "from pathlib import Path\n"
            "import storage\n"
            f"db = Path({str(self.db_path)!r})\n"
            "storage.init_db(db)\n"
            f"ok = storage.acquire_task_lock({self.task['id']}, 'child-process', db_path=db)\n"
            "print('LOCKED' if ok else 'DENIED', flush=True)\n"
            "time.sleep(3)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        try:
            line = proc.stdout.readline().strip()
            self.assertEqual(line, "LOCKED", f"child stderr: {proc.stderr.read()[:400]}")
            # The parent shares the same file and must be refused.
            self.assertFalse(
                storage.acquire_task_lock(self.task["id"], "parent-process", db_path=self.db_path)
            )
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    # ---------------------------------------------------------------- G2

    # G2: a queued check that is cancelled before starting must release its slot.
    def test_cancelled_future_releases_reservation(self):
        second_id = storage.create_task(
            {k: v for k, v in self.task.items() if k != "id"}, db_path=self.db_path
        )
        storage.update_next_check(self.task["id"], "2000-01-01T00:00:00+00:00", db_path=self.db_path)
        storage.update_next_check(second_id, "2000-01-01T00:00:00+00:00", db_path=self.db_path)

        sched = scheduler.TaskScheduler(db_path=self.db_path, max_workers=1, max_backlog=4)
        sched._executor = ThreadPoolExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()
        executed: list[int] = []

        def fake_check(task, **kwargs):
            executed.append(task["id"])
            started.set()
            release.wait(timeout=5)

        executor = None
        try:
            with patch.object(scheduler, "check_task", side_effect=fake_check):
                sched._tick()
                self.assertTrue(started.wait(timeout=5))
                # One running, one queued.
                self.assertEqual(sched.pending_count(), 2)

                sched.stop()
                executor = sched._executor
                sched._executor = None
                executor.shutdown(wait=False, cancel_futures=True)
                release.set()

                deadline = time.monotonic() + 5
                while sched.pending_count() > 0 and time.monotonic() < deadline:
                    time.sleep(0.05)
        finally:
            release.set()
            if executor is not None and sched._executor is None:
                executor.shutdown(wait=True)
            if sched._executor is not None:
                sched._executor.shutdown(wait=True)
                sched._executor = None

        self.assertEqual(sched.pending_count(), 0)
        self.assertEqual(executed, [self.task["id"]])

    # G2: a stopping scheduler cancels a notification that has not started.
    @patch.object(scheduler.config, "notification_max_retries", return_value=1)
    def test_should_abort_prevents_send(self, mock_retries):
        sent = []
        result = scheduler.check_task(
            self.task,
            fetcher=lambda url: self._quote("299.00"),
            send_fn=lambda *a: sent.append(1) or {"success": True},
            db_path=self.db_path,
            should_abort=lambda: True,
        )
        self.assertTrue(result["is_deal"])
        self.assertFalse(result["notified"])
        self.assertEqual(result["notification_cancelled_reason"], "scheduler_stopping")
        self.assertEqual(sent, [])
        task = storage.get_task(self.task["id"], db_path=self.db_path)
        self.assertEqual(task["send_in_flight"], 0)

    # G2: a second scheduler instance in the same process is refused.
    def test_second_instance_in_process_is_rejected(self):
        storage.update_next_check(self.task["id"], "2999-01-01T00:00:00+00:00", db_path=self.db_path)
        first = scheduler.TaskScheduler(db_path=self.db_path)
        second = scheduler.TaskScheduler(db_path=self.db_path)
        thread = threading.Thread(target=first.run_loop, daemon=True)
        with patch.object(scheduler, "check_task"):
            thread.start()
            time.sleep(0.3)
            try:
                with self.assertRaises(scheduler.SchedulerError):
                    second.run_loop()
            finally:
                first.stop()
                thread.join(timeout=3)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()

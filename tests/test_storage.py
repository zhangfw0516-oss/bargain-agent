"""Tests for storage persistence."""

import tempfile
import unittest
from pathlib import Path

import storage


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        storage.init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _sample_task(self) -> dict:
        return {
            "product_name": "Kindle",
            "url": "https://www.pbtech.co.nz/kindle",
            "target_price": "300.00",
            "currency": "NZD",
            "comparison": "lt",
            "frequency_minutes": 60,
            "condition": None,
            "notify_email": "user@example.com",
            "status": "active",
        }

    def test_init_db(self):
        tables = storage._fetchall(self.db_path, "SELECT name FROM sqlite_master WHERE type='table'")
        names = {t["name"] for t in tables}
        self.assertIn("tasks", names)
        self.assertIn("quotes", names)
        self.assertIn("notification_events", names)

    def test_create_and_get_task(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        task = storage.get_task(task_id, db_path=self.db_path)
        self.assertIsNotNone(task)
        self.assertEqual(task["product_name"], "Kindle")
        self.assertEqual(task["status"], "active")

    def test_list_tasks(self):
        storage.create_task(self._sample_task(), db_path=self.db_path)
        storage.create_task({**self._sample_task(), "status": "paused"}, db_path=self.db_path)
        active = storage.list_tasks(status="active", db_path=self.db_path)
        self.assertEqual(len(active), 1)
        all_tasks = storage.list_tasks(db_path=self.db_path)
        self.assertEqual(len(all_tasks), 2)

    def test_quote_and_event_lifecycle(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        quote_id = storage.save_quote(
            task_id,
            {
                "fetched_at": "2026-09-23T04:00:00+00:00",
                "price": "299.00",
                "currency": "NZD",
                "in_stock": True,
                "product_name": "Kindle",
                "retailer": "pbtech.co.nz",
                "url": "https://www.pbtech.co.nz/kindle",
            },
            db_path=self.db_path,
        )
        quote = storage.get_quote(quote_id, db_path=self.db_path)
        self.assertEqual(quote["price"], "299.00")
        event_id = storage.create_event(task_id, quote_id, db_path=self.db_path)
        storage.update_event(event_id, "sent", attempts=1, sent_at="2026-09-23T04:01:00+00:00", db_path=self.db_path)
        event = storage.list_events(task_id=task_id, db_path=self.db_path)[0]
        self.assertEqual(event["status"], "sent")

    def test_reset_active_events(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        event_id = storage.create_event(task_id, None, db_path=self.db_path)
        storage.reset_active_events(task_id, db_path=self.db_path)
        event = storage.get_active_event(task_id, db_path=self.db_path)
        self.assertIsNone(event)
        rows = storage.list_events(task_id=task_id, db_path=self.db_path)
        self.assertEqual(rows[0]["status"], "reset")

    # R2: failed events remain visible to the scheduler for budget accounting.
    def test_get_active_event_includes_failed(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        event_id = storage.create_event(task_id, None, db_path=self.db_path)
        storage.update_event(event_id, "failed", attempts=3, error="exhausted", db_path=self.db_path)
        event = storage.get_active_event(task_id, db_path=self.db_path)
        self.assertIsNotNone(event)
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["attempts"], 3)

    # ---------------------------------------------------------------- F1

    # F1: a verified exit must also end a failed event, otherwise the exhausted
    # budget would block every future deal for that task.
    def test_reset_active_events_includes_failed(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        event_id = storage.create_event(task_id, None, db_path=self.db_path)
        storage.update_event(event_id, "failed", attempts=1, error="exhausted", db_path=self.db_path)

        count = storage.reset_active_events(task_id, db_path=self.db_path)
        self.assertEqual(count, 1)
        self.assertIsNone(storage.get_active_event(task_id, db_path=self.db_path))
        self.assertEqual(storage.list_events(task_id=task_id, db_path=self.db_path)[0]["status"], "reset")

    # ---------------------------------------------------------------- F2

    # F2: the task lock is exclusive and only its owner can release it.
    def test_task_lock_is_exclusive(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        self.assertTrue(storage.acquire_task_lock(task_id, "owner-a", db_path=self.db_path))
        self.assertFalse(storage.acquire_task_lock(task_id, "owner-b", db_path=self.db_path))
        # A non-owner release is a no-op.
        storage.release_task_lock(task_id, "owner-b", db_path=self.db_path)
        self.assertFalse(storage.acquire_task_lock(task_id, "owner-c", db_path=self.db_path))
        storage.release_task_lock(task_id, "owner-a", db_path=self.db_path)
        self.assertTrue(storage.acquire_task_lock(task_id, "owner-d", db_path=self.db_path))

    # F2: a lock abandoned by a dead process is reclaimed after the stale window.
    def test_stale_task_lock_is_reclaimed(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        self.assertTrue(storage.acquire_task_lock(task_id, "dead-owner", db_path=self.db_path, stale_seconds=0))
        self.assertTrue(storage.acquire_task_lock(task_id, "new-owner", db_path=self.db_path, stale_seconds=0))
        locks = storage.list_task_locks(db_path=self.db_path)
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["owner"], "new-owner")

    # F2: locks for different tasks do not block each other.
    def test_task_locks_are_per_task(self):
        first = storage.create_task(self._sample_task(), db_path=self.db_path)
        second = storage.create_task(self._sample_task(), db_path=self.db_path)
        self.assertTrue(storage.acquire_task_lock(first, "owner-a", db_path=self.db_path))
        self.assertTrue(storage.acquire_task_lock(second, "owner-b", db_path=self.db_path))

    # ---------------------------------------------------------------- G1

    # G1: the send permit is only granted to an active task.
    def test_try_begin_send_requires_active_task(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        storage.update_task_status(task_id, "paused", db_path=self.db_path)
        self.assertFalse(storage.try_begin_send(task_id, "owner-a", db_path=self.db_path))
        self.assertEqual(
            storage.get_task(task_id, db_path=self.db_path)["send_in_flight"], 0
        )

    def test_end_send_releases_permit_idempotently(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        self.assertTrue(storage.try_begin_send(task_id, "owner-a", db_path=self.db_path))
        self.assertTrue(storage.end_send(task_id, "owner-a", db_path=self.db_path))
        # A second release by the same owner is a no-op rather than an error.
        self.assertFalse(storage.end_send(task_id, "owner-a", db_path=self.db_path))
        self.assertTrue(storage.try_begin_send(task_id, "owner-b", db_path=self.db_path))

    # ---------------------------------------------------------------- H1

    # H1: a stale holder must not be able to release a newer permit.
    def test_old_owner_cannot_release_a_newer_permit(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        self.assertTrue(storage.try_begin_send(task_id, "owner-old", db_path=self.db_path))
        self.assertEqual(storage.clear_send_permit(task_id, db_path=self.db_path), "cleared")
        self.assertTrue(storage.try_begin_send(task_id, "owner-new", db_path=self.db_path))

        self.assertFalse(storage.end_send(task_id, "owner-old", db_path=self.db_path))
        permit = storage.get_send_permit(task_id, db_path=self.db_path)
        self.assertEqual(permit["send_in_flight"], 1)
        self.assertEqual(permit["send_owner"], "owner-new")

    def test_clear_send_permit_reports_its_state(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        self.assertEqual(storage.clear_send_permit(task_id, db_path=self.db_path), "not_in_flight")
        self.assertTrue(storage.try_begin_send(task_id, "owner-a", db_path=self.db_path))
        self.assertEqual(storage.clear_send_permit(task_id, db_path=self.db_path), "cleared")
        self.assertEqual(storage.clear_send_permit(task_id, db_path=self.db_path), "not_in_flight")
        self.assertEqual(storage.clear_send_permit(999, db_path=self.db_path), "missing")

    # H1: recovery must not hand sending rights back to a stopped task.
    def test_recovery_does_not_restore_send_rights_to_a_stopped_task(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        self.assertTrue(storage.try_begin_send(task_id, "owner-a", db_path=self.db_path))
        storage.set_task_status_atomic(task_id, "stopped", db_path=self.db_path, block_on_send=True)
        self.assertEqual(storage.clear_send_permit(task_id, db_path=self.db_path), "cleared")
        self.assertFalse(storage.try_begin_send(task_id, "owner-b", db_path=self.db_path))

    # H1: a database created before the send columns must migrate cleanly.
    def test_migration_adds_send_columns_to_an_older_database(self):
        import sqlite3 as _sqlite3

        old_path = self.db_path.with_suffix(".old.db")
        conn = _sqlite3.connect(str(old_path))
        try:
            conn.executescript(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, product_name TEXT NOT NULL, url TEXT NOT NULL,
                    target_price TEXT NOT NULL, currency TEXT NOT NULL,
                    comparison TEXT NOT NULL, frequency_minutes INTEGER NOT NULL,
                    condition TEXT, notify_email TEXT NOT NULL,
                    next_check_at TEXT NOT NULL, last_check_at TEXT,
                    last_quote_id INTEGER, last_event_id INTEGER
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        try:
            storage.init_db(old_path)
            task_id = storage.create_task(self._sample_task(), db_path=old_path)
            self.assertTrue(storage.try_begin_send(task_id, "owner-a", db_path=old_path))
            self.assertTrue(storage.end_send(task_id, "owner-a", db_path=old_path))
        finally:
            old_path.unlink(missing_ok=True)

    def test_set_task_status_atomic_reports_missing_task(self):
        self.assertEqual(
            storage.set_task_status_atomic(999, "stopped", db_path=self.db_path), "missing"
        )

    def test_set_task_status_atomic_without_blocking_on_send(self):
        task_id = storage.create_task(self._sample_task(), db_path=self.db_path)
        self.assertTrue(storage.try_begin_send(task_id, "owner-a", db_path=self.db_path))
        outcome = storage.set_task_status_atomic(
            task_id, "stopped", db_path=self.db_path, block_on_send=False
        )
        self.assertEqual(outcome, "changed")
        self.assertEqual(storage.get_task(task_id, db_path=self.db_path)["status"], "stopped")


if __name__ == "__main__":
    unittest.main()

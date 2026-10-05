"""Tests for cli.py command handlers."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
import storage


class _FakeLLMClient:
    def __init__(self, payload: dict):
        self.chat = self
        self.completions = self
        self.payload = payload

    def create(self, **kwargs):
        import json

        class Msg:
            content = json.dumps(self.payload)

        class Choice:
            finish_reason = "stop"
            message = Msg()

        class Resp:
            choices = [Choice()]

        return Resp()


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _sample_payload(self) -> dict:
        return {
            "schema_version": "1",
            "status": "ready",
            "product_name": "Kindle Paperwhite 16GB",
            "url": "https://www.pbtech.co.nz/kindle",
            "target_price": "300.00",
            "currency": "NZD",
            "comparison": "lt",
            "frequency_minutes": 60,
            "condition": None,
            "missing_fields": [],
            "clarification_question": None,
        }

    # R3: add without confirmation is rejected.
    def test_add_requires_confirmation(self):
        with patch("cli.config._get", return_value="user@example.com"):
            with patch("cli.task_parser.run") as mock_parse:
                mock_parse.return_value = self._sample_payload()
                with patch("sys.stdout", new=io.StringIO()) as stdout, patch("sys.stdin", new=io.StringIO("n\n")):
                    rc = cli.main(["--db-path", str(self.db_path), "add", "Monitor kindle"])
        self.assertEqual(rc, 3)
        self.assertIn("cancelled", stdout.getvalue())
        tasks = self._tasks()
        self.assertEqual(len(tasks), 0)

    # R3: add with confirmation creates the task.
    def test_add_confirmed_creates_task(self):
        with patch("cli.config._get", return_value="user@example.com"):
            with patch("cli.task_parser.run") as mock_parse:
                mock_parse.return_value = self._sample_payload()
                with patch("sys.stdout", new=io.StringIO()), patch("sys.stdin", new=io.StringIO("y\n")):
                    rc = cli.main(["--db-path", str(self.db_path), "add", "Monitor kindle"])
        self.assertEqual(rc, 0)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "active")

    # R3: --yes skips interactive confirmation.
    def test_add_yes_flag(self):
        with patch("cli.config._get", return_value="user@example.com"):
            with patch("cli.task_parser.run") as mock_parse:
                mock_parse.return_value = self._sample_payload()
                rc = cli.main(["--db-path", str(self.db_path), "add", "--yes", "Monitor kindle"])
        self.assertEqual(rc, 0)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)

    # R3: unsupported/needs_clarification is not created.
    def test_add_needs_clarification_not_created(self):
        payload = {**self._sample_payload(), "status": "needs_clarification", "missing_fields": ["frequency_minutes"], "clarification_question": "How often?"}
        with patch("cli.task_parser.run") as mock_parse:
            mock_parse.return_value = payload
            with patch("sys.stdout", new=io.StringIO()):
                rc = cli.main(["--db-path", str(self.db_path), "add", "Monitor kindle"])
        self.assertEqual(rc, 2)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 0)

    # R4: list on a fresh database does not crash.
    def test_list_empty_db(self):
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = cli.main(["--db-path", str(self.db_path), "list"])
        self.assertEqual(rc, 0)
        self.assertIn("No tasks found", stdout.getvalue())

    # R4: updating a missing task returns non-zero.
    def test_pause_missing_task(self):
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "pause", "42"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", stderr.getvalue())

    def test_resume_missing_task(self):
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "resume", "42"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", stderr.getvalue())

    def test_stop_missing_task(self):
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "stop", "42"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", stderr.getvalue())

    # ---------------------------------------------------------------- F6

    # F6: a new/used/refurbished condition cannot be verified, so it must block
    # task creation rather than being silently ignored.
    def test_add_with_condition_is_blocked(self):
        payload = {**self._sample_payload(), "condition": "new"}
        with patch("cli.config._get", return_value="user@example.com"):
            with patch("cli.task_parser.run", return_value=payload):
                with patch("sys.stderr", new=io.StringIO()) as stderr:
                    rc = cli.main(["--db-path", str(self.db_path), "add", "--yes", "Monitor a new Kindle"])
        self.assertEqual(rc, 4)
        self.assertIn("not supported", stderr.getvalue())
        self.assertEqual(self._tasks(), [])

    # F6: an explicit "any" condition is acceptable (nothing to verify).
    def test_add_with_any_condition_is_allowed(self):
        payload = {**self._sample_payload(), "condition": "any"}
        with patch("cli.config._get", return_value="user@example.com"):
            with patch("cli.task_parser.run", return_value=payload):
                rc = cli.main(["--db-path", str(self.db_path), "add", "--yes", "Monitor a Kindle"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._tasks()), 1)

    # ---------------------------------------------------------------- G1

    def _tasks(self) -> list:
        # The DB may not have been created yet when nothing was written.
        storage.init_db(self.db_path)
        return storage.list_tasks(db_path=self.db_path)

    def _create_task(self) -> int:
        storage.init_db(self.db_path)
        return storage.create_task(
            {
                "product_name": "Kindle",
                "url": "https://www.pbtech.co.nz/product/Kindle",
                "target_price": "300.00",
                "currency": "NZD",
                "comparison": "lt",
                "frequency_minutes": 60,
                "condition": None,
                "notify_email": "user@example.com",
                "status": "active",
            },
            db_path=self.db_path,
        )

    # G1: a stop racing an in-flight send must be reported honestly.
    def test_stop_reports_in_flight_send(self):
        task_id = self._create_task()
        self.assertTrue(storage.try_begin_send(task_id, "owner-a", db_path=self.db_path))
        try:
            with patch("sys.stdout", new=io.StringIO()) as stdout, patch("sys.stderr", new=io.StringIO()) as stderr:
                rc = cli.main(["--db-path", str(self.db_path), "stop", str(task_id)])
        finally:
            storage.end_send(task_id, "owner-a", db_path=self.db_path)

        self.assertEqual(rc, 0)
        self.assertIn("stopped", stdout.getvalue())
        self.assertIn("may still be delivered", stderr.getvalue())
        self.assertEqual(storage.get_task(task_id, db_path=self.db_path)["status"], "stopped")

    # G1: a clean stop must not print the in-flight warning.
    def test_stop_without_in_flight_send_is_clean(self):
        task_id = self._create_task()
        with patch("sys.stdout", new=io.StringIO()) as stdout, patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "stop", str(task_id)])
        self.assertEqual(rc, 0)
        self.assertIn("stopped", stdout.getvalue())
        self.assertNotIn("may still be delivered", stderr.getvalue())

    # G1: pause uses the same boundary and reports it the same way.
    def test_pause_reports_in_flight_send(self):
        task_id = self._create_task()
        self.assertTrue(storage.try_begin_send(task_id, "owner-a", db_path=self.db_path))
        try:
            with patch("sys.stdout", new=io.StringIO()) as stdout, patch("sys.stderr", new=io.StringIO()) as stderr:
                rc = cli.main(["--db-path", str(self.db_path), "pause", str(task_id)])
        finally:
            storage.end_send(task_id, "owner-a", db_path=self.db_path)

        self.assertEqual(rc, 0)
        self.assertIn("paused", stdout.getvalue())
        self.assertIn("may still be delivered", stderr.getvalue())

    # ---------------------------------------------------------------- H1

    def test_release_send_clears_a_stale_permit(self):
        task_id = self._create_task()
        self.assertTrue(storage.try_begin_send(task_id, "dead-process", db_path=self.db_path))
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = cli.main(["--db-path", str(self.db_path), "release-send", "--yes", str(task_id)])
        self.assertEqual(rc, 0)
        self.assertIn("released", stdout.getvalue())
        self.assertEqual(storage.get_send_permit(task_id, db_path=self.db_path)["send_in_flight"], 0)
        # Recovery must not change the task's own status.
        self.assertEqual(storage.get_task(task_id, db_path=self.db_path)["status"], "active")

    def test_release_send_is_a_noop_without_a_permit(self):
        task_id = self._create_task()
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = cli.main(["--db-path", str(self.db_path), "release-send", "--yes", str(task_id)])
        self.assertEqual(rc, 0)
        self.assertIn("no send permit", stdout.getvalue())

    def test_release_send_requires_confirmation_without_yes(self):
        task_id = self._create_task()
        self.assertTrue(storage.try_begin_send(task_id, "dead-process", db_path=self.db_path))
        with patch("sys.stdout", new=io.StringIO()) as stdout, patch("sys.stdin", new=io.StringIO("n\n")):
            rc = cli.main(["--db-path", str(self.db_path), "release-send", str(task_id)])
        self.assertEqual(rc, 3)
        self.assertIn("cancelled", stdout.getvalue())
        self.assertEqual(storage.get_send_permit(task_id, db_path=self.db_path)["send_in_flight"], 1)

    # ------------------------------------------------ Stage B closeout / Stage C: CLI
    def test_sources_lists_a_tasks_sources(self):
        task_id = self._create_task()
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = cli.main(["--db-path", str(self.db_path), "sources", str(task_id)])
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn(f"Task {task_id}", out)
        self.assertIn("Sources (1)", out)
        self.assertIn("pbtech.co.nz", out)

    def test_sources_disable_reports_when_no_source_is_left(self):
        task_id = self._create_task()
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "sources", str(task_id), "--disable", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("no enabled sources", stderr.getvalue())

    def test_sources_rejects_a_source_from_another_task(self):
        first = self._create_task()
        second = self._create_task()
        foreign = storage.list_task_sources(second, db_path=self.db_path)[0]["id"]
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "sources", str(first), "--disable", str(foreign)])
        self.assertEqual(rc, 1)
        self.assertIn("belongs to task", stderr.getvalue())

    def test_sources_for_a_missing_task(self):
        storage.init_db(self.db_path)
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "sources", "42"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", stderr.getvalue())

    def test_history_reports_no_observations_yet(self):
        task_id = self._create_task()
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = cli.main(["--db-path", str(self.db_path), "history", str(task_id)])
        self.assertEqual(rc, 0)
        self.assertIn("no observations recorded yet", stdout.getvalue())

    def test_history_lists_observations_newest_first(self):
        task_id = self._create_task()
        source = storage.list_task_sources(task_id, db_path=self.db_path)[0]
        for price, when in (("260.00", "2026-09-24T01:00:00+00:00"), ("239.00", "2026-09-24T02:00:00+00:00")):
            storage.save_quote(
                task_id,
                {"price": price, "currency": "NZD", "in_stock": True, "fetched_at": when,
                 "retailer": "pbtech.co.nz", "url": "u"},
                db_path=self.db_path,
                source_id=source["id"],
            )
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = cli.main(["--db-path", str(self.db_path), "history", str(task_id)])
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("239.00", out)
        self.assertIn("260.00", out)
        # Newest first.
        self.assertLess(out.index("239.00"), out.index("260.00"))

    def test_history_rejects_a_non_positive_limit(self):
        task_id = self._create_task()
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "history", str(task_id), "--limit", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("--limit", stderr.getvalue())

    def test_modify_target_price_ends_the_current_episode(self):
        task_id = self._create_task()
        storage.create_event(task_id, None, db_path=self.db_path)
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = cli.main(
                ["--db-path", str(self.db_path), "modify", str(task_id), "--target-price", "180.00"]
            )
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        # The reset is atomic with the threshold change (config_version bump +
        # episode end in one transaction), so the CLI no longer prints a count.
        self.assertIn("Ended the active notification episode", out)
        self.assertEqual(self._tasks()[0]["target_price"], "180.00")
        self.assertEqual(
            [e["status"] for e in storage.list_events(task_id=task_id, db_path=self.db_path)], ["reset"]
        )

    def test_modify_frequency_does_not_touch_events(self):
        task_id = self._create_task()
        storage.create_event(task_id, None, db_path=self.db_path)
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = cli.main(
                ["--db-path", str(self.db_path), "modify", str(task_id), "--frequency-minutes", "15"]
            )
        self.assertEqual(rc, 0)
        self.assertIn("will not be re-sent", stdout.getvalue())
        self.assertEqual(self._tasks()[0]["frequency_minutes"], 15)
        self.assertEqual(
            [e["status"] for e in storage.list_events(task_id=task_id, db_path=self.db_path)], ["pending"]
        )

    def test_modify_rejects_a_non_positive_price(self):
        task_id = self._create_task()
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "modify", str(task_id), "--target-price", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("positive number", stderr.getvalue())
        self.assertEqual(self._tasks()[0]["target_price"], "300.00")

    def test_modify_rejects_a_non_numeric_price(self):
        task_id = self._create_task()
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "modify", str(task_id), "--target-price", "cheap"])
        self.assertEqual(rc, 1)
        self.assertIn("positive number", stderr.getvalue())

    def test_modify_rejects_a_non_positive_frequency(self):
        task_id = self._create_task()
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "modify", str(task_id), "--frequency-minutes", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("positive number", stderr.getvalue())

    def test_modify_without_any_field_is_an_error(self):
        task_id = self._create_task()
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "modify", str(task_id)])
        self.assertEqual(rc, 1)
        self.assertIn("nothing to change", stderr.getvalue())

    def test_modify_a_missing_task(self):
        storage.init_db(self.db_path)
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "modify", "42", "--target-price", "10"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", stderr.getvalue())

    def test_release_send_reports_missing_task(self):
        storage.init_db(self.db_path)
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = cli.main(["--db-path", str(self.db_path), "release-send", "--yes", "42"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", stderr.getvalue())

    # ---------------------------------------------------------------- find

    FIND_RESULT = {
        "status": "ready", "description": "fitbit", "need": {"budget": "250.00", "currency": "NZD"},
        "queries": ["fitbit charge 6"], "stages": {"select": "ok"}, "notes": [], "evidence": [], "usage": {},
        "recommendations": [
            {"id": "e1", "title": "Fitbit Charge 6", "link": "https://www.google.co.nz/shopping/product/111",
             "merchant": "Noel Leeming", "price": "229.00", "currency": "NZD", "product_id": "111",
             "page_token": "tok", "search_query": "fitbit charge 6", "monitorable": True, "trusted": True,
             "summary": "好手环"},
            {"id": "e2", "title": "Fitbit Inspire", "link": "https://small.example.nz/inspire",
             "merchant": "small.example.nz", "price": "99.00", "currency": "NZD", "product_id": None,
             "monitorable": False, "trusted": False},
        ],
    }

    def _run_find(self, argv_extra, result=None, stdin=""):
        import copy
        from types import SimpleNamespace

        from sources.base import Offer, ProductRef

        detail = SimpleNamespace(offers=lambda ref: ([Offer("JB Hi-Fi", "219.00", "NZD", "https://jb.example.nz/p", "serpapi")],
                                                     ProductRef(ref.product_id, "tok2", ref.search_query, ref.title)))
        providers = SimpleNamespace(shopping=None, supplement=None, offers=None, detail=detail)
        import os

        with patch.dict(os.environ, {"DEFAULT_NOTIFY_EMAIL": "user@example.com"}), \
                patch("sources.registry.build_providers", return_value=providers), \
                patch("cli.pipeline.run_find", return_value=copy.deepcopy(result or self.FIND_RESULT)), \
                patch("sys.stdin", new=io.StringIO(stdin)):
            return cli.main(["--db-path", str(self.db_path), "find", *argv_extra])

    def test_find_lists_recommendations_and_creates_a_product_task(self):
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_find(["--select", "1", "--yes", "fitbit under 250"])
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("1. Fitbit Charge 6", out)
        self.assertIn("[unverified merchant]", out)
        self.assertIn("JB Hi-Fi: NZD 219.00", out)
        self.assertIn("Watching: Noel Leeming", out)
        task = self._tasks()[0]
        self.assertEqual((task["target_price"], task["currency"]), ("250.00", "NZD"))
        self.assertGreaterEqual(task["frequency_minutes"], 1440)
        source = storage.list_task_sources(task["id"], db_path=self.db_path)[0]
        self.assertEqual((source["source_kind"], source["product_id"], source["page_token"]), ("serpapi_product", "111", "tok2"))

    def test_find_watches_preselected_same_product_matches(self):
        evidence = [
            {"id": "e3", "title": "Fitbit Charge 6 Obsidian", "merchant": "PB Tech", "price": "198.00", "currency": "NZD",
             "product_id": "333", "link": "https://g.example/333"},
            {"id": "e4", "title": "Fitbit Charge 6", "merchant": "Random Shop", "price": "190.00", "currency": "NZD",
             "product_id": "444", "link": "https://g.example/444"},
        ]
        result = {**self.FIND_RESULT, "evidence": evidence}
        with patch("cli.scheduler.default_verifier", return_value=None), patch("sys.stdout", new=io.StringIO()) as stdout:
            self.assertEqual(self._run_find(["--select", "1", "--yes", "fitbit"], result=result), 0)
        self.assertIn("[+] PB Tech", stdout.getvalue())
        sources = storage.list_task_sources(self._tasks()[0]["id"], db_path=self.db_path)
        self.assertEqual([s["retailer"] for s in sources], ["Noel Leeming", "PB Tech"])
        self.assertEqual({s["search_query"] for s in sources}, {"Fitbit Charge 6"})

    def test_find_no_matches_flag(self):
        evidence = [{"id": "e3", "title": "Fitbit Charge 6", "merchant": "PB Tech", "price": "198.00", "currency": "NZD",
                     "product_id": "333", "link": "https://g.example/333"}]
        with patch("cli.scheduler.default_verifier", return_value=None), patch("sys.stdout", new=io.StringIO()):
            self._run_find(["--select", "1", "--yes", "--no-matches", "fitbit"], result={**self.FIND_RESULT, "evidence": evidence})
        self.assertEqual(len(storage.list_task_sources(self._tasks()[0]["id"], db_path=self.db_path)), 1)

    def test_check_due_prints_and_logs_a_summary(self):
        log = self.db_path.with_suffix(".log")
        results = [{"task_id": 1, "is_deal": True, "notified": True, "chosen_retailer": "PB Tech", "chosen_price": "198.00",
                    "sources_checked": 2, "sources_failed": 0},
                   {"task_id": 2, "skipped": True, "reason": "lock_held"}]
        with patch("cli.scheduler.check_due_tasks", return_value=results), patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = cli.main(["--db-path", str(self.db_path), "check-due", "--log", str(log)])
        self.assertEqual(rc, 0)
        self.assertIn("task 1: deal PB Tech 198.00, email sent (2 source(s))", stdout.getvalue())
        self.assertIn("task 2: skipped (lock_held)", log.read_text(encoding="utf-8"))
        log.unlink()
        with patch("cli.scheduler.check_due_tasks", return_value=[{"task_id": 3, "error": "x"}]), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(cli.main(["--db-path", str(self.db_path), "check-due"]), 1)

    def test_find_and_modify_drop_percent(self):
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(self._run_find(["--select", "1", "--yes", "--drop-percent", "15", "fitbit"]), 0)
        tid = self._tasks()[0]["id"]
        self.assertEqual(storage.get_task(tid, db_path=self.db_path)["drop_percent"], "15")
        with patch("sys.stdout", new=io.StringIO()):
            cli.main(["--db-path", str(self.db_path), "modify", str(tid), "--drop-percent", "0"])
        self.assertIsNone(storage.get_task(tid, db_path=self.db_path)["drop_percent"])
        with patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(cli.main(["--db-path", str(self.db_path), "modify", str(tid), "--drop-percent", "95"]), 1)

    def test_find_single_listing_becomes_a_url_source(self):
        with patch("sys.stdout", new=io.StringIO()):
            rc = self._run_find(["--select", "2", "--yes", "--target-price", "90", "fitbit"])
        self.assertEqual(rc, 0)
        source = storage.list_task_sources(self._tasks()[0]["id"], db_path=self.db_path)[0]
        self.assertEqual((source["source_kind"], source["url"]), ("url", "https://small.example.nz/inspire"))

    def test_find_creates_nothing_when_cancelled_or_declined(self):
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            self.assertEqual(self._run_find(["fitbit"], stdin=chr(10)), 3)
        self.assertIn("nothing was created", stdout.getvalue())
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(self._run_find(["--select", "1", "fitbit"], stdin="n" + chr(10)), 3)
        self.assertEqual(self._tasks(), [])

    def test_find_rejects_bad_selection_and_missing_target(self):
        with patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(self._run_find(["--select", "9", "--yes", "fitbit"]), 1)
            no_budget = {**self.FIND_RESULT, "need": {}}
            self.assertEqual(self._run_find(["--select", "1", "--yes", "fitbit"], result=no_budget), 2)
        self.assertEqual(self._tasks(), [])

    def test_find_without_recommendations(self):
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_find(["--yes", "zzz"], result={**self.FIND_RESULT, "recommendations": []})
        self.assertEqual(rc, 2)
        self.assertIn("No suitable product", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

"""Tests for cli.py command handlers."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
import discovery
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
            with patch("cli.agent.parse_user_instruction") as mock_parse:
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
            with patch("cli.agent.parse_user_instruction") as mock_parse:
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
            with patch("cli.agent.parse_user_instruction") as mock_parse:
                mock_parse.return_value = self._sample_payload()
                rc = cli.main(["--db-path", str(self.db_path), "add", "--yes", "Monitor kindle"])
        self.assertEqual(rc, 0)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)

    # R3: unsupported/needs_clarification is not created.
    def test_add_needs_clarification_not_created(self):
        payload = {**self._sample_payload(), "status": "needs_clarification", "missing_fields": ["frequency_minutes"], "clarification_question": "How often?"}
        with patch("cli.agent.parse_user_instruction") as mock_parse:
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
            with patch("cli.agent.parse_user_instruction", return_value=payload):
                with patch("sys.stderr", new=io.StringIO()) as stderr:
                    rc = cli.main(["--db-path", str(self.db_path), "add", "--yes", "Monitor a new Kindle"])
        self.assertEqual(rc, 4)
        self.assertIn("not supported", stderr.getvalue())
        self.assertEqual(self._tasks(), [])

    # F6: an explicit "any" condition is acceptable (nothing to verify).
    def test_add_with_any_condition_is_allowed(self):
        payload = {**self._sample_payload(), "condition": "any"}
        with patch("cli.config._get", return_value="user@example.com"):
            with patch("cli.agent.parse_user_instruction", return_value=payload):
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

    def test_search_reports_sources_and_creates_a_multi_source_task(self):
        pb = self._offer(retailer="PB Tech", url="https://www.pbtech.co.nz/product/air")
        jb = self._offer(
            retailer="JB Hi-Fi", url="https://www.jbhifi.co.nz/products/air", title="Google Fitbit Air (Obsidian)"
        )
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_search(
                ["--select", "1,2", "--yes", "find Fitbit Air"],
                result=self._result(offers=[pb, jb]),
            )
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("Sources to monitor (2)", out)
        self.assertIn("Created task", out)

        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        sources = storage.list_task_sources(tasks[0]["id"], db_path=self.db_path)
        self.assertEqual([s["retailer"] for s in sources], ["PB Tech", "JB Hi-Fi"])

    def test_search_cannot_monitor_an_unconfirmed_offer(self):
        # The Stage B gap: the user asked for black, this listing is "Berry".
        berry = self._offer(
            retailer="PB Tech",
            url="https://www.pbtech.co.nz/product/air-berry",
            title="Google Fitbit Air Fitness Tracker - Berry",
            match=discovery.MATCH_NEEDS_CONFIRMATION,
        )
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = self._run_search(["--select", "1", "--yes", "find Fitbit Air black"], result=self._result(offers=[berry]))
        self.assertEqual(rc, 1)
        self.assertIn("could not be confirmed", stderr.getvalue())
        self.assertEqual(self._tasks(), [])

    def test_unconfirmed_offer_is_shown_but_not_offered_as_a_choice(self):
        obsidian = self._offer(url="https://www.pbtech.co.nz/product/air")
        berry = self._offer(
            url="https://www.pbtech.co.nz/product/air-berry",
            title="Google Fitbit Air Fitness Tracker - Berry",
            match=discovery.MATCH_NEEDS_CONFIRMATION,
        )
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_search(["find Fitbit Air black"], result=self._result(offers=[obsidian, berry]))
        self.assertEqual(rc, 3)  # no selection made; nothing created
        out = stdout.getvalue()
        self.assertIn("NOT SELECTABLE", out)
        self.assertIn("Berry", out)
        self.assertEqual(self._tasks(), [])

    def test_search_refuses_mixing_two_colours_in_one_task(self):
        obsidian = self._offer(url="https://www.pbtech.co.nz/product/air", title="Google Fitbit Air (Obsidian)")
        berry = self._offer(
            retailer="JB Hi-Fi",
            url="https://www.jbhifi.co.nz/products/air-berry",
            title="Google Fitbit Air (Berry)",
        )
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = self._run_search(
                ["--select", "1,2", "--yes", "find Fitbit Air"], result=self._result(offers=[obsidian, berry])
            )
        self.assertEqual(rc, 1)
        self.assertIn("same product variant", stderr.getvalue())
        self.assertEqual(self._tasks(), [])

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

    # ---------------------------------------------------------------- Stage B

    def _request(self, **overrides) -> dict:
        base = {
            "schema_version": "2",
            "status": "ready",
            "product_name": "Fitbit Air",
            "url": None,
            "budget": "200.00",
            "currency": "NZD",
            "colour": "black",
            "capacity": None,
            "condition": None,
            "comparison": "lte",
            "frequency_minutes": 60,
            "missing_fields": [],
            "clarification_question": None,
        }
        base.update(overrides)
        return base

    def _offer(self, **overrides) -> dict:
        base = {
            "retailer": "PB Tech",
            "title": "Google Fitbit Air Fitness Tracker - Obsidian",
            "url": "https://www.pbtech.co.nz/product/WTHFIT114043/Google-Fitbit-Air-Fitness-Tracker---Obsidian---Up",
            "price": "239.00",
            "currency": "NZD",
            "in_stock": True,
            "shipping_cost": None,
            "shipping_known": False,
            "condition": None,
            "queried_at": "2026-09-23T00:00:00+00:00",
            "within_budget": False,
            "buyable": True,
            "excluded_reason": None,
            # Only an explicit `same` is selectable; the default stands in for a
            # real discovery result row whose match verdict was confirmed.
            "match": discovery.MATCH_SAME,
            "notes": ["Shipping cost not stated; not included in the price shown."],
        }
        base.update(overrides)
        return base

    def _result(self, offers=None, **overrides) -> dict:
        offers = offers if offers is not None else [self._offer()]
        base = {
            "query": "Fitbit Air",
            "currency": "NZD",
            "budget": "200.00",
            "queried_at": "2026-09-23T00:00:00+00:00",
            "sources": [{"retailer": "PB Tech", "status": "ok", "candidates": 1, "error": None}],
            "candidates": [],
            "offers": offers,
            "notes": [],
            "counts": {"candidates": 1, "same": 1, "needs_confirmation": 0, "different": 0, "offers": len(offers)},
        }
        base.update(overrides)
        return base

    def _run_search(self, argv_extra, request=None, result=None):
        request = request if request is not None else self._request()
        result = result if result is not None else self._result()
        with patch("cli.config._get", return_value="user@example.com"):
            with patch("cli.agent.parse_shopping_request", return_value=request):
                with patch("cli.discovery.search_and_compare", return_value=result):
                    return cli.main(["--db-path", str(self.db_path), "search", *argv_extra])

    def test_search_shows_comparison_and_creates_a_monitor_task_on_confirmation(self):
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_search(["--select", "1", "--yes", "find Fitbit Air"])
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("PB Tech", out)
        self.assertIn("NZD 239.00", out)
        self.assertIn("Shipping cost not stated", out)
        self.assertIn("Created task", out)

        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["url"], self._offer()["url"])
        self.assertEqual(task["target_price"], "200.00")
        self.assertEqual(task["currency"], "NZD")
        self.assertEqual(task["frequency_minutes"], 60)
        self.assertEqual(task["status"], "active")

    def test_search_creates_nothing_when_the_selection_is_cancelled(self):
        with patch("sys.stdout", new=io.StringIO()) as stdout, patch("sys.stdin", new=io.StringIO("\n")):
            rc = self._run_search(["find Fitbit Air"])
        self.assertEqual(rc, 3)
        self.assertIn("nothing was created", stdout.getvalue())
        self.assertEqual(self._tasks(), [])

    def test_search_creates_nothing_when_confirmation_is_declined(self):
        with patch("sys.stdout", new=io.StringIO()) as stdout, patch("sys.stdin", new=io.StringIO("n\n")):
            rc = self._run_search(["--select", "1", "find Fitbit Air"])
        self.assertEqual(rc, 3)
        self.assertIn("cancelled", stdout.getvalue().lower())
        self.assertEqual(self._tasks(), [])

    def test_search_needing_clarification_creates_nothing(self):
        request = self._request(status="needs_clarification", frequency_minutes=None, missing_fields=["frequency_minutes"])
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_search(["find Fitbit Air"], request=request)
        self.assertEqual(rc, 2)
        self.assertIn("Need more information", stdout.getvalue())
        self.assertEqual(self._tasks(), [])

    def test_search_with_a_url_points_at_the_add_flow(self):
        request = self._request(url="https://www.pbtech.co.nz/product/WTHFIT114043/Google-Fitbit-Air-Fitness-Tracker---Obsidian---Up")
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            with patch("cli.agent.parse_shopping_request", return_value=request):
                rc = cli.main(["--db-path", str(self.db_path), "search", "monitor this URL"])
        self.assertEqual(rc, 2)
        self.assertIn("add", stderr.getvalue())
        self.assertEqual(self._tasks(), [])

    def test_search_without_a_budget_cannot_create_a_monitor_task(self):
        request = self._request(budget=None)
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = self._run_search(["--select", "1", "--yes", "find Fitbit Air"], request=request)
        self.assertEqual(rc, 2)
        self.assertIn("target price", stderr.getvalue())
        self.assertEqual(self._tasks(), [])

    def test_search_with_no_offers_creates_nothing(self):
        result = self._result(offers=[], counts={"candidates": 0, "same": 0, "needs_confirmation": 0, "different": 0, "offers": 0})
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_search(["--yes", "find Fitbit Air"], result=result)
        self.assertEqual(rc, 2)
        self.assertIn("No comparable offer", stdout.getvalue())
        self.assertEqual(self._tasks(), [])

    def test_excluded_offer_cannot_be_monitored(self):
        good = self._offer()
        excluded = self._offer(
            retailer="Mighty Ape", price=None, currency=None, excluded_reason="price_unknown"
        )
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = self._run_search(
                ["--select", "2", "--yes", "find Fitbit Air"],
                result=self._result(offers=[good, excluded]),
            )
        self.assertEqual(rc, 1)
        self.assertIn("cannot be monitored", stderr.getvalue())
        self.assertEqual(self._tasks(), [])

    def test_search_with_only_excluded_offers_says_so(self):
        excluded = self._offer(price=None, excluded_reason="price_unknown")
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_search(["--yes", "find Fitbit Air"], result=self._result(offers=[excluded]))
        self.assertEqual(rc, 2)
        self.assertIn("Nothing can be monitored", stdout.getvalue())
        self.assertEqual(self._tasks(), [])

    def test_search_reports_a_partial_source_failure(self):
        result = self._result(
            sources=[
                {"retailer": "PB Tech", "status": "ok", "candidates": 1, "error": None},
                {"retailer": "Mighty Ape", "status": "error", "candidates": 0, "error": "connection refused"},
            ],
            notes=["Mighty Ape could not be searched: connection refused"],
        )
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_search(["--select", "1", "--yes", "find Fitbit Air"], result=result)
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertIn("FAILED (connection refused)", out)
        self.assertIn("Mighty Ape could not be searched", out)

    # ---------------------------------------------------------------- B2: condition
    # `search` must refuse an explicit new/used/refurbished requirement exactly like
    # `add`, and must not quietly drop it and continue.

    def test_search_with_condition_is_refused_even_with_yes(self):
        request = self._request(condition="used")
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            with patch("cli.agent.parse_shopping_request", return_value=request):
                with patch("cli.discovery.search_and_compare") as searched:
                    rc = cli.main(["--db-path", str(self.db_path), "search", "--select", "1", "--yes", "find a used Fitbit Air"])
        self.assertEqual(rc, 4)
        self.assertIn("not supported", stderr.getvalue())
        self.assertIn("used", stderr.getvalue())
        self.assertEqual(self._tasks(), [])
        # The refusal happens before any retailer is contacted.
        self.assertEqual(searched.call_count, 0)

    def test_search_with_new_condition_is_refused(self):
        request = self._request(condition="new")
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            rc = self._run_search(["--select", "1", "--yes", "find a new Fitbit Air"], request=request)
        self.assertEqual(rc, 4)
        self.assertIn("not supported", stderr.getvalue())
        self.assertEqual(self._tasks(), [])

    def test_search_with_any_condition_still_works(self):
        request = self._request(condition="any")
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_search(["--select", "1", "--yes", "find any Fitbit Air"], request=request)
        self.assertEqual(rc, 0)
        self.assertIn("Created task", stdout.getvalue())
        self.assertEqual(len(self._tasks()), 1)

    def test_search_without_a_condition_still_works(self):
        with patch("sys.stdout", new=io.StringIO()) as stdout:
            rc = self._run_search(["--select", "1", "--yes", "find Fitbit Air"], request=self._request(condition=None))
        self.assertEqual(rc, 0)
        self.assertIn("Created task", stdout.getvalue())
        self.assertEqual(len(self._tasks()), 1)


if __name__ == "__main__":
    unittest.main()

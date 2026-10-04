"""Stage D offline tests: the local web interface in demo mode.

The server runs on a thread bound to 127.0.0.1 with a throwaway SQLite database.
Demo mode guarantees: no network beyond the loopback, no LLM call (offline demo
parser), no real shop access (demo retailers), and no SMTP (notifications go
through the recorded fake sender, asserted via /api/demo/sends).

Round-2 coverage adds: CSRF/Origin/Host write guards, idempotent task creation,
frequency-only modify semantics, add-by-URL, missing-field supplements, the
continuous-monitoring loop lifecycle (refresh-safe, single-run), demo-database
isolation, and live-mode recipient boundaries.

Round-3 (stage D v5) adds: the offline LIVE-path end-to-end regression
(TestLiveModeOffline). The server runs in live mode on a temp database with
every external collaborator faked process-wide (fake DeepSeek, fake shops,
fake Brave, fake fetchers, explicit fake sender) — zero network, zero LLM,
zero SMTP — and walks natural-language search, preview (zero writes),
confirmed multi-source task creation, the server-side confirm gates for
immediate/due checks and monitor start, exactly one fake reminder per deal
window, history/modify/pause/stop, and actionable non-500 errors for
parser/config failures that never leak credentials.
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib import error as urlerror
from urllib import request as urlrequest

import config
import config_setup
import demo_data
import scheduler
import storage
import webapp

from tests import fake_server as live_fakes

REAL_DB_STEM = "bargain_agent"  # the user's real database file stem


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """A tiny HTTP client that holds one CSRF session (cookie + header)."""

    def __init__(self, base: str):
        self.base = base
        self.csrf = None
        self.cookie = None
        self.origin = f"http://127.0.0.1:{base.rsplit(':', 1)[1]}"

    def _headers(self, method: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _capture(self, headers) -> None:
        set_cookie = headers.get("Set-Cookie")
        if set_cookie and set_cookie.startswith("csrftoken="):
            self.cookie = set_cookie.split(";", 1)[0]
            self.csrf = self.cookie.split("=", 1)[1]

    def get(self, path: str):
        req = urlrequest.Request(self.base + path, headers=self._headers("GET"))
        try:
            with urlrequest.urlopen(req, timeout=10) as resp:
                self._capture(resp.headers)
                return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers
        except urlerror.HTTPError as exc:
            self._capture(exc.headers)
            return exc.code, json.loads(exc.read().decode("utf-8")), exc.headers

    def post(self, path: str, payload: dict | None = None, *,
             raw: bytes | None = None, csrf: str | None = "auto",
             origin: str | None = "auto", host: str | None = None,
             cookie: str | None = "auto"):
        headers = self._headers("POST")
        if csrf == "auto":
            csrf = self.csrf
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        if origin == "auto":
            origin = self.origin
        if origin is not None:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        if cookie == "auto":
            cookie = self.cookie
        if cookie is not None:
            headers["Cookie"] = cookie
        if "Cookie" not in headers:
            headers.pop("Cookie", None)
        data = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
        req = urlrequest.Request(self.base + path, data=data, method="POST", headers=headers)
        try:
            with urlrequest.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


EN_INSTRUCTION = "find AeroBuds Pro black, budget NZD 200, check every 60 minutes"
ZH_INSTRUCTION = "找 AeroBuds Pro 黑色 预算 200 纽币 每 60 分钟查一次"


class ServerHarness:
    def __init__(self, *, demo: bool = True, db_path: Path | None = None):
        self.port = _free_port()
        self.httpd = webapp.serve(db_path, port=self.port, demo=demo)
        self.db_path = Path(self.httpd.db_path)  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.client = Client(self.base)

    def start_csrf(self):
        # Fetch the token (also sets the cookie server-side).
        status, data, _ = self.client.get("/api/csrf")
        assert status == 200
        self.client.csrf = data["csrf_token"]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class WebAppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Setup-config isolation: /api/setup must never touch the developer's
        # real .env — redirect config_setup to a throwaway file for this process.
        cls._prev_env_override = os.environ.get("BARGAIN_AGENT_ENV_FILE")
        fd, env_name = tempfile.mkstemp(prefix="webapp-test-env-", suffix=".txt")
        os.close(fd)  # close the handle or Windows keeps the file locked
        cls.env_file = Path(env_name)
        cls.env_file.write_text("", encoding="utf-8")
        os.environ["BARGAIN_AGENT_ENV_FILE"] = str(cls.env_file)
        cls.harness = ServerHarness()
        cls.harness.start_csrf()
        cls.db_path = cls.harness.db_path
        cls.client = cls.harness.client
        cls.base = cls.harness.base

    @classmethod
    def tearDownClass(cls):
        cls.harness.stop()
        if cls._prev_env_override is None:
            os.environ.pop("BARGAIN_AGENT_ENV_FILE", None)
        else:
            os.environ["BARGAIN_AGENT_ENV_FILE"] = cls._prev_env_override
        cls.env_file.unlink(missing_ok=True)

    def setUp(self):
        # The server (and its database) is shared by the whole class; the demo
        # recorders must reset per test so send counts and failure injections
        # never leak between tests.
        demo_data.RECORDED_SENDS.clear()
        demo_data.DEMO_FAIL_URLS.clear()

    def _search(self, instruction=EN_INSTRUCTION, **supplements):
        status, data = self.client.post("/api/search", {"instruction": instruction, **supplements})
        self.assertEqual(status, 200)
        return data

    def _pick_two(self, instruction=EN_INSTRUCTION):
        data = self._search(instruction)
        self.assertEqual(data["status"], "ready")
        result = data["result"]
        picked = [o["url"] for o in result["offers"]
                  if not o["excluded_reason"] and o["match"] == "same"][:2]
        return data, picked

    def _select_two(self, instruction=EN_INSTRUCTION) -> int:
        """Preview then confirm — the full creation path."""
        data, picked = self._pick_two(instruction)
        status, preview = self.client.post("/api/search/select",
                                           {"token": data["token"], "urls": picked})
        self.assertEqual(status, 200)
        self.assertIn("preview", preview)
        self.assertNotIn("created", preview)      # preview creates nothing
        status, created = self.client.post("/api/search/confirm",
                                           {"token": data["token"], "urls": picked})
        self.assertEqual(status, 200)
        return created["created"]


class TestServerBasics(WebAppTestCase):
    def test_index_is_english_ui(self):
        # '/' returns HTML, not JSON — fetch raw.
        with urlrequest.urlopen(self.base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("Bargain Agent — Price Monitor", html)
        self.assertIn("Search &amp; compare", html)
        self.assertIn("Monitored tasks", html)
        self.assertIn("Add a task by product URL", html)

    def test_binds_loopback_only(self):
        self.assertEqual(self.harness.httpd.server_address[0], "127.0.0.1")

    def test_unknown_route_is_404(self):
        status, data, _ = self.client.get("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_bad_json_is_400(self):
        status, data = self.client.post("/api/search", raw=b"{not json")
        self.assertEqual(status, 400)

    def test_get_on_post_route_is_404(self):
        status, _data, _ = self.client.get("/api/search")
        self.assertEqual(status, 404)

    def test_mode_badge_reports_demo(self):
        status, data, _ = self.client.get("/api/tasks")
        self.assertEqual(status, 200)
        self.assertTrue(data["demo"])


class TestWriteGuards(WebAppTestCase):
    """CSRF / Origin / Host protection on state-changing requests."""

    def _post_rejected(self, path, payload, **kwargs):
        """POST that a write guard rejects (403).

        The guard runs before the request body is read, so on Windows the
        server can send the 403 and close the socket while the client still has
        unread body bytes in flight — the client then sees a transient
        connection reset (WinError 10053) instead of the response. The
        rejection is side-effect-free, so retry a couple of times before
        concluding the request failed.
        """
        last_error = None
        for _ in range(3):
            try:
                return self.client.post(path, payload, **kwargs)
            except (urlerror.URLError, ConnectionError) as exc:
                last_error = exc
        raise last_error

    def test_missing_csrf_token_is_403(self):
        status, data = self._post_rejected("/api/search", {"instruction": EN_INSTRUCTION},
                                           csrf=None)
        self.assertEqual(status, 403)

    def test_wrong_csrf_token_is_403(self):
        status, data = self._post_rejected("/api/search", {"instruction": EN_INSTRUCTION},
                                           csrf="forged-token")
        self.assertEqual(status, 403)

    def test_missing_origin_is_403(self):
        status, data = self._post_rejected("/api/search", {"instruction": EN_INSTRUCTION},
                                           origin=None)
        self.assertEqual(status, 403)

    def test_foreign_origin_is_403(self):
        status, data = self._post_rejected("/api/search", {"instruction": EN_INSTRUCTION},
                                           origin="http://evil.example")
        self.assertEqual(status, 403)

    def test_spoofed_host_is_403(self):
        status, data = self._post_rejected("/api/search", {"instruction": EN_INSTRUCTION},
                                           host="evil.example:80")
        self.assertEqual(status, 403)


class TestDemoDatabaseIsolation(WebAppTestCase):
    def test_demo_server_refuses_the_real_database(self):
        real = config.database_path()
        with self.assertRaises(SystemExit):
            webapp.serve(real, port=_free_port(), demo=True)

    def test_demo_default_database_is_an_isolated_temp_file(self):
        harness = ServerHarness()  # no explicit db-path
        try:
            self.assertNotEqual(harness.db_path.stem, REAL_DB_STEM)
            self.assertIn("webui-demo-", harness.db_path.name)
        finally:
            harness.stop()

    def test_demo_tasks_never_leak_into_the_real_database_path(self):
        # The shared harness DB is a throwaway file, not the configured one.
        self.assertNotEqual(self.db_path.resolve(), config.database_path().resolve())


class TestSearchFlow(WebAppTestCase):
    def test_english_search_returns_offers_and_verdicts(self):
        data = self._search(EN_INSTRUCTION)
        self.assertEqual(data["status"], "ready")
        result = data["result"]
        verdicts = {o["match"] for o in result["offers"]}
        self.assertIn("same", verdicts)
        self.assertTrue(result["selectable"], "at least two confirmed offers expected")
        retailers = {s["retailer"] for s in result["sources"] if s["status"] == "ok"}
        self.assertEqual(retailers, {"Demo Tech", "Demo Mart"})

    def test_chinese_search_finds_the_same_product(self):
        data = self._search(ZH_INSTRUCTION)
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["request"]["product_name"], "AeroBuds Pro")
        self.assertEqual(data["request"]["currency"], "NZD")
        self.assertEqual(data["request"]["budget"], "200.00")

    def test_missing_budget_needs_clarification_and_form_supplement_works(self):
        data = self._search("find AeroBuds Pro black, check every 60 minutes")
        self.assertEqual(data["status"], "needs_clarification")
        self.assertIn("budget", data["missing_fields"])
        # Supplement the missing fields through the form path.
        data2 = self._search("find AeroBuds Pro black, check every 60 minutes",
                             budget="200", currency="NZD")
        self.assertEqual(data2["status"], "ready")
        self.assertEqual(data2["request"]["budget"], "200.00")

    def test_all_identity_verdicts_are_visible(self):
        data = self._search(EN_INSTRUCTION)
        counts = data["result"]["counts"]
        self.assertGreater(counts["same"], 0)
        self.assertGreater(counts["needs_confirmation"], 0)
        self.assertGreater(counts["different"], 0)

    def test_empty_instruction_is_400(self):
        status, data = self.client.post("/api/search", {"instruction": "   "})
        self.assertEqual(status, 400)


class TestTaskLifecycle(WebAppTestCase):
    def test_select_creates_a_multi_source_task_with_identity(self):
        task_id = self._select_two()
        status, detail, _ = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["sources"]), 2)
        self.assertEqual({s["retailer"] for s in detail["sources"]}, {"Demo Tech", "Demo Mart"})
        row = storage.get_task(task_id, db_path=self.db_path)
        self.assertIsNotNone(row["identity_json"])

    def test_preview_and_cancel_never_create_a_task(self):
        """D-R1: preview is side-effect-free; cancelling leaves zero new tasks."""
        data, picked = self._pick_two()
        before = len(storage.list_tasks(db_path=self.db_path))
        status, preview = self.client.post("/api/search/select",
                                           {"token": data["token"], "urls": picked})
        self.assertEqual(status, 200)
        p = preview["preview"]
        self.assertEqual(p["currency"], "NZD")
        self.assertEqual(len(p["sources"]), 2)
        self.assertTrue(p["notify_email"])
        after_select = len(storage.list_tasks(db_path=self.db_path))
        self.assertEqual(after_select, before)    # no task created by preview
        # "Cancel" is purely a UI action: nothing to undo on the server.
        self.assertEqual(len(storage.list_tasks(db_path=self.db_path)), before)

    def test_confirmation_creates_exactly_one_task(self):
        data, picked = self._pick_two()
        self.client.post("/api/search/select", {"token": data["token"], "urls": picked})
        before = len(storage.list_tasks(db_path=self.db_path))
        status, created = self.client.post("/api/search/confirm",
                                           {"token": data["token"], "urls": picked})
        self.assertEqual(status, 200)
        self.assertFalse(created.get("duplicate"))
        self.assertEqual(len(storage.list_tasks(db_path=self.db_path)), before + 1)

    def test_repeated_confirmation_does_not_duplicate_the_task(self):
        data, picked = self._pick_two()
        self.client.post("/api/search/select", {"token": data["token"], "urls": picked})
        status, first = self.client.post("/api/search/confirm",
                                         {"token": data["token"], "urls": picked})
        self.assertEqual(status, 200)
        self.assertFalse(first.get("duplicate"))
        count_after_first = len(storage.list_tasks(db_path=self.db_path))
        # Same confirmation submitted again: same task, no duplicate row.
        status, second = self.client.post("/api/search/confirm",
                                          {"token": data["token"], "urls": picked})
        self.assertEqual(status, 200)
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["created"], first["created"])
        self.assertEqual(len(storage.list_tasks(db_path=self.db_path)), count_after_first)
        # A *different* selection on the same token is refused.
        other = [o["url"] for o in data["result"]["offers"]
                 if not o["excluded_reason"] and o["match"] == "same"][2:]
        if other:
            status, resp = self.client.post("/api/search/confirm",
                                            {"token": data["token"], "urls": other})
            self.assertEqual(status, 409)

    def test_concurrent_confirmations_of_one_token_create_one_task(self):
        """D-R2: same token confirmed in parallel yields exactly one task."""
        data, picked = self._pick_two()
        self.client.post("/api/search/select", {"token": data["token"], "urls": picked})
        before = len(storage.list_tasks(db_path=self.db_path))
        barrier = threading.Barrier(4, timeout=15)
        results: list[tuple[int, dict]] = []

        def worker():
            barrier.wait()
            results.append(self.client.post("/api/search/confirm",
                                            {"token": data["token"], "urls": picked}))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(len(results), 4)
        created_ids = {resp.get("created") for _, resp in results}
        self.assertEqual(len(created_ids), 1)     # one task for all four confirmations
        self.assertEqual(len(storage.list_tasks(db_path=self.db_path)), before + 1)
        duplicates = sum(1 for _, resp in results if resp.get("duplicate"))
        self.assertEqual(duplicates, 3)

    def test_failed_confirmation_does_not_consume_the_token(self):
        """D-R2: if creation fails, the token stays usable (retry works)."""
        data, picked = self._pick_two()
        self.client.post("/api/search/select", {"token": data["token"], "urls": picked})
        original = storage.create_task_with_sources
        with patch.object(storage, "create_task_with_sources",
                          side_effect=RuntimeError("simulated db failure")):
            status, resp = self.client.post("/api/search/confirm",
                                            {"token": data["token"], "urls": picked})
        self.assertEqual(status, 500)
        # Token not consumed: the same confirmation now succeeds.
        status, created = self.client.post("/api/search/confirm",
                                           {"token": data["token"], "urls": picked})
        self.assertEqual(status, 200)
        self.assertFalse(created.get("duplicate"))
        self.assertIn("created", created)

    def test_select_with_unknown_offer_url_is_400(self):
        data = self._search(EN_INSTRUCTION)
        status, resp = self.client.post("/api/search/select",
                                        {"token": data["token"], "urls": ["https://nope.example/x"]})
        self.assertEqual(status, 400)

    def test_check_finds_the_deal_and_records_exactly_one_fake_send(self):
        task_id = self._select_two()
        status, result = self.client.post(f"/api/tasks/{task_id}/check")
        self.assertEqual(status, 200)
        self.assertTrue(result["is_deal"])
        self.assertTrue(result["notified"])
        self.assertEqual(result["sources_checked"], 2)
        self.assertEqual(result["chosen_price"], "189.00")  # cheapest same offer

        status, sends, _ = self.client.get("/api/demo/sends")
        self.assertEqual(len(sends["sends"]), 1)

        _post = self.client.post(f"/api/tasks/{task_id}/check")
        _, sends, _ = self.client.get("/api/demo/sends")
        self.assertEqual(len(sends["sends"]), 1)

    def test_accessory_page_never_triggers_a_deal(self):
        task_id = self._select_two()
        for source in storage.list_task_sources(task_id, db_path=self.db_path):
            storage.set_task_source_enabled(source["id"], False, db_path=self.db_path)
        storage.add_task_source(
            task_id, "https://demo-mart.example/aerobuds-pro-active-band",
            retailer="Demo Mart", db_path=self.db_path,
        )
        status, result = self.client.post(f"/api/tasks/{task_id}/check")
        self.assertEqual(status, 200)
        accessory = [o for o in result["observations"] if o["retailer"] == "Demo Mart"
                     and o["price"] == "19.00"]
        self.assertTrue(accessory)
        self.assertNotEqual(accessory[0]["identity_verdict"], "same")
        self.assertFalse(result["is_deal"])
        _, sends, _ = self.client.get("/api/demo/sends")
        self.assertEqual(len(sends["sends"]), 0)

    def test_modify_price_ends_the_episode_and_rejudges(self):
        task_id = self._select_two()
        self.client.post(f"/api/tasks/{task_id}/check")
        status, data = self.client.post(f"/api/tasks/{task_id}/modify",
                                        {"target_price": "100.00"})
        self.assertEqual(status, 200)
        self.assertTrue(data["episode_ended"])
        status, result = self.client.post(f"/api/tasks/{task_id}/check")
        self.assertFalse(result["is_deal"])
        _, sends, _ = self.client.get("/api/demo/sends")
        self.assertEqual(len(sends["sends"]), 1)  # no resend after re-judge

    def test_frequency_only_modify_never_touches_price_or_comparison(self):
        task_id = self._select_two()
        before = storage.get_task(task_id, db_path=self.db_path)
        status, data = self.client.post(f"/api/tasks/{task_id}/modify",
                                        {"frequency_minutes": "30"})
        self.assertEqual(status, 200)
        self.assertFalse(data["episode_ended"])
        after = storage.get_task(task_id, db_path=self.db_path)
        self.assertEqual(after["target_price"], before["target_price"])
        self.assertEqual(after["comparison"], before["comparison"])
        self.assertEqual(after["frequency_minutes"], 30)

    def test_modify_without_fields_is_400(self):
        task_id = self._select_two()
        status, _ = self.client.post(f"/api/tasks/{task_id}/modify", {})
        self.assertEqual(status, 400)

    def test_pause_skips_the_check_and_resume_restores_it(self):
        task_id = self._select_two()
        self.client.post(f"/api/tasks/{task_id}/pause")
        status, result = self.client.post(f"/api/tasks/{task_id}/check")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "task_paused")
        self.client.post(f"/api/tasks/{task_id}/resume")
        status, result = self.client.post(f"/api/tasks/{task_id}/check")
        self.assertFalse(result["skipped"])

    def test_stop_stops_the_task(self):
        task_id = self._select_two()
        status, data = self.client.post(f"/api/tasks/{task_id}/stop")
        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["status"], "stopped")

    def test_disable_source_and_check_only_the_other(self):
        task_id = self._select_two()
        status, detail, _ = self.client.get(f"/api/tasks/{task_id}")
        first = detail["sources"][0]
        status, data = self.client.post(f"/api/tasks/{task_id}/sources/disable",
                                        {"source_id": first["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["enabled_sources"], 1)
        status, result = self.client.post(f"/api/tasks/{task_id}/check")
        self.assertEqual(result["sources_checked"], 1)
        self.assertNotEqual(result["observations"][0]["source_id"], first["id"])
        status, data = self.client.post(f"/api/tasks/{task_id}/sources/enable",
                                        {"source_id": first["id"]})
        self.assertEqual(data["enabled_sources"], 2)

    def test_source_ownership_is_enforced(self):
        first = self._select_two()
        second = self._select_two(ZH_INSTRUCTION)
        status, detail, _ = self.client.get(f"/api/tasks/{second}")
        foreign = detail["sources"][0]["id"]
        status, data = self.client.post(f"/api/tasks/{first}/sources/disable",
                                        {"source_id": foreign})
        self.assertEqual(status, 400)

    def test_source_toggle_without_source_id_is_400_not_500(self):
        # A malformed toggle (no source_id) is a client error, never a 500.
        task_id = self._select_two()
        for action in ("enable", "disable"):
            status, data = self.client.post(f"/api/tasks/{task_id}/sources/{action}", {})
            self.assertEqual(status, 400, action)
            self.assertNotIn("Internal error", data.get("error", ""))

    def test_failed_source_is_reported_not_hidden(self):
        task_id = self._select_two()
        sources = storage.list_task_sources(task_id, db_path=self.db_path)
        demo_data.DEMO_FAIL_URLS.add(sources[0]["url"])
        try:
            status, result = self.client.post(f"/api/tasks/{task_id}/check")
            self.assertEqual(status, 200)
            self.assertEqual(result["sources_failed"], 1)
            failed = [o for o in result["observations"] if o["error"]]
            self.assertTrue(failed)
            status, detail, _ = self.client.get(f"/api/tasks/{task_id}")
            errored = [s for s in detail["sources"] if s["last_error"]]
            self.assertTrue(errored)
        finally:
            demo_data.DEMO_FAIL_URLS.clear()

    def test_unknown_stock_is_visible(self):
        data = self._search(EN_INSTRUCTION)
        unknown = [o for o in data["result"]["offers"] if o["in_stock"] is None]
        self.assertTrue(unknown, "demo data includes an unknown-stock page")

    def test_history_is_recorded_per_source(self):
        task_id = self._select_two()
        self.client.post(f"/api/tasks/{task_id}/check")
        status, data, _ = self.client.get(f"/api/tasks/{task_id}/history")
        self.assertEqual(status, 200)
        sources = {h["source_id"] for h in data["history"]}
        self.assertEqual(len(sources), 2)

    def test_missing_task_is_404(self):
        status, _data, _ = self.client.get("/api/tasks/424242")
        self.assertEqual(status, 404)
        status, _data = self.client.post("/api/tasks/424242/check")
        self.assertEqual(status, 404)


class TestAddByUrl(WebAppTestCase):
    def test_add_by_url_creates_a_single_source_task(self):
        status, data = self.client.post("/api/tasks/add", {
            "url": "https://demo-tech.example/aerobuds-pro-black",
            "product_name": "AeroBuds Pro Wireless Earbuds - Black",
            "target_price": "199.00", "currency": "NZD",
            "comparison": "lte", "frequency_minutes": 60,
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(data["task"]["sources"]), 1)

    def test_add_rejects_non_http_protocols(self):
        status, data = self.client.post("/api/tasks/add", {
            "url": "javascript:alert(1)", "product_name": "x",
            "target_price": "10.00", "currency": "NZD", "frequency_minutes": 60,
        })
        self.assertEqual(status, 400)
        self.assertIn("http", data["error"])

    def test_add_rejects_unsupported_host(self):
        status, data = self.client.post("/api/tasks/add", {
            "url": "https://not-a-demo-shop.example/p", "product_name": "x",
            "target_price": "10.00", "currency": "NZD", "frequency_minutes": 60,
        })
        self.assertEqual(status, 400)

    def test_add_rejects_missing_fields(self):
        status, _ = self.client.post("/api/tasks/add", {
            "url": "https://demo-tech.example/aerobuds-pro-black",
            "product_name": "", "target_price": "", "frequency_minutes": 60,
        })
        self.assertEqual(status, 400)


class TestMonitorLoop(WebAppTestCase):
    def test_monitor_lifecycle_is_single_run_and_refresh_safe(self):
        task_id = self._select_two()
        status, data, _ = self.client.get("/api/monitor/status")
        self.assertEqual(data["status"], "not_started")
        # Page refresh calls status again; it must NOT start anything.
        status, data, _ = self.client.get("/api/monitor/status")
        self.assertEqual(data["status"], "not_started")

        status, data = self.client.post("/api/monitor/start")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "running")

        # Double start and page refresh during a run are both refused.
        status, data = self.client.post("/api/monitor/start")
        self.assertEqual(status, 409)
        status, data, _ = self.client.get("/api/monitor/status")
        self.assertEqual(data["status"], "running")
        self.assertFalse(data["restartable"])

        status, data = self.client.post("/api/monitor/stop")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "stopped")
        self.assertFalse(data["restartable"])

        # Restart after stop is refused by the scheduler's one-run-per-process rule.
        status, data = self.client.post("/api/monitor/start")
        self.assertEqual(status, 409)


class TestLiveModeBoundaries(WebAppTestCase):
    """The live form exists but is never acceptance-exercised against real
    services; these tests pin its boundaries without any network, LLM or SMTP
    call. The server runs on a throwaway database, never the real one."""

    @classmethod
    def setUpClass(cls):
        fd, db_name = tempfile.mkstemp(prefix="webui-live-boundaries-", suffix=".db")
        os.close(fd)
        cls.harness = ServerHarness(demo=False, db_path=Path(db_name))
        cls.harness.start_csrf()
        cls.client = cls.harness.client
        cls.base = cls.harness.base

    @classmethod
    def tearDownClass(cls):
        cls.harness.stop()

    def test_live_mode_reports_the_email_warning(self):
        status, data, _ = self.client.get("/api/tasks")
        self.assertFalse(data["demo"])
        # The demo recipient must never appear in live mode; without a configured
        # DEFAULT_NOTIFY_EMAIL the warning still says emails may be sent.
        self.assertIn("real notification email", data["email_warning"])

    def test_live_add_by_url_requires_a_configured_real_recipient(self):
        import os
        from unittest.mock import patch

        # Simulate an unconfigured .env: with no recipient visible to config,
        # live task creation must be refused instead of silently mailing
        # nothing (or the demo address). The developer's real .env DOES have
        # a recipient now, so blank it out explicitly for this test.
        with patch.dict(os.environ, {"DEFAULT_NOTIFY_EMAIL": ""}):
            status, data = self.client.post("/api/tasks/add", {
                "url": "https://www.pbtech.co.nz/product/test", "product_name": "Test",
                "target_price": "10.00", "currency": "NZD", "frequency_minutes": 60,
            })
        self.assertEqual(status, 400)
        self.assertIn("email", data["error"])

    def test_live_demo_recipient_is_refused(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"DEFAULT_NOTIFY_EMAIL": "demo-user@localhost"}):
            status, data = self.client.post("/api/tasks/add", {
                "url": "https://www.pbtech.co.nz/product/test", "product_name": "Test",
                "target_price": "10.00", "currency": "NZD", "frequency_minutes": 60,
            })
        self.assertEqual(status, 400)

    def test_live_monitor_start_requires_explicit_confirm(self):
        status, data = self.client.post("/api/monitor/start", {})
        self.assertEqual(status, 400)
        self.assertIn("confirm", data["error"])


class TestLiveModeOffline(unittest.TestCase):
    """Offline end-to-end regression of the LIVE web path (stage D v5 gate).

    The server runs in live mode (demo=False) on a throwaway database, and every
    external collaborator is substituted process-wide by the fakes from
    tools_live_offline_server: fake DeepSeek parser, two fake shops, a fake
    Brave web-search provider, fake detail/check fetchers and an explicit fake
    sender. The business modules — discovery matching, identity gating,
    comparison, storage, scheduling — are the real ones. Zero network, zero
    LLM, zero real shop fetch, zero SMTP.
    """

    LIVE_NOTIFY = live_fakes.LIVE_NOTIFY_EMAIL

    @classmethod
    def setUpClass(cls):
        fd, db_name = tempfile.mkstemp(prefix="webui-live-offline-", suffix=".db")
        os.close(fd)
        cls.db_path = Path(db_name)
        cls.env_patch = patch.dict(
            os.environ, {"DEFAULT_NOTIFY_EMAIL": live_fakes.LIVE_NOTIFY_EMAIL})
        cls.env_patch.start()
        cls.fakes = live_fakes.install_live_fakes()
        # The scheduler's process slot is one-per-process; another test class
        # may already have run (and cleanly finished) a monitor loop. Reset the
        # finished-run bookkeeping so this class can exercise the live monitor
        # start; nothing runs concurrently and tearDownClass restores the state.
        cls._saved_scheduler = (scheduler._active_scheduler, scheduler._scheduler_has_run)
        scheduler._active_scheduler = None
        scheduler._scheduler_has_run = False
        cls.harness = ServerHarness(demo=False, db_path=cls.db_path)
        cls.harness.start_csrf()
        cls.client = cls.harness.client
        cls.base = cls.harness.base

    @classmethod
    def tearDownClass(cls):
        cls.harness.stop()
        thread = cls.harness.httpd.app_state.monitor.get("thread")
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        scheduler._active_scheduler, scheduler._scheduler_has_run = cls._saved_scheduler
        cls.fakes.close()
        cls.env_patch.stop()
        try:
            os.unlink(cls.db_path)
        except OSError:
            pass

    def setUp(self):
        live_fakes.reset_live_sends()
        live_fakes.FETCH_CALLS.clear()

    # -- helpers ---------------------------------------------------------
    def _search(self, instruction=live_fakes.EN_INSTRUCTION):
        status, data = self.client.post("/api/search", {"instruction": instruction})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ready")
        return data

    def _selectable(self, data):
        return [o["url"] for o in data["result"]["offers"]
                if not o.get("excluded_reason") and o.get("match") == "same"]

    def _create_task(self, instruction=live_fakes.EN_INSTRUCTION):
        """Full live creation path: natural-language search -> confirm."""
        data = self._search(instruction)
        urls = self._selectable(data)
        self.assertGreaterEqual(len(urls), 2)
        status, created = self.client.post("/api/search/confirm",
                                           {"token": data["token"], "urls": urls})
        self.assertEqual(status, 200)
        self.assertFalse(created.get("duplicate"))
        return created["created"]

    # -- error paths: actionable non-500, never a credential leak --------
    def test_live_config_error_is_an_actionable_400_not_a_500(self):
        import agent

        def broken(instruction):
            raise config.ConfigError(
                "Missing LLM_API_KEY. Set LLM_API_KEY in .env (copied from .env.example).")

        with patch.object(agent, "parse_shopping_request", broken):
            status, data = self.client.post("/api/search", {"instruction": "find anything"})
        self.assertEqual(status, 400)
        self.assertIn("Configuration problem", data["error"])
        self.assertIn("LLM_API_KEY", data["error"])
        self.assertNotIn("Internal error", data["error"])
        # No credential-shaped string may leak into the response.
        self.assertNotRegex(json.dumps(data), r"sk-[A-Za-z0-9]{6,}")

    def test_live_parser_error_is_an_actionable_400_not_a_500(self):
        import agent

        def broken(instruction):
            raise agent.ParserError(
                "LLM request failed. Check configuration, quota and connectivity.")

        with patch.object(agent, "parse_shopping_request", broken):
            status, data = self.client.post("/api/search", {"instruction": "find anything"})
        self.assertEqual(status, 400)
        self.assertIn("could not be parsed", data["error"])
        self.assertIn("Check configuration, quota and connectivity", data["error"])
        self.assertNotIn("Internal error", data["error"])
        self.assertNotRegex(json.dumps(data), r"sk-[A-Za-z0-9]{6,}")

    # -- natural-language search ----------------------------------------
    def test_live_natural_language_search_produces_a_ready_result(self):
        for instruction in (live_fakes.EN_INSTRUCTION, live_fakes.ZH_INSTRUCTION):
            before = len(storage.list_tasks(db_path=self.db_path))
            data = self._search(instruction)
            result = data["result"]
            by_name = {s["retailer"]: s for s in result["sources"]}
            self.assertEqual(by_name["Live Tech"]["status"], "ok", instruction)
            self.assertEqual(by_name["Live Mart"]["status"], "ok", instruction)
            self.assertEqual(by_name["Brave Web Search"]["status"], "ok", instruction)
            # Shops configured in the real registry but not part of this offline
            # run are reported as not searched — never silently omitted.
            self.assertTrue(any(s["status"] == "disabled" for s in result["sources"]))
            counts = result["counts"]
            self.assertGreaterEqual(counts["same"], 3)
            # Wrong colour and the accessory are rejected; the unstated colour
            # and the unstated-generation listings need confirmation.
            self.assertGreaterEqual(counts["needs_confirmation"], 1)
            self.assertGreaterEqual(counts["different"], 2)
            # Unknown stock must be visible, never rendered as buyable.
            self.assertTrue(any(o["in_stock"] is None for o in result["offers"]))
            # A web-search offer is among the confirmed ones.
            self.assertTrue(any(o.get("source_kind") == "web_search"
                                and o.get("match") == "same" for o in result["offers"]))
            # A search alone never creates a task.
            self.assertEqual(len(storage.list_tasks(db_path=self.db_path)), before)

    # -- preview / confirm ------------------------------------------------
    def test_live_preview_writes_nothing_and_confirm_creates_one_multi_source_task(self):
        data = self._search()
        urls = self._selectable(data)
        before = len(storage.list_tasks(db_path=self.db_path))
        status, preview = self.client.post("/api/search/select",
                                           {"token": data["token"], "urls": urls})
        self.assertEqual(status, 200)
        self.assertEqual(len(storage.list_tasks(db_path=self.db_path)), before)
        p = preview["preview"]
        self.assertEqual(p["notify_email"], self.LIVE_NOTIFY)
        self.assertEqual({s["source_kind"] for s in p["sources"]},
                         {"retailer_adapter", "web_search"})
        self.assertIn("real notification email", preview["email_warning"])
        # "Cancel" is purely a UI action: still zero tasks.
        self.assertEqual(len(storage.list_tasks(db_path=self.db_path)), before)
        status, created = self.client.post("/api/search/confirm",
                                           {"token": data["token"], "urls": urls})
        self.assertEqual(status, 200)
        task_id = created["created"]
        self.assertEqual(len(storage.list_tasks(db_path=self.db_path)), before + 1)
        row = storage.get_task(task_id, db_path=self.db_path)
        self.assertIsNotNone(row["identity_json"])
        self.assertEqual(row["notify_email"], self.LIVE_NOTIFY)
        self.assertEqual(row["target_price"], "200.00")
        status, detail, _ = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["sources"]), 3)
        self.assertEqual({s["source_kind"] for s in detail["sources"]},
                         {"retailer_adapter", "web_search"})

    # -- immediate check gating + exactly one fake reminder ---------------
    def test_live_immediate_check_is_gated_and_sends_exactly_one_fake_reminder(self):
        task_id = self._create_task()
        # Direct API call without confirm: refused, nothing fetched, nothing sent.
        status, data = self.client.post(f"/api/tasks/{task_id}/check")
        self.assertEqual(status, 400)
        self.assertIn("confirm", data["error"])
        self.assertEqual(len(live_fakes.LIVE_SENDS), 0)
        self.assertEqual(len(live_fakes.FETCH_CALLS), 0)
        # Confirmed check: a real deal on the cheapest confirmed offer.
        status, result = self.client.post(f"/api/tasks/{task_id}/check", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(result["is_deal"])
        self.assertTrue(result["notified"])
        self.assertEqual(result["sources_checked"], 3)
        self.assertEqual(result["chosen_price"], "179.00")
        self.assertEqual(len(live_fakes.LIVE_SENDS), 1)
        self.assertEqual(live_fakes.LIVE_SENDS[0]["to"], self.LIVE_NOTIFY)
        self.assertEqual(len(live_fakes.FETCH_CALLS), 3)
        # Same deal window: a second confirmed check must not re-notify.
        status, result = self.client.post(f"/api/tasks/{task_id}/check", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(result["is_deal"])
        self.assertFalse(result["notified"])
        self.assertEqual(len(live_fakes.LIVE_SENDS), 1)
        self.assertEqual(len(live_fakes.FETCH_CALLS), 6)

    # -- due-batch gating --------------------------------------------------
    def test_live_due_batch_is_gated_and_checks_due_tasks(self):
        task_id = self._create_task()
        storage.update_next_check(task_id, "2020-01-01T00:00:00+00:00",
                                  db_path=self.db_path)
        status, data = self.client.post("/api/check/due")
        self.assertEqual(status, 400)
        self.assertIn("confirm", data["error"])
        self.assertEqual(len(live_fakes.LIVE_SENDS), 0)
        status, data = self.client.post("/api/check/due", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(data["checked"], 1)
        self.assertEqual(len(live_fakes.LIVE_SENDS), 1)

    # -- history / modify / pause / stop in live mode ----------------------
    def test_live_history_modify_pause_and_stop(self):
        task_id = self._create_task()
        self.client.post(f"/api/tasks/{task_id}/check", {"confirm": True})
        status, history, _ = self.client.get(f"/api/tasks/{task_id}/history")
        self.assertEqual(status, 200)
        self.assertEqual(len({h["source_id"] for h in history["history"]}), 3)
        # Frequency-only modify never touches the threshold or comparison.
        before = storage.get_task(task_id, db_path=self.db_path)
        status, data = self.client.post(f"/api/tasks/{task_id}/modify",
                                        {"frequency_minutes": 30})
        self.assertEqual(status, 200)
        self.assertFalse(data["episode_ended"])
        after = storage.get_task(task_id, db_path=self.db_path)
        self.assertEqual(after["target_price"], before["target_price"])
        self.assertEqual(after["comparison"], before["comparison"])
        self.assertEqual(after["frequency_minutes"], 30)
        # Threshold change ends the episode; at NZD 100 nothing is a deal.
        status, data = self.client.post(f"/api/tasks/{task_id}/modify",
                                        {"target_price": "100.00"})
        self.assertEqual(status, 200)
        self.assertTrue(data["episode_ended"])
        status, result = self.client.post(f"/api/tasks/{task_id}/check", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertFalse(result["is_deal"])
        # Pause skips the check; resume restores it; stop ends the task.
        self.client.post(f"/api/tasks/{task_id}/pause")
        status, result = self.client.post(f"/api/tasks/{task_id}/check", {"confirm": True})
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "task_paused")
        self.client.post(f"/api/tasks/{task_id}/resume")
        status, data = self.client.post(f"/api/tasks/{task_id}/stop")
        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["status"], "stopped")

    # -- direct-URL add in live mode --------------------------------------
    def test_live_add_by_url_then_honest_check_failure(self):
        status, data = self.client.post("/api/tasks/add", {
            "url": "https://www.pbtech.co.nz/product/TEST-live-path",
            "product_name": "Test Live Product",
            "target_price": "10.00", "currency": "NZD",
            "comparison": "lte", "frequency_minutes": 60,
        })
        self.assertEqual(status, 200)
        task_id = data["created"]
        self.assertEqual(data["task"]["notify_email"], self.LIVE_NOTIFY)
        # The live confirm gate applies to direct-URL tasks too.
        status, resp = self.client.post(f"/api/tasks/{task_id}/check")
        self.assertEqual(status, 400)
        # Confirmed check: the offline fake has no page for this URL, so the
        # source fails — reported honestly, never as a deal or "no result".
        status, result = self.client.post(f"/api/tasks/{task_id}/check", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(result["sources_failed"], 1)
        self.assertFalse(result["is_deal"])
        self.assertEqual(len(live_fakes.LIVE_SENDS), 0)
        self.client.post(f"/api/tasks/{task_id}/stop")

    # -- monitor loop ------------------------------------------------------
    def test_live_monitor_start_is_gated_and_runs_without_any_fetch(self):
        status, data, _ = self.client.get("/api/monitor/status")
        self.assertEqual(data["status"], "not_started")
        status, data = self.client.post("/api/monitor/start", {})
        self.assertEqual(status, 400)
        self.assertIn("confirm", data["error"])
        fetches_before = len(live_fakes.FETCH_CALLS)
        status, data = self.client.post("/api/monitor/start", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "running")
        self.assertIn("real notification email", data["email_warning"])
        # No task is due yet: the running loop must fetch nothing and send
        # nothing (all fetches go through the process-wide fake anyway).
        time.sleep(1.5)
        self.assertEqual(len(live_fakes.FETCH_CALLS), fetches_before)
        self.assertEqual(len(live_fakes.LIVE_SENDS), 0)
        status, data = self.client.post("/api/monitor/stop")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "stopped")
        # One run per process: a restart is refused even with confirm.
        status, data = self.client.post("/api/monitor/start", {"confirm": True})
        self.assertEqual(status, 409)



class TestSetupConfig(WebAppTestCase):
    """Setup page contract: ONLY the recipient email is set via the web UI.

    SMTP sender details and the DeepSeek API key are operator-level settings
    maintained in .env directly — the page can never write them.
    """

    def test_status_reports_missing_keys_never_values(self):
        self.env_file.write_text("", encoding="utf-8")  # hermetic: nothing configured
        status, data, _ = self.client.get("/api/setup/status")
        self.assertEqual(status, 200)
        self.assertFalse(data["complete"])
        self.assertFalse(data["smtp_ready"])
        self.assertFalse(data["llm_ready"])
        for key in config_setup.REQUIRED_KEYS:
            self.assertIn(key, data["missing"])
        # No config value may ever appear in the response.
        self.assertNotIn("=", json.dumps(data))

    def test_recipient_save_persists_to_the_app_local_env_file(self):
        self.env_file.write_text("", encoding="utf-8")
        status, data = self.client.post(
            "/api/setup", {"DEFAULT_NOTIFY_EMAIL": "someone@example.com"})
        self.assertEqual(status, 200)
        self.assertIn("DEFAULT_NOTIFY_EMAIL", data["saved"])
        self.assertFalse(data["smtp_ready"])   # UI must explain this
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("DEFAULT_NOTIFY_EMAIL=someone@example.com", text)
        # Values are applied for the running process without a restart.
        status, data2, _ = self.client.get("/api/setup/status")
        self.assertEqual(data2["missing"],
                         list(config_setup.SMTP_KEYS + config_setup.LLM_KEYS))

    def test_smtp_and_llm_keys_cannot_be_written_through_the_web_ui(self):
        status, data = self.client.post("/api/setup", {
            "DEFAULT_NOTIFY_EMAIL": "someone@example.com",
            "SMTP_HOST": "smtp.example.com", "SMTP_PASS": "unit-test-app-pass",
            "LLM_API_KEY": "unit-test-key",
        })
        self.assertEqual(status, 400)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertNotIn("smtp.example.com", text)
        self.assertNotIn("unit-test-key", text)

    def test_smtp_and_llm_presence_comes_from_the_env_file(self):
        self.env_file.write_text(
            "DEFAULT_NOTIFY_EMAIL=someone@example.com\n"
            "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\n"
            "SMTP_USER=someone@example.com\nSMTP_PASS=unit-test-app-pass\n"
            "LLM_API_KEY=unit-test-key\n"
            "LLM_API_BASE_URL=https://api.deepseek.com\nLLM_MODEL=deepseek-chat\n",
            encoding="utf-8")
        status, data, _ = self.client.get("/api/setup/status")
        self.assertTrue(data["smtp_ready"])
        self.assertTrue(data["llm_ready"])
        self.assertTrue(data["complete"])

    def test_setup_save_with_no_fields_is_400(self):
        status, data = self.client.post("/api/setup", {})
        self.assertEqual(status, 400)

    def test_setup_save_rejects_a_malformed_email(self):
        status, data = self.client.post("/api/setup", {"DEFAULT_NOTIFY_EMAIL": "not-an-email"})
        self.assertEqual(status, 400)


class TestSourceKindThroughWebApp(WebAppTestCase):
    """Discovery expansion: source kinds survive preview/confirm; demo stays offline."""

    def test_demo_search_keeps_the_web_search_source_out_of_the_flow(self):
        # Demo mode is fully offline: not even an "unavailable" web-search row.
        data = self._search()
        self.assertEqual(data["status"], "ready")
        retailers = [s["retailer"] for s in data["result"]["sources"]]
        self.assertNotIn("Brave Web Search", retailers)

    def test_confirmed_demo_task_sources_carry_a_kind(self):
        task_id = self._select_two()
        status, detail, _ = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertTrue(detail["sources"])
        for source in detail["sources"]:
            self.assertEqual(source["source_kind"], "retailer_adapter")

    def test_preview_sources_carry_a_kind(self):
        data, picked = self._pick_two()
        status, preview = self.client.post("/api/search/select",
                                           {"token": data["token"], "urls": picked})
        self.assertEqual(status, 200)
        for source in preview["preview"]["sources"]:
            self.assertIn("source_kind", source)
            self.assertEqual(source["source_kind"], "retailer_adapter")


if __name__ == "__main__":
    unittest.main()
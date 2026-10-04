"""Stage-D web regressions for the intelligent-find ("iterative discovery") mode.

Two layers are covered, both fully offline:

* ``TestFindDemoFlow`` drives the real HTTP server in demo mode. Its find
  collaborators are the deterministic demo brain (no model, no web, no fetch), so
  the loop, validation, handoff and the existing select/confirm chain are the
  real code paths. The key guarantees pinned here are: the find routes exist and
  are reachable; no task is created before the *existing final* confirmation;
  a chosen recommendation becomes a specific product request that retains the
  description's constraints; and a Chinese colour in the description (which the
  derived English product name would otherwise drop) still validates.

* ``TestFindLiveModeOffline`` drives a live-mode server (demo=False) on a
  throwaway database with every external collaborator faked process-wide. It
  pins the ``LLM_FIND_ENABLED`` gate, the evidence-backed recommendations, the
  re-fetch of the *chosen* product's page when discovery finished before pricing
  it, and the "no DB write before the final confirmation" boundary in live mode.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import error as urlerror

import config_setup
import demo_data
import find_pipeline
import iterative_discovery
import storage

import tests.test_webapp as webapp_tests
from tests import fake_server as live_fakes
from tests.test_webapp import ServerHarness, WebAppTestCase


DEMO_DESCRIPTION = "wireless running earbuds, black, budget NZD 200, check every 60 minutes"
ZH_DESCRIPTION = "找 AeroBuds Pro 黑色 预算 200 纽币 每 60 分钟查一次"


def _post_with_retry(client, path, payload, **kwargs):
    """POST that a write guard may reject (403).

    The guard runs before the body is read, so on Windows the server can send the
    403 and close the socket while unread body bytes are still in flight; the
    client then sees a transient connection reset. The rejection is
    side-effect-free, so retry a couple of times.
    """
    last_error = None
    for _ in range(3):
        try:
            return client.post(path, payload, **kwargs)
        except (urlerror.URLError, ConnectionError) as exc:
            last_error = exc
    raise last_error


class TestFindDemoFlow(WebAppTestCase):
    """The find flow over the shared demo server (offline, no model/web/fetch)."""

    def setUp(self):
        super().setUp()
        demo_data.RECORDED_SENDS.clear()

    # -- helpers ---------------------------------------------------------
    def _find(self, description=DEMO_DESCRIPTION):
        status, data = self.client.post("/api/find", {"description": description})
        self.assertEqual(status, 200)
        return data

    def _task_ids(self):
        return [t["id"] for t in storage.list_tasks(db_path=self.db_path)]

    # -- reachability / shape --------------------------------------------
    def test_index_exposes_the_find_section_and_route(self):
        import webapp

        html = webapp.build_index_html()
        self.assertIn('id="findSection"', html)
        self.assertIn("/api/find", html)

    def test_find_returns_concrete_evidence_backed_recommendations(self):
        data = self._find()
        self.assertEqual(data["status"], "ready")
        self.assertTrue(data["token"])
        recs = data["recommendations"]
        self.assertEqual(len(recs), 4)
        prices = sorted(r["price"] for r in recs)
        self.assertEqual(prices, ["179.00", "189.00", "19.00", "199.00"])
        for rec in recs:
            self.assertTrue(rec["provenance_id"])
            self.assertTrue(rec["url"].startswith("https://"))
            self.assertEqual(rec["currency"], "NZD")
            self.assertTrue(rec["fetched"])

    def test_find_progress_and_limits_are_reported(self):
        data = self._find()
        progress = data["progress"]
        self.assertEqual(progress["finished_reason"], "pipeline_complete")
        self.assertEqual(progress["searches"], 2)
        self.assertGreaterEqual(progress["detail_fetches"], 4)
        self.assertEqual(data["limits"]["max_rounds"], 8)
        self.assertEqual(data["limits"]["max_searches"], 16)
        self.assertEqual(data["limits"]["max_detail_fetches"], 24)

    # -- write guards apply to both find routes --------------------------
    def test_find_routes_require_csrf(self):
        status, data = _post_with_retry(self.client, "/api/find",
                                        {"description": DEMO_DESCRIPTION}, csrf=None)
        self.assertEqual(status, 403)
        status, data = _post_with_retry(self.client, "/api/find/confirm",
                                        {"token": "x", "provenance_id": "e1"}, csrf=None)
        self.assertEqual(status, 403)

    def test_find_routes_require_origin(self):
        status, _ = _post_with_retry(self.client, "/api/find",
                                     {"description": DEMO_DESCRIPTION}, origin=None)
        self.assertEqual(status, 403)
        status, _ = _post_with_retry(self.client, "/api/find/confirm",
                                     {"token": "x", "provenance_id": "e1"}, origin=None)
        self.assertEqual(status, 403)

    def test_find_routes_reject_foreign_origin_and_host(self):
        status, _ = _post_with_retry(self.client, "/api/find",
                                     {"description": DEMO_DESCRIPTION},
                                     origin="http://evil.example")
        self.assertEqual(status, 403)
        status, _ = _post_with_retry(self.client, "/api/find",
                                     {"description": DEMO_DESCRIPTION}, host="evil.example:80")
        self.assertEqual(status, 403)

    # -- input validation -------------------------------------------------
    def test_find_empty_description_is_400(self):
        status, data = self.client.post("/api/find", {"description": "   "})
        self.assertEqual(status, 400)
        self.assertIn("describe", data["error"].lower())

    def test_find_overlong_description_is_400(self):
        status, data = self.client.post("/api/find", {"description": "x" * 401})
        self.assertEqual(status, 400)

    def test_find_confirm_without_token_or_provenance_is_400(self):
        status, data = self.client.post("/api/find/confirm", {"token": "", "provenance_id": ""})
        self.assertEqual(status, 400)

    def test_find_confirm_with_unknown_token_is_404(self):
        status, data = self.client.post("/api/find/confirm",
                                        {"token": "nope", "provenance_id": "e1"})
        self.assertEqual(status, 404)

    def test_find_confirm_with_unknown_provenance_is_400(self):
        data = self._find()
        status, resp = self.client.post("/api/find/confirm",
                                        {"token": data["token"], "provenance_id": "e999"})
        self.assertEqual(status, 400)
        self.assertIn("not among the discovered", resp["error"])

    # -- no task before the existing final confirmation ------------------
    def test_find_creates_no_task(self):
        before = self._task_ids()
        self._find()
        self.assertEqual(self._task_ids(), before)

    def test_find_confirm_creates_no_task(self):
        data = self._find()
        before = self._task_ids()
        rec = data["recommendations"][1]  # a colour-confirmed black offer
        status, resp = self.client.post("/api/find/confirm",
                                        {"token": data["token"], "provenance_id": rec["provenance_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self._task_ids(), before)
        self.assertEqual(resp["status"], "ready")

    def test_find_confirm_then_preview_still_creates_no_task(self):
        data = self._find()
        rec = data["recommendations"][1]
        _, resp = self.client.post("/api/find/confirm",
                                   {"token": data["token"], "provenance_id": rec["provenance_id"]})
        selectable = resp["result"]["selectable"]
        self.assertGreaterEqual(len(selectable), 1)
        urls = [resp["result"]["offers"][i - 1]["url"] for i in selectable]
        before = self._task_ids()
        status, preview = self.client.post("/api/search/select",
                                           {"token": resp["token"], "urls": urls})
        self.assertEqual(status, 200)
        self.assertIn("preview", preview)
        self.assertNotIn("created", preview)
        self.assertEqual(self._task_ids(), before)

    def test_full_chain_creates_exactly_one_task_after_final_confirm(self):
        data = self._find()
        rec = data["recommendations"][1]
        _, resp = self.client.post("/api/find/confirm",
                                   {"token": data["token"], "provenance_id": rec["provenance_id"]})
        selectable = resp["result"]["selectable"]
        urls = [resp["result"]["offers"][i - 1]["url"] for i in selectable]
        before = self._task_ids()
        status, created = self.client.post("/api/search/confirm",
                                           {"token": resp["token"], "urls": urls})
        self.assertEqual(status, 200)
        after = self._task_ids()
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(created["created"], after[-1])

    # -- specific request retains the description's constraints ----------
    def test_confirm_recommendation_retains_constraints_and_uses_evidence_name(self):
        data = self._find()
        rec = data["recommendations"][1]
        status, resp = self.client.post("/api/find/confirm",
                                        {"token": data["token"], "provenance_id": rec["provenance_id"]})
        self.assertEqual(status, 200)
        request = resp["request"]
        self.assertEqual(request["product_name"], "AeroBuds Pro Wireless Earbuds - Black")
        self.assertEqual(request["budget"], "200.00")
        self.assertEqual(request["currency"], "NZD")
        self.assertEqual(request["colour"], "black")
        self.assertEqual(request["frequency_minutes"], 60)
        self.assertEqual(resp["chosen"]["provenance_id"], rec["provenance_id"])

    def test_chinese_description_confirm_retains_colour(self):
        # Regression: the anti-invention guard checks a constraint against the
        # instruction. The derived English product name does not contain the
        # Chinese colour word, so the description itself must be used.
        data = self._find(ZH_DESCRIPTION)
        self.assertEqual(data["status"], "ready")
        rec = data["recommendations"][0]
        status, resp = self.client.post("/api/find/confirm",
                                        {"token": data["token"], "provenance_id": rec["provenance_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(resp["request"]["colour"], "黑色")
        self.assertEqual(resp["request"]["budget"], "200.00")

    # -- deterministic demo brain never gates the descriptive query -------
    def test_vague_description_still_returns_concrete_products(self):
        data = self._find("something to listen to music with while running")
        self.assertEqual(data["status"], "ready")
        self.assertTrue(data["recommendations"])

    # -- adversarial tool output / partial results -----------------------
    def test_adversarial_search_output_never_500s_and_invents_nothing(self):
        def junk_search(query):
            return [
                None,
                "not a dict",
                {"nourl": 1},
                {"url": "javascript:alert(1)", "title": "bad"},
                {"url": "https://ok.example/p", "title": "Good Product"},
            ]

        def rewrite(payload):
            return ["anything"]

        with patch.object(demo_data, "demo_find_rewrite", rewrite), \
                patch.object(demo_data, "demo_find_search", junk_search):
            data = self._find("anything at all")
        self.assertEqual(data["status"], "ready")
        recs = data["recommendations"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["url"], "https://ok.example/p")
        self.assertIsNone(recs[0]["price"])   # a snippet is never a price
        self.assertFalse(recs[0]["fetched"])

    def test_partial_result_is_surfaced_and_keeps_evidence(self):
        def flaky_select(payload):
            raise RuntimeError("model provider down")

        def empty_describe(payload):
            return {}

        with patch.object(demo_data, "demo_find_select", flaky_select), \
                patch.object(demo_data, "demo_find_describe", empty_describe):
            data = self._find()
        # The select stage degrades to evidence order; results are still produced.
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["stages"]["select"], "fallback")
        self.assertGreaterEqual(len(data["recommendations"]), 1)
        self.assertTrue(any("Select stage failed" in n for n in data["notes"]))


def _live_evidence_without_fetches(notes):
    """Stage-3 replacement that records the live search result but never reads a
    detail page, so the confirm step must fetch the chosen product itself."""
    evidence = {}
    order = []
    url_to_pid = {}
    import iterative_discovery as itd

    added = itd._add_search_candidates(
        live_fakes.fake_brave_search("AeroBuds Pro black"),
        query="AeroBuds Pro black",
        round_no=1,
        next_id=1,
        evidence=evidence,
        order=order,
        url_to_pid=url_to_pid,
    )
    return evidence, order, 1, 0


class TestFindLiveModeOffline(unittest.TestCase):
    """Live-mode find flow with every external collaborator faked process-wide."""

    LIVE_NOTIFY = live_fakes.LIVE_NOTIFY_EMAIL

    @classmethod
    def setUpClass(cls):
        # Isolate the setup-config file so no test can touch the developer's .env.
        cls._prev_env_override = os.environ.get("BARGAIN_AGENT_ENV_FILE")
        fd, env_name = tempfile.mkstemp(prefix="find-live-env-", suffix=".txt")
        os.close(fd)
        cls.env_file = Path(env_name)
        cls.env_file.write_text("", encoding="utf-8")
        cls.env_patch = patch.dict(os.environ, {
            "BARGAIN_AGENT_ENV_FILE": str(cls.env_file),
            "DEFAULT_NOTIFY_EMAIL": live_fakes.LIVE_NOTIFY_EMAIL,
            "LLM_FIND_ENABLED": "1",
        })
        cls.env_patch.start()
        # Real keys in the isolated file make config_setup.status()["llm_ready"]
        # true without patching it out.
        config_setup.write_updates({
            "LLM_API_KEY": "test-key-not-real",
            "LLM_API_BASE_URL": "https://example.invalid/v1",
            "LLM_MODEL": "test-model",
            # These flow tests are not about the master-AI concreteness judge:
            # switching it off keeps the deterministic heuristic (the thing
            # being asserted) and guarantees no live LLM call is attempted
            # with the placeholder credential.
            "LLM_CONCRETENESS": "false",
        })

        fd, db_name = tempfile.mkstemp(prefix="webui-find-live-", suffix=".db")
        os.close(fd)
        cls.db_path = Path(db_name)

        cls.fakes = live_fakes.install_live_fakes()
        # The find module resolves its own collaborators at call time; patch the
        # ones install_live_fakes does not cover, recording every detail fetch so
        # the chosen-product re-fetch can be proven.
        cls.fetch_calls = []

        def recording_fetch(url):
            cls.fetch_calls.append(url)
            return live_fakes.fake_web_offer(url)

        cls.find_patches = [
            patch.object(find_pipeline, "_merged_search", live_fakes.fake_brave_search),
            patch.object(iterative_discovery, "_default_fetch", recording_fetch),
            patch.object(find_pipeline, "run_understand",
                         lambda description, **kw: demo_data.demo_find_understand(description)),
            patch.object(find_pipeline, "run_rewrite",
                         lambda description, understanding, **kw: demo_data.demo_find_rewrite(
                             {"description": description, "need": understanding or {}})),
            patch.object(find_pipeline, "run_select",
                         lambda description, understanding, evidence, order, **kw:
                             demo_data.demo_find_select(
                                 {"description": description, "need": understanding or {},
                                  "evidence": [evidence[pid] for pid in order]})),
            patch.object(find_pipeline, "run_describe",
                         lambda description, understanding, recommendations, **kw:
                             demo_data.demo_find_describe(
                                 {"description": description, "need": understanding or {},
                                  "products": recommendations})),
        ]
        for p in cls.find_patches:
            p.start()

        cls.harness = ServerHarness(demo=False, db_path=cls.db_path)
        cls.harness.start_csrf()
        cls.client = cls.harness.client

    @classmethod
    def tearDownClass(cls):
        cls.harness.stop()
        for p in cls.find_patches:
            p.stop()
        cls.fakes.close()
        cls.env_patch.stop()
        if cls._prev_env_override is None:
            os.environ.pop("BARGAIN_AGENT_ENV_FILE", None)
        else:
            os.environ["BARGAIN_AGENT_ENV_FILE"] = cls._prev_env_override
        cls.env_file.unlink(missing_ok=True)
        try:
            os.unlink(cls.db_path)
        except OSError:
            pass

    def setUp(self):
        self.fetch_calls.clear()

    def _find(self, description=DEMO_DESCRIPTION):
        status, data = self.client.post("/api/find", {"description": description})
        self.assertEqual(status, 200)
        return data

    def _task_ids(self):
        return [t["id"] for t in storage.list_tasks(db_path=self.db_path)]

    def test_live_find_flag_off_is_400(self):
        with patch.dict(os.environ, {"LLM_FIND_ENABLED": ""}):
            status, data = self.client.post("/api/find", {"description": DEMO_DESCRIPTION})
        self.assertEqual(status, 400)
        self.assertIn("LLM_FIND_ENABLED", data["error"])

    def test_live_find_requires_write_guards(self):
        status, _ = _post_with_retry(self.client, "/api/find",
                                     {"description": DEMO_DESCRIPTION}, csrf=None)
        self.assertEqual(status, 403)
        status, _ = _post_with_retry(self.client, "/api/find",
                                     {"description": DEMO_DESCRIPTION}, origin=None)
        self.assertEqual(status, 403)
        status, _ = _post_with_retry(self.client, "/api/find",
                                     {"description": DEMO_DESCRIPTION}, host="evil.example:80")
        self.assertEqual(status, 403)

    def test_live_find_returns_evidence_backed_recommendations(self):
        data = self._find()
        self.assertEqual(data["status"], "ready")
        recs = data["recommendations"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["url"], "https://webshop.example/aerobuds-pro-black")
        self.assertEqual(recs[0]["price"], "179.00")
        self.assertTrue(recs[0]["source"] in ("web_search", "Brave Web Search"))

    def test_live_find_confirm_creates_no_task_then_chain_creates_one(self):
        data = self._find()
        rec = data["recommendations"][0]
        before = self._task_ids()
        status, resp = self.client.post("/api/find/confirm",
                                        {"token": data["token"], "provenance_id": rec["provenance_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(resp["request"]["product_name"], "AeroBuds Pro Wireless Earbuds - Black")
        self.assertEqual(resp["request"]["budget"], "200.00")
        self.assertEqual(resp["request"]["colour"], "black")
        self.assertEqual(self._task_ids(), before)
        # The chosen web offer plus the two shops' black offers are selectable.
        selectable = resp["result"]["selectable"]
        self.assertGreaterEqual(len(selectable), 3)
        urls = [resp["result"]["offers"][i - 1]["url"] for i in selectable]
        status, created = self.client.post("/api/search/confirm",
                                           {"token": resp["token"], "urls": urls})
        self.assertEqual(status, 200)
        after = self._task_ids()
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(len(created["task"]["sources"]), len(urls))

    def test_live_find_confirm_refetches_the_chosen_page_when_not_yet_priced(self):
        # No detail fetch during discovery: the chosen product's price must come
        # from a real (fake) fetch of its observed URL at confirm time.
        with patch.object(find_pipeline, "run_search_and_fetch",
                          lambda queries, **kw: _live_evidence_without_fetches(kw["notes"])):
            data = self._find()
        rec = data["recommendations"][0]
        self.assertIsNone(rec["price"])
        self.assertFalse(rec["fetched"])
        self.fetch_calls.clear()
        status, resp = self.client.post("/api/find/confirm",
                                        {"token": data["token"], "provenance_id": rec["provenance_id"]})
        self.assertEqual(status, 200)
        self.assertIn("https://webshop.example/aerobuds-pro-black", self.fetch_calls)
        priced = [o for o in resp["result"]["offers"] if o.get("url") == rec["url"]]
        self.assertEqual(len(priced), 1)
        self.assertEqual(priced[0]["price"], "179.00")

    def test_live_find_does_not_write_to_the_database_before_final_confirm(self):
        before = len(self._task_ids())
        data = self._find()
        rec = data["recommendations"][0]
        self.client.post("/api/find/confirm",
                         {"token": data["token"], "provenance_id": rec["provenance_id"]})
        # A discovery run plus a specific-request comparison must leave the DB
        # untouched: only /api/search/confirm (the existing final step) may write.
        self.assertEqual(len(self._task_ids()), before)


if __name__ == "__main__":
    unittest.main()

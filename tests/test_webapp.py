"""Offline tests for the local web interface.

The real HTTP server runs on 127.0.0.1 with a temporary database and fake
services (find pipeline, SerpApi offers, observer, sender, task parser), so no
network, model or SMTP call can happen.
"""

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import error as urlerror
from urllib import request as urlrequest

import storage
import webapp
from sources.base import Offer, ProductRef, RateLimited

EMAIL = "user@example.com"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """A tiny HTTP client holding one CSRF session (cookie + header)."""

    def __init__(self, base: str):
        self.base = base
        self.csrf = None
        self.cookie = None
        self.origin = base

    def _capture(self, headers) -> None:
        set_cookie = headers.get("Set-Cookie")
        if set_cookie and set_cookie.startswith("csrftoken="):
            self.cookie = set_cookie.split(";", 1)[0]
            self.csrf = self.cookie.split("=", 1)[1]

    def get(self, path: str):
        req = urlrequest.Request(self.base + path, headers={"Cookie": self.cookie} if self.cookie else {})
        try:
            with urlrequest.urlopen(req, timeout=10) as resp:
                self._capture(resp.headers)
                body = resp.read().decode("utf-8")
                return resp.status, (json.loads(body) if "json" in resp.headers.get("Content-Type", "") else body)
        except urlerror.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(self, path, payload=None, *, raw=None, csrf="auto", origin="auto", host=None, cookie="auto"):
        headers = {"Content-Type": "application/json"}
        csrf = self.csrf if csrf == "auto" else csrf
        origin = self.origin if origin == "auto" else origin
        cookie = self.cookie if cookie == "auto" else cookie
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        if origin is not None:
            headers["Origin"] = origin
        if cookie is not None:
            headers["Cookie"] = cookie
        if host:
            headers["Host"] = host
        data = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
        req = urlrequest.Request(self.base + path, data=data, method="POST", headers=headers)
        try:
            with urlrequest.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


def rec(rid, title, *, price="229.00", product_id="111", merchant="Noel Leeming", link=None, trusted=True):
    return {
        "id": rid, "title": title, "link": link or f"https://www.google.co.nz/shopping/product/{product_id or rid}",
        "merchant": merchant, "price": price, "currency": "NZD", "price_source": "serpapi", "product_id": product_id,
        "page_token": f"tok-{product_id}" if product_id else None, "thumbnail": None, "condition": None,
        "provider": "serpapi", "trusted": trusted, "search_query": "fitbit charge 6", "monitorable": bool(product_id),
        "summary": "简介",
    }


FIND_RESULT = {
    "status": "ready",
    "description": "fitbit under 250",
    "need": {"category": "fitness tracker", "budget": "250.00", "currency": "NZD"},
    "queries": ["fitbit charge 6"],
    "recommendations": [
        rec("e1", "Fitbit Charge 6"),
        rec("e2", "Fitbit Inspire | Small Shop", product_id=None, merchant="small.example.nz",
            link="https://small.example.nz/inspire", trusted=False, price="99.00"),
    ],
    "evidence": [
        {"id": "e1", "title": "Fitbit Charge 6", "merchant": "Noel Leeming", "price": "229.00", "currency": "NZD",
         "product_id": "111", "link": "https://www.google.co.nz/shopping/product/111", "trusted": True},
        {"id": "e3", "title": "Fitbit Charge 6 Obsidian", "merchant": "PB Tech", "price": "198.00", "currency": "NZD",
         "product_id": "333", "link": "https://g.example/333", "trusted": True},
        {"id": "e4", "title": "Fitbit Charge 6", "merchant": "JB Hi-Fi", "price": "219.00", "currency": "NZD",
         "product_id": "444", "link": "https://g.example/444", "trusted": True},
        {"id": "e5", "title": "Fitbit Charge 6", "merchant": "Random Shop", "price": "189.00", "currency": "NZD",
         "product_id": "555", "link": "https://g.example/555", "trusted": False},
        {"id": "e6", "title": "Fitbit Charge 6 Band", "merchant": "Strap Shop", "price": "19.00", "currency": "NZD",
         "product_id": "666", "link": "https://g.example/666", "trusted": False},
    ],
    "stages": {"understand": "ok", "rewrite": "ok", "search_1": "ok", "select": "ok", "describe": "ok"},
    "notes": [],
    "usage": {"searches": 1},
}

OFFERS = [
    Offer("Noel Leeming", "229.00", "NZD", "https://nl.example.nz/p", "serpapi", "Fitbit Charge 6", in_stock=True),
    Offer("JB Hi-Fi", "219.00", "NZD", "https://jb.example.nz/p", "serpapi", "Fitbit Charge 6", in_stock=True),
    Offer("Random Shop", "150.00", "NZD", "https://rand.example.nz/p", "serpapi", "Fitbit Charge 6", in_stock=True),
    Offer("Reseller", "120.00", "NZD", "https://re.example.nz/p", "serpapi", "Fitbit Charge 6", condition="refurbished"),
]


class Fakes:
    def __init__(self):
        self.finds = []
        self.offer_refs = []
        self.extra_queries = []
        self.offers_response = list(OFFERS)
        self.sent = []
        self.observations = [
            {"product_name": "Fitbit Charge 6", "retailer": "JB Hi-Fi", "url": "https://jb.example.nz/p",
             "price": "219.00", "currency": "NZD", "in_stock": True, "trusted": True},
        ]

    def find(self, description):
        self.finds.append(description)
        return json.loads(json.dumps({**FIND_RESULT, "description": description}))

    def product_offers(self, ref):
        self.offer_refs.append(ref)
        if isinstance(self.offers_response, Exception):
            raise self.offers_response
        return self.offers_response, ProductRef(ref.product_id, "tok-fresh", ref.search_query, ref.title)

    def extra_search(self, query):
        self.extra_queries.append(query)
        return []

    def observer(self, source, task):
        return [dict(q) for q in self.observations]

    def send(self, to, task, quote):
        self.sent.append((to, quote))
        return {"success": True}

    def services(self):
        return webapp.Services(
            find=self.find, product_offers=self.product_offers, extra_search=self.extra_search,
            observer=self.observer, verifier=None,
            send_fn=self.send, parse_task=self.parse_task,
            provider_status=lambda: {"shopping": "serpapi", "extractors": "tavily"},
        )

    def parse_task(self, instruction):
        if "unclear" in instruction:
            return {"status": "needs_clarification", "missing_fields": ["target_price"], "clarification_question": "价格？"}
        return {"status": "ready", "product_name": "Kindle", "url": "https://shop.example.nz/kindle",
                "target_price": "250.00", "currency": "NZD", "comparison": "lt", "frequency_minutes": 60, "condition": None}


class WebCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "web.db"
        self.env_file = Path(self.dir.name) / ".env"
        self.env = patch.dict(os.environ, {"DEFAULT_NOTIFY_EMAIL": EMAIL, "BARGAIN_AGENT_ENV_FILE": str(self.env_file)})
        self.env.start()
        self.fakes = Fakes()
        self.port = _free_port()
        self.httpd = webapp.serve(self.db, port=self.port, services=self.fakes.services())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(f"http://127.0.0.1:{self.port}")
        self.client.get("/api/csrf")

    def tearDown(self):
        state = self.httpd.app_state
        if state.monitor.get("scheduler") is not None:
            state.monitor["scheduler"].stop()
            state.monitor["thread"].join(timeout=10)
        import scheduler

        scheduler._active_scheduler = None
        scheduler._scheduler_has_run = False
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.stop()
        self.dir.cleanup()

    def find(self):
        status, data = self.client.post("/api/find", {"description": "fitbit under 250"})
        self.assertEqual(status, 200, data)
        return data

    def create_product_task(self, **overrides):
        token = self.find()["token"]
        body = {"token": token, "rec_id": "e1", "target_price": "230", "comparison": "lte", "frequency_minutes": 1440, **overrides}
        status, data = self.client.post("/api/find/confirm", body)
        self.assertEqual(status, 200, data)
        return token, data


class TestWriteGuards(WebCase):
    def test_index_sets_csrf_cookie(self):
        client = Client(self.client.base)
        status, body = client.get("/")
        self.assertEqual(status, 200)
        self.assertIn("<html", body.lower())
        self.assertIsNotNone(client.csrf)

    def test_post_requires_csrf_origin_host_and_cookie(self):
        cases = [
            {"csrf": None}, {"csrf": "wrong"}, {"origin": None}, {"origin": "http://evil.example"},
            {"host": "evil.example"}, {"cookie": None},
        ]
        for kwargs in cases:
            status, data = self.client.post("/api/find", {"description": "x"}, **kwargs)
            self.assertEqual(status, 403, kwargs)
        self.assertEqual(self.fakes.finds, [])

    def test_body_limits(self):
        self.assertEqual(self.client.post("/api/find", raw=b"x" * (64 * 1024 + 1))[0], 400)
        self.assertEqual(self.client.post("/api/find", raw=b"[1]")[0], 400)
        self.assertEqual(self.client.post("/api/find", raw=b"{nope")[0], 400)

    def test_unknown_routes(self):
        self.assertEqual(self.client.get("/api/nope")[0], 404)
        self.assertEqual(self.client.post("/api/search", {})[0], 404)
        self.assertEqual(self.client.post("/api/tasks/1/explode", {})[0], 404)


class TestFindFlow(WebCase):
    def test_find_returns_recommendations_and_writes_nothing(self):
        data = self.find()
        self.assertEqual([r["id"] for r in data["recommendations"]], ["e1", "e2"])
        self.assertEqual(data["stages"]["select"], "ok")
        self.assertEqual(storage.list_tasks(db_path=self.db), [])

    def test_async_find_job_reports_status_then_result(self):
        import time

        status, started = self.client.post("/api/find", {"description": "fitbit under 250", "async": True})
        self.assertEqual(status, 200)
        for _ in range(50):
            state = self.client.get(f"/api/find/status?job={started['job']}")[1]
            if state["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(state["status"], "done")
        self.assertEqual([r["id"] for r in state["response"]["recommendations"]], ["e1", "e2"])
        preview = self.client.post("/api/find/preview", {"token": state["response"]["token"], "rec_id": "e1"})
        self.assertEqual(preview[0], 200)
        self.assertEqual(self.client.get("/api/find/status?job=nope")[0], 404)

    def test_refine_accumulates_follow_ups(self):
        token = self.find()["token"]
        first = self.client.post("/api/find/refine", {"token": token, "message": "only black"})[1]
        self.assertEqual(first["followups"], ["only black"])
        self.assertEqual(self.fakes.finds[-1], "fitbit under 250" + chr(10) + "Follow-up: only black")
        second = self.client.post("/api/find/refine", {"token": first["token"], "message": "cheaper"})[1]
        self.assertEqual(second["followups"], ["only black", "cheaper"])
        self.assertEqual(second["original_description"], "fitbit under 250")
        self.assertEqual(self.client.post("/api/find/refine", {"token": token, "message": " "})[0], 400)
        self.assertEqual(self.client.post("/api/find/refine", {"token": "gone", "message": "x"})[0], 404)
        job = self.client.post("/api/find/refine", {"token": token, "message": "white", "async": True})[1]
        self.assertIn("job", job)

    def test_find_validation(self):
        self.assertEqual(self.client.post("/api/find", {"description": "  "})[0], 400)
        self.assertEqual(self.client.post("/api/find", {"description": "x" * 401})[0], 400)

    def test_preview_lists_every_seller_and_suggests_target(self):
        token = self.find()["token"]
        status, data = self.client.post("/api/find/preview", {"token": token, "rec_id": "e1"})
        self.assertEqual(status, 200, data)
        preview = data["preview"]
        self.assertEqual([o["merchant"] for o in preview["offers"]], ["Noel Leeming", "JB Hi-Fi", "Random Shop", "Reseller"])
        self.assertEqual([o["trusted"] for o in preview["offers"]], [True, True, False, False])
        self.assertEqual(preview["best"]["merchant"], "PB Tech")  # pre-selected match at 198
        self.assertEqual(preview["suggested_target"], "250.00")
        self.assertEqual(preview["source_kind"], "serpapi_product")
        self.client.post("/api/find/preview", {"token": token, "rec_id": "e1"})
        self.assertEqual(len(self.fakes.offer_refs), 1, "preview offers are cached per find result")
        self.assertEqual(storage.list_tasks(db_path=self.db), [])

    def test_preview_of_unmonitorable_listing_uses_its_own_link(self):
        token = self.find()["token"]
        preview = self.client.post("/api/find/preview", {"token": token, "rec_id": "e2"})[1]["preview"]
        self.assertEqual(preview["source_kind"], "url")
        self.assertEqual(self.fakes.offer_refs, [])

    def test_preview_detail_error_falls_back_to_the_listing(self):
        self.fakes.offers_response = RateLimited("SerpApi quota exhausted")
        token = self.find()["token"]
        status, data = self.client.post("/api/find/preview", {"token": token, "rec_id": "e1"})
        self.assertEqual(status, 200)
        self.assertEqual([o["merchant"] for o in data["preview"]["offers"]], ["Noel Leeming"])

    def test_preview_lists_same_product_at_other_merchants(self):
        token = self.find()["token"]
        preview = self.client.post("/api/find/preview", {"token": token, "rec_id": "e1"})[1]["preview"]
        # verifier off -> only rule-passing trusted merchants; strap and unverified shop excluded.
        self.assertEqual([m["merchant"] for m in preview["matches"]], ["PB Tech", "JB Hi-Fi"])
        self.assertTrue(all(m["preselect"] for m in preview["matches"]))
        self.assertEqual(preview["max_sources"], 5)
        self.assertEqual(self.fakes.extra_queries, [])  # enough candidates already: no extra search
        self.assertEqual(preview["best"]["merchant"], "PB Tech")

    def test_extra_search_runs_once_when_candidates_are_few(self):
        FIND_RESULT_FEW = dict(FIND_RESULT, evidence=FIND_RESULT["evidence"][:1])
        self.fakes.find = lambda d: json.loads(json.dumps(FIND_RESULT_FEW))
        self.httpd.app_state.services.find = self.fakes.find
        token = self.find()["token"]
        self.client.post("/api/find/preview", {"token": token, "rec_id": "e1"})
        self.client.post("/api/find/preview", {"token": token, "rec_id": "e1"})
        self.assertEqual(self.fakes.extra_queries, ["Fitbit Charge 6"])

    def test_confirm_with_matches_creates_one_task_with_several_merchants(self):
        token = self.find()["token"]
        matches = self.client.post("/api/find/preview", {"token": token, "rec_id": "e1"})[1]["preview"]["matches"]
        ids = [m["id"] for m in matches]
        data = self.client.post("/api/find/confirm", {"token": token, "rec_id": "e1", "target_price": "210", "extra_ids": ids})[1]
        sources = storage.list_task_sources(data["created"], db_path=self.db)
        self.assertEqual([s["retailer"] for s in sources], ["Noel Leeming", "PB Tech", "JB Hi-Fi"])
        self.assertEqual({s["search_query"] for s in sources}, {"Fitbit Charge 6"})
        self.assertEqual([s["product_id"] for s in sources], ["111", "333", "444"])

    def test_confirm_rejects_unknown_match_ids_and_caps_sources(self):
        token = self.find()["token"]
        self.client.post("/api/find/preview", {"token": token, "rec_id": "e1"})
        status, _ = self.client.post("/api/find/confirm", {"token": token, "rec_id": "e1", "target_price": "1", "extra_ids": ["e5"]})
        self.assertEqual(status, 400)
        status, _ = self.client.post("/api/find/confirm", {"token": token, "rec_id": "e1", "target_price": "1", "extra_ids": "e3"})
        self.assertEqual(status, 400)
        with patch("config.monitor_max_sources", return_value=2):
            data = self.client.post("/api/find/confirm", {"token": token, "rec_id": "e1", "target_price": "1",
                                                          "extra_ids": ["e3", "e4"]})[1]
        self.assertEqual(len(storage.list_task_sources(data["created"], db_path=self.db)), 2)

    def test_confirm_creates_one_product_task_idempotently(self):
        token, data = self.create_product_task()
        task = data["task"]
        self.assertEqual((task["target_price"], task["currency"], task["frequency_minutes"]), ("230.00", "NZD", 1440))
        self.assertEqual(task["sources"][0]["source_kind"], "serpapi_product")
        source = storage.list_task_sources(data["created"], db_path=self.db)[0]
        self.assertEqual((source["product_id"], source["search_query"]), ("111", "Fitbit Charge 6"))
        again = self.client.post("/api/find/confirm", {"token": token, "rec_id": "e1", "target_price": "230"})[1]
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(storage.list_tasks(db_path=self.db)), 1)

    def test_drop_percent_on_confirm_and_modify(self):
        _, data = self.create_product_task(drop_percent="10")
        tid = data["created"]
        self.assertEqual(data["task"]["drop_percent"], "10")
        self.assertEqual(self.client.post(f"/api/tasks/{tid}/modify", {"drop_percent": 95})[0], 400)
        cleared = self.client.post(f"/api/tasks/{tid}/modify", {"drop_percent": ""})[1]
        self.assertIsNone(cleared["task"]["drop_percent"])
        self.assertTrue(cleared["episode_ended"])
        token = self.find()["token"]
        self.assertEqual(self.client.post("/api/find/confirm", {"token": token, "rec_id": "e1", "target_price": "1",
                                                                "drop_percent": "0.5"})[0], 400)

    def test_include_shipping_and_merchant_shipping(self):
        _, data = self.create_product_task(include_shipping=True)
        tid, sid = data["created"], data["task"]["sources"][0]["id"]
        self.assertTrue(data["task"]["include_shipping"])
        set_ = self.client.post(f"/api/tasks/{tid}/sources/shipping", {"source_id": sid, "shipping": "6.95"})[1]
        self.assertEqual(set_["task"]["sources"][0]["shipping_cost"], "6.95")
        self.assertEqual(self.client.post(f"/api/tasks/{tid}/sources/shipping", {"source_id": sid, "shipping": "-1"})[0], 400)
        self.assertEqual(self.client.post(f"/api/tasks/{tid}/sources/shipping", {"source_id": 999, "shipping": "1"})[0], 404)
        cleared = self.client.post(f"/api/tasks/{tid}/sources/shipping", {"source_id": sid, "shipping": ""})[1]
        self.assertIsNone(cleared["task"]["sources"][0]["shipping_cost"])
        same = self.client.post(f"/api/tasks/{tid}/modify", {"include_shipping": True})[1]
        self.assertFalse(same["episode_ended"])
        off = self.client.post(f"/api/tasks/{tid}/modify", {"include_shipping": False})[1]
        self.assertTrue(off["episode_ended"])
        self.assertFalse(off["task"]["include_shipping"])

    def test_confirm_enforces_minimum_frequency_for_product_sources(self):
        with patch("config.monitor_min_frequency_minutes", return_value=360):
            _, data = self.create_product_task(frequency_minutes=30)
        self.assertEqual(data["task"]["frequency_minutes"], 360)

    def test_confirm_unmonitorable_listing_creates_url_source(self):
        token = self.find()["token"]
        data = self.client.post("/api/find/confirm", {"token": token, "rec_id": "e2", "target_price": "90"})[1]
        self.assertEqual(data["task"]["sources"][0]["source_kind"], "url")
        self.assertEqual(data["task"]["sources"][0]["url"], "https://small.example.nz/inspire")

    def test_confirm_validation(self):
        token = self.find()["token"]
        for body in (
            {"token": token, "rec_id": "e1", "target_price": "-1"},
            {"token": token, "rec_id": "e1", "target_price": "10", "comparison": "gt"},
            {"token": token, "rec_id": "e1", "target_price": "10", "frequency_minutes": 1},
            {"token": token, "rec_id": "e9", "target_price": "10"},
        ):
            self.assertEqual(self.client.post("/api/find/confirm", body)[0], 400, body)
        self.assertEqual(self.client.post("/api/find/confirm", {"token": "gone", "rec_id": "e1", "target_price": "1"})[0], 404)
        with patch.dict(os.environ, {"DEFAULT_NOTIFY_EMAIL": ""}):
            self.assertEqual(self.client.post("/api/find/confirm", {"token": token, "rec_id": "e1", "target_price": "1"})[0], 400)
        self.assertEqual(storage.list_tasks(db_path=self.db), [])


class TestDirectUrl(WebCase):
    def test_explicit_fields(self):
        status, data = self.client.post("/api/tasks/add", {
            "url": "https://shop.example.nz/kindle", "product_name": "Kindle", "target_price": "250",
            "currency": "nzd", "frequency_minutes": 60,
        })
        self.assertEqual(status, 200, data)
        self.assertEqual(data["task"]["sources"][0]["source_kind"], "url")
        self.assertEqual(data["task"]["frequency_minutes"], 60)

    def test_instruction_goes_through_the_task_parser(self):
        data = self.client.post("/api/tasks/add", {"instruction": "monitor https://shop.example.nz/kindle below 250 NZD"})[1]
        self.assertEqual(data["task"]["product_name"], "Kindle")
        unclear = self.client.post("/api/tasks/add", {"instruction": "unclear request"})[1]
        self.assertEqual((unclear["created"], unclear["status"]), (None, "needs_clarification"))

    def test_private_and_odd_urls_are_refused(self):
        for url in ("http://localhost/x", "http://192.168.1.2/x", "ftp://shop.example.nz/x", "https://user:pw@shop.example.nz/x",
                    "https://shop.example.nz:8443/x"):
            body = {"url": url, "product_name": "X", "target_price": "1", "currency": "NZD"}
            self.assertEqual(self.client.post("/api/tasks/add", body)[0], 400, url)


class TestTaskLifecycle(WebCase):
    def test_check_requires_confirm_and_sends_exactly_one_email(self):
        _, data = self.create_product_task()
        tid = data["created"]
        self.assertEqual(self.client.post(f"/api/tasks/{tid}/check", {})[0], 400)
        first = self.client.post(f"/api/tasks/{tid}/check", {"confirm": True})[1]
        self.assertTrue(first["is_deal"])
        self.assertTrue(first["notified"])
        self.assertEqual(first["chosen_retailer"], "JB Hi-Fi")
        second = self.client.post(f"/api/tasks/{tid}/check", {"confirm": True})[1]
        self.assertTrue(second["is_deal"])
        self.assertFalse(second["notified"])
        self.assertEqual(len(self.fakes.sent), 1)
        self.assertEqual(self.fakes.sent[0][0], EMAIL)

    def test_detail_history_modify_pause_resume_stop(self):
        _, data = self.create_product_task()
        tid = data["created"]
        self.client.post(f"/api/tasks/{tid}/check", {"confirm": True})
        detail = self.client.get(f"/api/tasks/{tid}")[1]
        self.assertEqual(detail["sources"][0]["latest"][0]["retailer"], "JB Hi-Fi")
        self.assertEqual(len(self.client.get(f"/api/tasks/{tid}/history")[1]["history"]), 1)

        modified = self.client.post(f"/api/tasks/{tid}/modify", {"target_price": "200"})[1]
        self.assertTrue(modified["episode_ended"])
        self.assertEqual(self.client.post(f"/api/tasks/{tid}/modify", {})[0], 400)
        with patch("config.monitor_min_frequency_minutes", return_value=360):
            self.assertEqual(self.client.post(f"/api/tasks/{tid}/modify", {"frequency_minutes": 10})[1]["task"]["frequency_minutes"], 360)

        for action, status in (("pause", "paused"), ("resume", "active"), ("stop", "stopped")):
            self.assertEqual(self.client.post(f"/api/tasks/{tid}/{action}", {})[1]["task"]["status"], status)
        self.assertEqual(self.client.post(f"/api/tasks/{tid}/check", {"confirm": True})[1]["reason"], "task_stopped")

    def test_source_toggle_and_ownership(self):
        _, data = self.create_product_task()
        tid, sid = data["created"], data["task"]["sources"][0]["id"]
        off = self.client.post(f"/api/tasks/{tid}/sources/disable", {"source_id": sid})[1]
        self.assertEqual(off["enabled_sources"], 0)
        self.assertIsNotNone(off["warning"])
        self.assertEqual(self.client.post(f"/api/tasks/{tid}/sources/enable", {"source_id": 999})[0], 404)
        other = self.client.post("/api/tasks/add", {"url": "https://shop.example.nz/k", "product_name": "K", "target_price": "1", "currency": "NZD"})[1]
        self.assertEqual(self.client.post(f"/api/tasks/{other['created']}/sources/enable", {"source_id": sid})[0], 400)
        self.assertEqual(self.client.post(f"/api/tasks/{tid}/sources/enable", {})[0], 400)

    def test_due_checks_and_tasks_overview(self):
        self.create_product_task()
        self.assertEqual(self.client.post("/api/check/due", {})[0], 400)
        due = self.client.post("/api/check/due", {"confirm": True})[1]
        self.assertEqual(due["checked"], 1)
        overview = self.client.get("/api/tasks")[1]
        self.assertEqual(len(overview["tasks"]), 1)
        self.assertIn("api_usage", overview)
        self.assertIn(EMAIL, overview["email_warning"])

    def test_monitor_runs_once_per_process(self):
        self.assertEqual(self.client.post("/api/monitor/start", {})[0], 400)
        self.assertEqual(self.client.post("/api/monitor/start", {"confirm": True})[1]["status"], "running")
        self.assertEqual(self.client.post("/api/monitor/start", {"confirm": True})[0], 409)
        self.assertEqual(self.client.post("/api/monitor/stop", {})[1]["status"], "stopped")
        self.assertEqual(self.client.post("/api/monitor/stop", {})[0], 409)
        self.assertEqual(self.client.get("/api/monitor/status")[1]["status"], "stopped")


class TestSetup(WebCase):
    def test_status_reports_missing_keys_only(self):
        data = self.client.get("/api/setup/status")[1]
        self.assertIn("SERPER_API_KEY or SERPAPI_API_KEY", data["missing"])
        self.assertEqual(data["providers"]["shopping"], "serpapi")
        self.assertNotIn(EMAIL, json.dumps(data))

    def test_only_recipient_can_be_saved(self):
        self.assertEqual(self.client.post("/api/setup", {"SMTP_PASS": "x"})[0], 400)
        self.assertEqual(self.client.post("/api/setup", {"DEFAULT_NOTIFY_EMAIL": "nope"})[0], 400)
        status, data = self.client.post("/api/setup", {"DEFAULT_NOTIFY_EMAIL": "new@example.com"})
        self.assertEqual(status, 200)
        self.assertIn("DEFAULT_NOTIFY_EMAIL=new@example.com", self.env_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

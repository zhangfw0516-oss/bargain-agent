"""Monitoring with SerpApi product sources: migration, quota, observation, identity
verdicts and the multi-seller deal decision. Fully offline."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pricing
import scheduler
import storage
from agents import AgentResult, LLMOptions
from sources.base import Offer, PageText, ProductRef, QuotaExceeded, RateLimited
from tests.fakes import FakeLLM

PRODUCT_URL = "https://www.google.co.nz/shopping/product/111"


def offer(merchant, price, *, condition=None, in_stock=True, title="Fitbit Charge 6", currency="NZD"):
    return Offer(merchant=merchant, price=price, currency=currency, link=f"https://{merchant.lower().replace(' ', '')}.example.nz/p",
                 provider="serpapi", title=title, condition=condition, in_stock=in_stock)


class FakeOffers:
    name = "serpapi"

    def __init__(self, *responses, refresh_token=None):
        self.responses = list(responses)
        self.refs = []
        self.refresh_token = refresh_token

    def offers(self, ref):
        self.refs.append(ref)
        value = self.responses.pop(0) if self.responses else []
        if isinstance(value, Exception):
            raise value
        fresh = ProductRef(ref.product_id, self.refresh_token or ref.page_token, ref.search_query, ref.title)
        return value, fresh


class FakeExtractor:
    def __init__(self, name, result):
        self.name, self.result, self.urls = name, result, []

    def extract(self, url):
        self.urls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def providers(offers=None, extractors=()):
    return SimpleNamespace(offers=offers, extractors=list(extractors))


class DbCase(unittest.TestCase):
    def setUp(self):
        scheduler._active_scheduler = None
        scheduler._scheduler_has_run = False
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "t.db"
        storage.init_db(self.db)

    def tearDown(self):
        self.dir.cleanup()

    def product_task(self, target="230.00", frequency=60, condition=None):
        task = {
            "product_name": "Fitbit Charge 6", "url": PRODUCT_URL, "target_price": target, "currency": "NZD",
            "comparison": "lte", "frequency_minutes": frequency, "notify_email": "u@example.com", "condition": condition,
            "identity": {"confirmed_title": "Fitbit Charge 6 Fitness Tracker - Obsidian"},
        }
        tid = storage.create_task_with_sources(task, [{
            "url": PRODUCT_URL, "retailer": "Google Shopping", "source_kind": storage.SOURCE_SERPAPI,
            "product_id": "111", "page_token": "tok-111", "search_query": "fitbit charge 6",
        }], db_path=self.db)
        return storage.get_task(tid, db_path=self.db)

    def observer(self, fake_offers):
        return lambda source, task: pricing.observe_source(source, task, providers=providers(fake_offers), db_path=self.db)

    def check(self, task, observer, *, verifier=None, sent=None):
        sent = sent if sent is not None else []

        def send(to, t, quote):
            sent.append(quote)
            return {"success": True}

        return scheduler.check_task(task, observer=observer, verifier=verifier, send_fn=send, max_retries=3, db_path=self.db), sent


class TestMigrationV1(unittest.TestCase):
    def test_legacy_database_is_backed_up_and_sources_become_url(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "old.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  status TEXT NOT NULL, product_name TEXT NOT NULL, url TEXT NOT NULL, target_price TEXT NOT NULL,
                  currency TEXT NOT NULL, comparison TEXT NOT NULL, frequency_minutes INTEGER NOT NULL, condition TEXT,
                  notify_email TEXT NOT NULL, next_check_at TEXT NOT NULL, last_check_at TEXT, last_quote_id INTEGER, last_event_id INTEGER);
                CREATE TABLE task_sources (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, retailer TEXT,
                  url TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  last_checked_at TEXT, last_error TEXT, source_kind TEXT NOT NULL DEFAULT 'retailer_adapter', UNIQUE(task_id, url));
                INSERT INTO tasks VALUES (1,'t','t','active','Kindle','https://pbtech.co.nz/k','200','NZD','lt',60,NULL,'a@b.c','t',NULL,NULL,NULL);
                INSERT INTO task_sources (task_id, retailer, url, enabled, created_at, updated_at, source_kind)
                  VALUES (1,'pbtech','https://pbtech.co.nz/k',1,'t','t','retailer_adapter'),
                         (1,'web','https://other.nz/k',0,'t','t','web_search');
            """)
            conn.commit()
            conn.close()

            storage.init_db(db)
            self.assertTrue((Path(d) / "old.db.pre-v1.bak").exists())
            rows = storage.list_task_sources(1, db_path=db)
            self.assertEqual([r["source_kind"] for r in rows], ["url", "url"])
            self.assertEqual([r["enabled"] for r in rows], [1, 0])
            self.assertIn("Migrated", rows[0]["last_error"])

            storage.init_db(db)  # idempotent
            self.assertEqual(len(storage.list_task_sources(1, db_path=db)), 2)
            c = sqlite3.connect(db)
            try:
                self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], storage.SCHEMA_VERSION)
            finally:
                c.close()

    def test_fresh_database_needs_no_backup(self):
        with tempfile.TemporaryDirectory() as d:
            storage.init_db(Path(d) / "new.db")
            self.assertFalse((Path(d) / "new.db.pre-v1.bak").exists())


class TestQuota(DbCase):
    def test_daily_cap_is_atomic_and_counted(self):
        self.assertTrue(storage.count_api_call("serpapi", 2, db_path=self.db))
        self.assertTrue(storage.count_api_call("serpapi", 2, db_path=self.db))
        self.assertFalse(storage.count_api_call("serpapi", 2, db_path=self.db))
        self.assertTrue(storage.count_api_call("tavily", None, db_path=self.db))
        self.assertEqual(storage.api_usage_today(db_path=self.db), {"serpapi": 2, "tavily": 1})

    def test_guard_raises_over_the_cap(self):
        guard = pricing.quota_guard(self.db)
        with patch("config.serpapi_daily_max_calls", return_value=1):
            guard("serpapi")
            with self.assertRaises(QuotaExceeded):
                guard("serpapi")
            guard("firecrawl")


class TestObserveSource(DbCase):
    def test_product_source_lists_every_seller_and_refreshes_token(self):
        task = self.product_task()
        source = storage.list_task_sources(task["id"], db_path=self.db)[0]
        fake = FakeOffers([offer("Noel Leeming", "229.00"), offer("Random Shop", "199.00")], refresh_token="tok-new")
        quotes = pricing.observe_source(source, task, providers=providers(fake), db_path=self.db)
        self.assertEqual([(q["retailer"], q["price"], q["trusted"]) for q in quotes],
                         [("Noel Leeming", "229.00", True), ("Random Shop", "199.00", False)])
        self.assertEqual(fake.refs[0].page_token, "tok-111")
        self.assertEqual(storage.list_task_sources(task["id"], db_path=self.db)[0]["page_token"], "tok-new")

    def test_empty_offers_warn_after_three_checks(self):
        task = self.product_task()
        source = storage.list_task_sources(task["id"], db_path=self.db)[0]
        fake = FakeOffers([], [], [])
        messages = []
        for _ in range(3):
            with self.assertRaises(pricing.ObservationError) as ctx:
                pricing.observe_source(source, task, providers=providers(fake), db_path=self.db)
            messages.append(str(ctx.exception))
        self.assertIn("may have been removed", messages[-1])
        self.assertNotIn("removed", messages[0])

    def test_provider_error_becomes_observation_error(self):
        task = self.product_task()
        source = storage.list_task_sources(task["id"], db_path=self.db)[0]
        with self.assertRaisesRegex(pricing.ObservationError, "quota"):
            pricing.observe_source(source, task, providers=providers(FakeOffers(RateLimited("quota"))), db_path=self.db)

    def test_url_source_falls_back_through_extractors(self):
        failing = FakeExtractor("tavily", RateLimited("tavily down"))
        working = FakeExtractor("firecrawl", PageText(url="u", text="Kindle $279 NZD in stock", provider="firecrawl"))
        llm = FakeLLM({"extract_price_from_page.md": {"product_name": "Kindle", "price": "279", "currency": "NZD", "in_stock": True}})
        source = {"id": 1, "url": "https://shop.example.nz/k", "retailer": None, "source_kind": "url"}
        quotes = pricing.observe_source(source, {"product_name": "Kindle"}, providers=providers(extractors=[failing, working]),
                                        llm_opts=LLMOptions(client=llm, model="m"))
        self.assertEqual((quotes[0]["price"], quotes[0]["retailer"], quotes[0]["trusted"]), ("279.00", "shop.example.nz", True))

    def test_url_source_with_all_extractors_failing(self):
        source = {"id": 1, "url": "https://shop.example.nz/k", "source_kind": "url"}
        with self.assertRaisesRegex(pricing.ObservationError, "tavily: blocked"):
            pricing.observe_source(source, {}, providers=providers(extractors=[FakeExtractor("tavily", RateLimited("blocked"))]))
        with self.assertRaisesRegex(pricing.ObservationError, "No page extractor"):
            pricing.observe_source(source, {}, providers=providers())


class TestIdentityVerdicts(DbCase):
    def quotes(self):
        return [
            {"retailer": "Noel Leeming", "product_name": "Fitbit Charge 6", "source_kind": "serpapi_product", "price": "229.00"},
            {"retailer": "Band Shop", "product_name": "Charge 6 strap", "source_kind": "serpapi_product", "price": "19.00"},
            {"retailer": "Reseller", "product_name": "Fitbit Charge 6", "condition": "refurbished", "source_kind": "serpapi_product"},
            {"retailer": "PB Tech", "product_name": "Fitbit", "source_kind": "url"},
            {"error": "boom"},
        ]

    def test_prefilter_and_veto_and_cache(self):
        task = self.product_task()
        calls = []

        def verifier(product, offers):
            calls.append([o["title"] for o in offers])
            verdicts = {o["id"]: ("different" if "strap" in o["title"] else "same", "r") for o in offers}
            return AgentResult(verdicts, "ok")

        quotes = self.quotes()
        scheduler.assign_identity_verdicts(task, quotes, verifier=verifier, db_path=self.db)
        self.assertEqual([q.get("identity_verdict") for q in quotes], ["same", "different", "different", "same", None])
        self.assertEqual(calls, [["Fitbit Charge 6", "Charge 6 strap"]])

        again = self.quotes()
        scheduler.assign_identity_verdicts(task, again, verifier=verifier, db_path=self.db)
        self.assertEqual(len(calls), 1, "cached verdicts must not call the agent again")
        self.assertEqual(again[1]["identity_verdict"], "different")

    def test_failed_verifier_leaves_offers_unverified_and_uncached(self):
        task = self.product_task()
        quotes = self.quotes()[:1]
        scheduler.assign_identity_verdicts(
            task, quotes, verifier=lambda p, o: AgentResult({x["id"]: ("unverified", None) for x in o}, "fallback"), db_path=self.db
        )
        self.assertEqual(quotes[0]["identity_verdict"], "unverified")
        self.assertEqual(storage.get_offer_verdicts(task["id"], [scheduler._offer_hash(quotes[0])], db_path=self.db), {})

    def test_requested_condition_is_kept(self):
        task = self.product_task(condition="refurbished")
        quotes = self.quotes()[2:3]
        scheduler.assign_identity_verdicts(task, quotes, verifier=None, db_path=self.db)
        self.assertEqual(quotes[0]["identity_verdict"], "same")


class TestMultiSellerCheck(DbCase):
    def test_cheapest_trusted_seller_triggers_one_email(self):
        task = self.product_task(target="230.00")
        fake = FakeOffers([offer("Noel Leeming", "229.00"), offer("JB Hi-Fi", "219.00"), offer("Random Shop", "150.00"),
                           offer("PB Tech", "199.00", in_stock=False), offer("Reseller", "120.00", condition="refurbished")])
        result, sent = self.check(task, self.observer(fake))
        self.assertTrue(result["is_deal"])
        self.assertEqual((result["chosen_retailer"], result["chosen_price"]), ("JB Hi-Fi", "219.00"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(storage.list_quotes(task["id"], db_path=self.db)), 5)
        run_ids = {q["check_run_id"] for q in storage.list_quotes(task["id"], db_path=self.db)}
        self.assertEqual(len(run_ids), 1)

    def test_untrusted_seller_alone_never_alerts_unless_allowed(self):
        task = self.product_task(target="200.00")
        result, sent = self.check(task, self.observer(FakeOffers([offer("Noel Leeming", "229.00"), offer("Random Shop", "150.00")])))
        self.assertFalse(result["is_deal"])
        self.assertEqual(sent, [])
        with patch("config.alert_trusted_only", return_value=False):
            storage.update_next_check(task["id"], "2000-01-01T00:00:00+00:00", db_path=self.db)
            result, sent = self.check(task, self.observer(FakeOffers([offer("Random Shop", "150.00")])))
        self.assertEqual((result["is_deal"], len(sent)), (True, 1))

    def test_same_episode_sends_once_then_resets_on_verified_exit(self):
        task = self.product_task(target="230.00")
        fake = FakeOffers([offer("JB Hi-Fi", "219.00")], [offer("JB Hi-Fi", "215.00")], [offer("JB Hi-Fi", "249.00")])
        sent = []
        self.check(task, self.observer(fake), sent=sent)
        self.check(task, self.observer(fake), sent=sent)
        self.assertEqual(len(sent), 1)
        result, _ = self.check(task, self.observer(fake), sent=sent)
        self.assertTrue(result.get("event_reset"))

    def test_unverified_offers_and_errors_block_reset(self):
        task = self.product_task(target="230.00")
        self.check(task, self.observer(FakeOffers([offer("JB Hi-Fi", "219.00")])))
        unverified = lambda p, offers: AgentResult({o["id"]: ("unverified", None) for o in offers}, "fallback")
        result, _ = self.check(task, self.observer(FakeOffers([offer("JB Hi-Fi", "249.00")])), verifier=unverified)
        self.assertEqual(result["event_reset_reason"], "identity_unconfirmed")
        result, _ = self.check(task, self.observer(FakeOffers(RateLimited("quota"))))
        self.assertEqual(result["event_reset_reason"], "not_all_sources_comparable")
        self.assertEqual(len(storage.list_events(task["id"], db_path=self.db)), 1)

    def test_vetoed_cheaper_offer_cannot_trigger(self):
        task = self.product_task(target="230.00")
        veto = lambda p, offers: AgentResult({o["id"]: ("different" if "Bundle" in o["title"] else "same", None) for o in offers}, "ok")
        fake = FakeOffers([offer("JB Hi-Fi", "199.00", title="Fitbit Charge 6 Bundle"), offer("Noel Leeming", "239.00")])
        result, sent = self.check(task, self.observer(fake), verifier=veto)
        self.assertFalse(result["is_deal"])
        self.assertEqual(sent, [])

    def test_product_sources_respect_the_minimum_frequency(self):
        task = self.product_task(frequency=60)
        with patch("config.monitor_min_frequency_minutes", return_value=360):
            self.check(task, self.observer(FakeOffers([offer("JB Hi-Fi", "249.00")])))
        next_check = datetime.fromisoformat(storage.get_task(task["id"], db_path=self.db)["next_check_at"])
        self.assertGreater((next_check - datetime.now(timezone.utc)).total_seconds(), 359 * 60)


class FakeShopping:
    name = "serper"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.queries = []

    def search(self, query, *, limit=20):
        self.queries.append(query)
        value = self.responses.pop(0) if self.responses else []
        if isinstance(value, Exception):
            raise value
        return value


def listing(pid, merchant, price):
    from sources.base import ShoppingResult

    return ShoppingResult(title="Fitbit Charge 6", link=f"https://g.example/{pid}", provider="serper",
                          merchant=merchant, price=price, currency="NZD", product_id=pid)


class TestSharedSearchMonitoring(DbCase):
    def multi_task(self, target="215.00", tokens=(None, None, None)):
        task = {"product_name": "Fitbit Charge 6", "url": PRODUCT_URL, "target_price": target, "currency": "NZD",
                "comparison": "lte", "frequency_minutes": 1440, "notify_email": "u@example.com"}
        sources = [
            {"url": f"https://g.example/{pid}", "retailer": m, "source_kind": storage.SOURCE_SERPAPI, "product_id": pid,
             "page_token": tok, "search_query": "Fitbit Charge 6"}
            for (pid, m), tok in zip((("a", "Harvey Norman"), ("b", "PB Tech"), ("c", "JB Hi-Fi")), tokens)
        ]
        tid = storage.create_task_with_sources(task, sources, db_path=self.db)
        return storage.get_task(tid, db_path=self.db)

    def observer_for(self, prov):
        context = pricing.CheckContext()
        return lambda source, task: pricing.observe_source(source, task, providers=prov, db_path=self.db, context=context)

    def test_one_search_per_check_for_all_merchants(self):
        task = self.multi_task()
        shopping = FakeShopping([listing("a", "Harvey Norman New Zealand", "220.00"), listing("b", "PB Tech", "198.00"),
                                 listing("c", "JB Hi-Fi", "219.00"), listing("z", "Other", "100.00")])
        prov = SimpleNamespace(shopping=shopping, detail=None, offers=None, extractors=[])
        result, sent = self.check(task, self.observer_for(prov))
        self.assertEqual(shopping.queries, ["Fitbit Charge 6"])
        self.assertEqual(result["sources_checked"], 3)
        self.assertEqual((result["chosen_retailer"], result["chosen_price"]), ("PB Tech", "198.00"))
        self.assertEqual(len(sent), 1)

    def test_missing_listing_is_found_with_a_merchant_search(self):
        task = self.multi_task()
        shopping = FakeShopping([listing("a", "Harvey Norman New Zealand", "220.00"), listing("c", "JB Hi-Fi", "219.00")],
                                [listing("b", "PB Tech", "198.00")])
        prov = SimpleNamespace(shopping=shopping, detail=None, offers=None, extractors=[])
        result, _ = self.check(task, self.observer_for(prov))
        self.assertEqual(shopping.queries, ["Fitbit Charge 6", "Fitbit Charge 6 PB Tech"])
        self.assertEqual((result["sources_failed"], result["chosen_retailer"]), (0, "PB Tech"))

    def test_missing_listings_use_at_most_two_detail_lookups(self):
        task = self.multi_task(tokens=("ta", "tb", "tc"))
        shopping = FakeShopping([listing("z", "Other", "100.00")], [], [])
        detail = FakeOffers([offer("Harvey Norman", "220.00")], [offer("PB Tech", "199.00")], [offer("JB Hi-Fi", "1.00")])
        prov = SimpleNamespace(shopping=shopping, detail=detail, offers=None, extractors=[])
        result, _ = self.check(task, self.observer_for(prov))
        self.assertEqual(len(detail.refs), 2)
        self.assertEqual(len(shopping.queries), 3)  # shared search + at most two merchant retries
        self.assertEqual(result["sources_failed"], 1)
        errors = [s["last_error"] for s in storage.list_task_sources(task["id"], db_path=self.db)]
        self.assertIn("did not appear", errors[2])

    def test_search_failure_is_a_source_error_not_a_reset(self):
        task = self.multi_task(target="300.00")
        ok = FakeShopping([listing("a", "Harvey Norman New Zealand", "220.00"), listing("b", "PB Tech", "198.00"),
                           listing("c", "JB Hi-Fi", "219.00")])
        self.check(task, self.observer_for(SimpleNamespace(shopping=ok, detail=None, offers=None, extractors=[])))
        failing = FakeShopping(RateLimited("Serper quota"))
        prov = SimpleNamespace(shopping=failing, detail=None, offers=None, extractors=[])
        result, sent = self.check(task, self.observer_for(prov))
        self.assertEqual(failing.queries, ["Fitbit Charge 6"])
        self.assertEqual(result["sources_failed"], 3)
        self.assertEqual(result["event_reset_reason"], "not_all_sources_comparable")


class TestCheckDue(DbCase):
    def test_only_due_active_tasks_are_checked_and_errors_are_isolated(self):
        due = self.product_task()
        later = self.product_task()
        paused = self.product_task()
        storage.update_next_check(later["id"], "2999-01-01T00:00:00+00:00", db_path=self.db)
        storage.set_task_status_atomic(paused["id"], "paused", db_path=self.db)
        failing = self.product_task()
        seen = []

        def fake_check(task, db_path=None):
            seen.append(task["id"])
            if task["id"] == failing["id"]:
                raise RuntimeError("boom")
            return {"task_id": task["id"], "is_deal": False}

        results = scheduler.check_due_tasks(self.db, check=fake_check)
        self.assertEqual(seen, [due["id"], failing["id"]])
        self.assertEqual(results[1]["error"], "RuntimeError: boom")


class TestDropPercent(DbCase):
    def test_alerts_on_each_new_drop_from_the_reference(self):
        task = self.product_task(target="100.00")
        storage.update_task_conditions(task["id"], drop_percent="10", db_path=self.db)
        prices = ["220.00", "200.00", "198.00", "198.00", "178.00"]
        fake = FakeOffers(*[[offer("JB Hi-Fi", p)] for p in prices])
        sent, reasons = [], []
        for _ in prices:
            fresh = storage.get_task(task["id"], db_path=self.db)
            result, _ = self.check(fresh, self.observer(fake), sent=sent)
            reasons.append(result.get("deal_reason"))
        # 220 sets the reference; 200 is only 9% lower; 198 is 10% -> alert (reference 198);
        # 198 again is no new drop (episode resets); 178 is 10.1% below 198 -> second alert.
        self.assertEqual(reasons, [None, None, "drop", None, "drop"])
        self.assertEqual([q["price"] for q in sent], ["198.00", "178.00"])
        self.assertEqual(storage.get_task(task["id"], db_path=self.db)["reference_price"], "178.00")

    def test_threshold_still_works_and_clearing_resets(self):
        task = self.product_task(target="230.00")
        storage.update_task_conditions(task["id"], drop_percent="10", db_path=self.db)
        result, sent = self.check(storage.get_task(task["id"], db_path=self.db), self.observer(FakeOffers([offer("JB Hi-Fi", "219.00")])))
        self.assertEqual((result["deal_reason"], len(sent)), ("threshold", 1))
        storage.update_task_conditions(task["id"], drop_percent="", db_path=self.db)
        fresh = storage.get_task(task["id"], db_path=self.db)
        self.assertEqual((fresh["drop_percent"], fresh["reference_price"]), (None, None))


if __name__ == "__main__":
    unittest.main()

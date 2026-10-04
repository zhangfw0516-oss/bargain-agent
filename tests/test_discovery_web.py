"""Offline tests for the discovery expansion (BROAD_PRODUCT_DISCOVERY.md).

Web-search candidates are merged into `discovery.search_and_compare` with the
built-in shops: URL dedupe with source attribution, the 6-detail budget
(≤3 web / ≤3 shops, leftovers transferred), ranking by confirmed-same first,
structured failure reasons for web detail pages, and the requirement that no
price/currency/stock ever comes from a search snippet. All network touchpoints
are injected — nothing here calls Brave or any shop.
"""

import unittest
from unittest.mock import patch

import discovery
import search_providers.brave as brave
from retailers.base import Candidate, Retailer, RetailerError


class FakeRetailer(Retailer):
    name = "Fake Shop"
    domains = ("fake.example",)

    def __init__(self, name="Fake Shop", candidates=None, offers=None, error=None):
        self.name = name
        self.domains = ("fake.example",)
        self._candidates = candidates or []
        self._offers = offers or {}
        self._error = error
        self.offer_calls = []

    def search(self, query, *, limit=10):
        if self._error is not None:
            raise self._error
        return list(self._candidates)[:limit]

    def fetch_offer(self, url):
        self.offer_calls.append(url)
        value = self._offers.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RetailerError("no offer configured")
        return value


def cand(title, url, retailer="Fake Shop", **kw):
    return Candidate(retailer=retailer, url=url, title=title, sku=None, **kw)


def quote(url, price="239.00", currency="NZD", in_stock=True, name="Fitbit Air"):
    return {
        "product_name": name,
        "url": url,
        "price": price,
        "currency": currency,
        "in_stock": in_stock,
        "fetched_at": "2026-09-24T00:00:00+00:00",
        "shipping_cost": None,
    }


REQUEST = {"query": "Fitbit Air", "colour": "black", "currency": "NZD", "budget": "250.00"}


def web(url, title):
    return {"url": url, "title": title, "source": "web_search", "snippet": "x", "queried_at": "t"}


def not_configured(query):
    """A web provider that reports "not configured", like a run without a key.

    `search_and_compare` falls back to the *real* Brave client when it is handed
    ``None``, so the "no provider" case is modelled by injecting a provider that
    raises :class:`brave.BraveNotConfigured` instead of leaving the argument unset.
    That keeps the suite offline even when `.env` carries a real key.
    """
    raise brave.BraveNotConfigured(
        "Web search is not configured (missing BRAVE_SEARCH_API_KEY); "
        "only the built-in shops were searched."
    )


def run(shop, web_fn=not_configured, web_offer=None, include_web=True, request=None):
    return discovery.search_and_compare(
        request or REQUEST,
        retailers=[shop] if shop is not None else [],
        # Never let `web_fn=None` reach the real Brave default.
        web_candidates_fn=web_fn if web_fn is not None else not_configured,
        web_offer_fn=web_offer,
        include_web=include_web,
    )


class TestWebSourceStatus(unittest.TestCase):
    def test_no_web_provider_reports_unavailable_and_shops_still_work(self):
        shop = FakeRetailer(candidates=[cand("Fitbit Air (Obsidian)", "https://fake.example/p/1")],
                            offers={"https://fake.example/p/1": quote("https://fake.example/p/1")})
        result = run(shop, web_fn=None)
        statuses = {s["retailer"]: s["status"] for s in result["sources"]}
        self.assertEqual(statuses["Fake Shop"], "ok")
        self.assertEqual(statuses[discovery.WEB_SEARCH_SOURCE], "unavailable")
        # Single-source unavailability must never turn the result into "no products".
        self.assertEqual(result["counts"]["offers"], 1)

    def test_web_error_is_isolated_from_shop_results(self):
        shop = FakeRetailer(candidates=[cand("Fitbit Air (Obsidian)", "https://fake.example/p/1")],
                            offers={"https://fake.example/p/1": quote("https://fake.example/p/1")})

        def boom(q):
            raise brave.BraveRateLimited("quota exhausted")

        result = run(shop, web_fn=boom)
        statuses = {s["retailer"]: s["status"] for s in result["sources"]}
        self.assertEqual(statuses[discovery.WEB_SEARCH_SOURCE], "error")
        self.assertEqual(result["counts"]["offers"], 1)
        self.assertTrue(any("could not be searched" in n for n in result["notes"]))

    def test_shop_error_is_isolated_from_web_results(self):
        broken = FakeRetailer(name="Broken Shop", error=RetailerError("403 DataDome"))
        good = FakeRetailer(
            name="Good Shop",
            candidates=[cand("Fitbit Air (Obsidian)", "https://good.example/p/1", retailer="Good Shop")],
            offers={"https://good.example/p/1": quote("https://good.example/p/1")},
        )

        def web_fn(q):
            return [web("https://web.example.co.nz/fitbit-air-obsidian", "Fitbit Air (Obsidian)")]

        def web_offer(url):
            return quote(url)

        result = discovery.search_and_compare(
            REQUEST, retailers=[broken, good], web_candidates_fn=web_fn, web_offer_fn=web_offer
        )
        self.assertEqual(result["counts"]["offers"], 2)  # 1 shop + 1 web
        kinds = {o["retailer"]: o["source_kind"] for o in result["offers"]}
        self.assertEqual(kinds["Good Shop"], discovery.SOURCE_KIND_RETAILER)
        self.assertEqual(kinds[discovery.WEB_SEARCH_SOURCE], discovery.SOURCE_KIND_WEB)


class TestDedupAndAttribution(unittest.TestCase):
    def test_same_url_across_sources_is_deduped_and_keeps_shop_attribution(self):
        shop = FakeRetailer(candidates=[cand("Fitbit Air (Obsidian)", "https://www.fake.example/p/1")],
                            offers={"https://www.fake.example/p/1": quote("https://www.fake.example/p/1")})

        def web_fn(q):
            # Same product page with a tracking query string and no www.
            return [web("https://fake.example/p/1?utm_source=brave", "Fitbit Air (Obsidian)")]

        result = run(shop, web_fn=web_fn)
        self.assertEqual(result["counts"]["candidates"], 1)
        self.assertEqual(result["counts"]["offers"], 1)
        entry = result["candidates"][0]
        self.assertEqual(entry["source_kind"], discovery.SOURCE_KIND_RETAILER)
        self.assertEqual(entry["also_found_by"], [discovery.WEB_SEARCH_SOURCE])
        offer = result["offers"][0]
        self.assertEqual(offer["retailer"], "Fake Shop")

    def test_tracking_and_www_variants_dedupe_to_one(self):
        def web_fn(q):
            return [
                web("https://shop.example.co.nz/product/air-obsidian", "Fitbit Air (Obsidian)"),
                web("https://www.shop.example.co.nz/product/air-obsidian?gclid=1", "Fitbit Air (Obsidian)"),
                web("https://www.shop.example.co.nz/product/air-obsidian/", "Fitbit Air (Obsidian)"),
            ]

        def web_offer(url):
            return quote(url)

        result = run(None, web_fn=web_fn, web_offer=web_offer)
        self.assertEqual(result["counts"]["candidates"], 1)
        self.assertEqual(result["counts"]["offers"], 1)


class TestDetailBudget(unittest.TestCase):
    def _shop_with_n(self, n, base_url="https://fake.example/p", offer_price="239.00"):
        candidates = [cand(f"Fitbit Air (Obsidian) #{i}", f"{base_url}/{i}")
                      for i in range(1, n + 1)]
        offers = {c.url: quote(c.url, price=offer_price) for c in candidates}
        return FakeRetailer(candidates=candidates, offers=offers)

    def test_six_detail_cap_with_three_per_source(self):
        shop = self._shop_with_n(5)
        web_urls = [f"https://web.example.co.nz/p/{i}" for i in range(1, 5)]

        def web_fn(q):
            return [web(u, "Fitbit Air (Obsidian)") for u in web_urls]

        def web_offer(url):
            return quote(url)

        result = run(shop, web_fn=web_fn, web_offer=web_offer)
        # 5 shop + 4 web same-candidates, but only 3+3 = 6 detail fetches.
        self.assertEqual(len(shop.offer_calls), 3)
        web_fetched = [u for u in web_urls if any(o["url"] == u for o in result["offers"])]
        self.assertEqual(len(web_fetched), 3)
        self.assertLessEqual(len(shop.offer_calls) + len(web_fetched), discovery.DETAIL_CAP)
        self.assertTrue(any("were priced" in n for n in result["notes"]))

    def test_unused_shop_quota_flows_to_web(self):
        shop = self._shop_with_n(1)
        web_urls = [f"https://web.example.co.nz/p/{i}" for i in range(1, 6)]

        def web_fn(q):
            return [web(u, "Fitbit Air (Obsidian)") for u in web_urls]

        def web_offer(url):
            return quote(url)

        result = run(shop, web_fn=web_fn, web_offer=web_offer)
        # 1 shop candidate -> 5 web candidates get fetched (2 quota + 3 transferred).
        self.assertEqual(len(shop.offer_calls), 1)
        priced = {o["url"] for o in result["offers"]}
        self.assertEqual(len(priced), 6)
        self.assertEqual(sum(1 for u in web_urls if u in priced), 5)

    def test_web_only_run_uses_up_to_six_details(self):
        web_urls = [f"https://web.example.co.nz/p/{i}" for i in range(1, 8)]

        def web_fn(q):
            return [web(u, "Fitbit Air (Obsidian)") for u in web_urls]

        def web_offer(url):
            return quote(url)

        result = run(None, web_fn=web_fn, web_offer=web_offer)
        self.assertEqual(len(result["offers"]), 6)


class TestRankingAndSnippets(unittest.TestCase):
    def test_confirmed_same_ranks_above_unconfirmed_at_equal_price(self):
        shop = FakeRetailer(
            candidates=[
                cand("Fitbit Air (Berry)", "https://fake.example/p/berry"),   # needs_confirmation
                cand("Fitbit Air (Obsidian)", "https://fake.example/p/obs"),  # same
            ],
            offers={
                "https://fake.example/p/berry": quote("https://fake.example/p/berry", name="Fitbit Air (Berry)"),
                "https://fake.example/p/obs": quote("https://fake.example/p/obs", name="Fitbit Air (Obsidian)"),
            },
        )
        result = run(shop, web_fn=lambda q: [])
        offers = result["offers"]
        self.assertEqual(offers[0]["url"], "https://fake.example/p/obs")
        self.assertEqual(offers[0]["match"], discovery.MATCH_SAME)

    def test_web_detail_never_inherits_price_from_snippet(self):
        # The snippet screams a price; the detail page has none. The offer must
        # come out price-unknown, never filled from the snippet.
        def web_fn(q):
            return [web("https://web.example.co.nz/fitbit-air-obsidian", "Fitbit Air (Obsidian) — only NZD 99 today!")]

        def web_offer(url):
            raise RetailerError("Selected JSON-LD product is missing price, currency")

        result = run(None, web_fn=web_fn, web_offer=web_offer)
        self.assertEqual(result["offers"], [])
        self.assertEqual(len(result["unavailable"]), 1)
        failure = result["unavailable"][0]
        self.assertEqual(failure["source_kind"], discovery.SOURCE_KIND_WEB)
        self.assertIn("missing price", failure["reason"])
        self.assertFalse(any(o.get("price") == "99" for o in result["offers"]))
        self.assertTrue(all("99" not in (o.get("price") or "") for o in result["offers"]))

    def test_shop_detail_failure_is_reported_without_stopping_web(self):
        shop = FakeRetailer(
            candidates=[cand("Fitbit Air (Obsidian)", "https://fake.example/p/1")],
            offers={"https://fake.example/p/1": RetailerError("page refused the request")},
        )

        def web_fn(q):
            return [web("https://web.example.co.nz/fitbit-air", "Fitbit Air (Obsidian)")]

        result = run(shop, web_fn=web_fn, web_offer=lambda url: quote(url))
        self.assertEqual(len(result["offers"]), 1)
        self.assertEqual(result["offers"][0]["retailer"], discovery.WEB_SEARCH_SOURCE)
        self.assertEqual(len(result["unavailable"]), 1)


class TestWebCandidateIdentity(unittest.TestCase):
    """Cosmetics and electronics counter-examples through the web path."""

    def _web_run(self, web_results, request, offer=quote):
        def web_fn(q):
            return [web(u, t) for u, t in web_results]

        return run(None, web_fn=web_fn, web_offer=offer, request=request)

    def test_cosmetic_capacity_conflict_30ml_vs_60ml(self):
        request = {"query": "Lumière Serum", "capacity": "30ml", "currency": "NZD", "budget": "80.00"}
        result = self._web_run(
            [("https://beauty.example.co.nz/serum-30ml", "Lumière Serum 30 ml"),
             ("https://beauty.example.co.nz/serum-60ml", "Lumière Serum 60 ml")],
            request,
        )
        by_url = {c["url"]: c for c in result["candidates"]}
        self.assertEqual(by_url["https://beauty.example.co.nz/serum-30ml"]["match"], discovery.MATCH_SAME)
        self.assertEqual(by_url["https://beauty.example.co.nz/serum-60ml"]["match"], discovery.MATCH_DIFFERENT)

    def test_cosmetic_shade_conflict_is_rejected(self):
        request = {"query": "Velvet Lipstick Ruby", "colour": "red", "currency": "NZD", "budget": "40.00"}
        result = self._web_run(
            [("https://beauty.example.co.nz/ruby", "Velvet Lipstick Ruby (classic red)"),
             ("https://beauty.example.co.nz/rose", "Velvet Lipstick Rosewood")],
            request,
        )
        by_url = {c["url"]: c for c in result["candidates"]}
        self.assertEqual(by_url["https://beauty.example.co.nz/ruby"]["match"], discovery.MATCH_SAME)
        self.assertEqual(by_url["https://beauty.example.co.nz/rose"]["match"], discovery.MATCH_DIFFERENT)

    def test_electronics_accessory_and_wrong_model_rejected_on_web_results(self):
        request = {"query": "Fitbit Air", "colour": None, "currency": "NZD", "budget": "250.00"}
        result = self._web_run(
            [("https://shop.example.co.nz/fitbit-air", "Fitbit Air (Obsidian)"),
             ("https://shop.example.co.nz/fitbit-air-band", "Fitbit Air Active Band"),
             ("https://shop.example.co.nz/fitbit-charge-6", "Fitbit Charge 6 Fitness Tracker")],
            request,
        )
        matches = {c["url"]: c["match"] for c in result["candidates"]}
        self.assertEqual(matches["https://shop.example.co.nz/fitbit-air"], discovery.MATCH_SAME)
        self.assertEqual(matches["https://shop.example.co.nz/fitbit-air-band"], discovery.MATCH_DIFFERENT)
        self.assertEqual(matches["https://shop.example.co.nz/fitbit-charge-6"], discovery.MATCH_DIFFERENT)

    def test_only_confirmed_same_offers_are_selectable(self):
        """Selectability follows the *detail page*, not the card that led to it."""
        request = {"query": "Fitbit Air", "colour": "black", "currency": "NZD", "budget": "250.00"}

        def page(url):
            # Only the first page states the colour; the second is the bare model.
            name = "Fitbit Air (Obsidian)" if url.endswith("obsidian") else "Fitbit Air"
            return quote(url, name=name)

        result = self._web_run(
            [("https://web.example.co.nz/fitbit-air-obsidian", "Fitbit Air (Obsidian)"),
             ("https://web.example.co.nz/fitbit-air", "Fitbit Air")],
            request,
            offer=page,
        )
        selectable_urls = {
            o["url"] for o in result["offers"]
            if o["match"] == discovery.MATCH_SAME and not o["excluded_reason"]
        }
        self.assertEqual(selectable_urls, {"https://web.example.co.nz/fitbit-air-obsidian"})

    def test_a_card_title_alone_never_makes_an_offer_selectable(self):
        """A card that says "Obsidian" proves nothing about the page it links to.

        Both candidates' cards look right, but neither detail page states the colour,
        so neither offer can be confirmed and neither is monitorable.
        """
        request = {"query": "Fitbit Air", "colour": "black", "currency": "NZD", "budget": "250.00"}
        result = self._web_run(
            [("https://web.example.co.nz/fitbit-air-obsidian", "Fitbit Air (Obsidian)"),
             ("https://web.example.co.nz/fitbit-air", "Fitbit Air")],
            request,
        )  # the default `quote` detail name is the bare "Fitbit Air"
        self.assertEqual(
            [o["match"] for o in result["offers"]],
            [discovery.MATCH_NEEDS_CONFIRMATION] * 2,
        )
        self.assertEqual(
            [o["url"] for o in result["offers"] if o["match"] == discovery.MATCH_SAME],
            [],
        )
        for offer in result["offers"]:
            with self.assertRaises(ValueError):
                discovery.select_offer(result, result["offers"].index(offer) + 1)


class TestMonitorNeverCallsBrave(unittest.TestCase):
    def test_check_task_does_not_touch_the_search_provider(self):
        """The monitoring loop fetches confirmed URLs only — Brave is never called."""
        import json
        import tempfile
        from pathlib import Path

        import scheduler
        import storage

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            storage.init_db(db)
            task_id = storage.create_task_with_sources(
                {
                    "product_name": "Fitbit Air",
                    "url": "https://web.example.co.nz/fitbit-air",
                    "target_price": "250.00",
                    "currency": "NZD",
                    "comparison": "lte",
                    "frequency_minutes": 60,
                    "notify_email": "user@example.com",
                    "status": "active",
                },
                [{"url": "https://web.example.co.nz/fitbit-air",
                  "retailer": discovery.WEB_SEARCH_SOURCE,
                  "source_kind": discovery.SOURCE_KIND_WEB}],
                db_path=db,
            )

            calls = {"brave": 0, "fetch_quote": 0, "on_host": 0}
            original_search = brave.search

            def counting_search(*a, **kw):
                calls["brave"] += 1
                return original_search(*a, **kw)

            def fake_on_host(url):
                calls["on_host"] += 1
                return quote(url)

            def forbidden_fetch_quote(url):
                calls["fetch_quote"] += 1
                raise AssertionError("retailer fetch path used for a web_search source")

            with patch.object(brave, "search", counting_search), \
                 patch.object(scheduler.scraper, "fetch_quote_on_host_llm", fake_on_host), \
                 patch.object(scheduler.scraper, "fetch_quote", forbidden_fetch_quote):
                task = storage.get_task(task_id, db_path=db)
                result = scheduler.check_task(task, db_path=db, send_fn=lambda *a, **kw: {"success": True})

            self.assertEqual(calls["brave"], 0)
            self.assertEqual(calls["on_host"], 1)
            self.assertEqual(calls["fetch_quote"], 0)
            self.assertFalse(result.get("skipped"))


if __name__ == "__main__":
    unittest.main()

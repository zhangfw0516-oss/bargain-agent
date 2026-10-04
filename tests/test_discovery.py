"""Offline tests for Stage B discovery: identity matching and comparison.

No network access: retailer search/detail calls are injected.
"""

import unittest

import discovery
from retailers.base import Candidate, Retailer, RetailerError


class FakeRetailer(Retailer):
    name = "Fake Shop"
    domains = ("fake.example",)

    def __init__(self, name="Fake Shop", candidates=None, offers=None, error=None, domains=("fake.example",)):
        self.name = name
        self.domains = domains
        self._candidates = candidates or []
        self._offers = offers or {}
        self._error = error
        self.search_calls = 0
        self.offer_calls = []

    def search(self, query, *, limit=10):
        self.search_calls += 1
        if self._error is not None:
            raise self._error
        return list(self._candidates)[:limit]

    def fetch_offer(self, url):
        self.offer_calls.append(url)
        if url in self._offers:
            value = self._offers[url]
            if isinstance(value, Exception):
                raise value
            return value
        raise RetailerError("no offer configured")


def candidate(title, url="https://fake.example/p/1", sku="SKU1", retailer="Fake Shop", **kw):
    return Candidate(retailer=retailer, url=url, title=title, sku=sku, **kw)


def quote(url, price="239.00", currency="NZD", in_stock=True, name="Google Fitbit Air Fitness Tracker - Obsidian"):
    return {
        "product_name": name,
        "url": url,
        "price": price,
        "currency": currency,
        "in_stock": in_stock,
        "fetched_at": "2026-09-23T00:00:00+00:00",
        "shipping_cost": None,
    }


def no_web(query):
    """An explicit empty web provider.

    `search_and_compare` uses the *real* Brave client when no provider is injected, so
    every call here injects this instead: these tests are about the built-in-shop
    flow and must stay offline even when `.env` carries a real ``BRAVE_SEARCH_API_KEY``.
    """
    return []


class TestMatchCandidate(unittest.TestCase):
    def setUp(self):
        self.request = {"query": "Fitbit Air", "colour": None, "capacity": None}

    def test_model_match(self):
        status, reasons = discovery.match_candidate(self.request, candidate("Google Fitbit Air Fitness Tracker"))
        self.assertEqual(status, discovery.MATCH_SAME)
        self.assertTrue(any("Model matched" in r for r in reasons))

    def test_fitbit_air_2_is_never_fitbit_air(self):
        # Generation stated by the user but missing on the listing: needs
        # confirmation, never same.
        request = {**self.request, "query": "Fitbit Air 2"}
        status, reasons = discovery.match_candidate(request, candidate("Google Fitbit Air (Obsidian)"))
        self.assertEqual(status, discovery.MATCH_NEEDS_CONFIRMATION)
        self.assertTrue(any("Generation" in r for r in reasons))

    def test_matching_generation_is_same(self):
        request = {**self.request, "query": "Fitbit Air 2"}
        status, _ = discovery.match_candidate(request, candidate("Google Fitbit Air 2 (Obsidian)"))
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_conflicting_generation_is_rejected(self):
        request = {**self.request, "query": "Fitbit Air 2"}
        status, reasons = discovery.match_candidate(request, candidate("Google Fitbit Air 3 (Obsidian)"))
        self.assertEqual(status, discovery.MATCH_DIFFERENT)
        self.assertTrue(any("generation" in r.lower() for r in reasons))

    def test_listing_generation_the_request_did_not_name_is_not_same(self):
        # A listing-stated generation the user did not constrain is accepted as
        # identifying evidence (Fitbit Air 2026 is the only Fitbit Air), not held
        # forever in needs_confirmation. A user-named generation still conflicts.
        status, reasons = discovery.match_candidate(self.request, candidate("Google Fitbit Air 2 (Obsidian)"))
        self.assertEqual(status, discovery.MATCH_SAME)
        self.assertTrue(any("generation" in r.lower() for r in reasons))

    def test_generation_guard_does_not_break_charge_6(self):
        request = {**self.request, "query": "Fitbit Charge 6"}
        status, _ = discovery.match_candidate(request, candidate("Fitbit Charge 6 Fitness Tracker - Black / Black"))
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_capacity_digits_are_not_generations(self):
        request = {**self.request, "capacity": "64GB"}
        status, _ = discovery.match_candidate(request, candidate("Fitbit Air 64 GB"))
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_real_dual_retailer_titles_still_match(self):
        status, _ = discovery.match_candidate(
            self.request, candidate("Google Fitbit Air Fitness Tracker - Obsidian - Up to 7 Day Battery Life")
        )
        self.assertEqual(status, discovery.MATCH_SAME)
        status, _ = discovery.match_candidate(
            {**self.request, "colour": "black"}, candidate("Google Fitbit Air (Obsidian)")
        )
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_different_model_is_rejected(self):
        status, reasons = discovery.match_candidate(self.request, candidate("Fitbit Charge 6 Fitness Tracker"))
        self.assertEqual(status, discovery.MATCH_DIFFERENT)
        self.assertTrue(any("does not mention" in r for r in reasons))

    def test_same_model_different_colour_is_rejected(self):
        request = {**self.request, "colour": "black"}
        status, reasons = discovery.match_candidate(request, candidate("Fitbit Air - Blue"))
        self.assertEqual(status, discovery.MATCH_DIFFERENT)
        self.assertTrue(any("Different colour" in r for r in reasons))

    def test_marketing_colour_name_matches(self):
        # PB Tech lists the black Fitbit Air as "Obsidian".
        request = {**self.request, "colour": "black"}
        status, _ = discovery.match_candidate(request, candidate("Google Fitbit Air - Obsidian"))
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_chinese_colour_matches(self):
        request = {**self.request, "colour": "黑色"}
        status, _ = discovery.match_candidate(request, candidate("Google Fitbit Air - Obsidian"))
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_unstated_colour_needs_confirmation(self):
        request = {**self.request, "colour": "black"}
        status, reasons = discovery.match_candidate(request, candidate("Fitbit Air Fitness Tracker"))
        self.assertEqual(status, discovery.MATCH_NEEDS_CONFIRMATION)
        self.assertTrue(any("not stated" in r for r in reasons))

    def test_same_model_different_capacity_is_rejected(self):
        request = {**self.request, "capacity": "64GB"}
        status, reasons = discovery.match_candidate(request, candidate("Fitbit Air 128GB"))
        self.assertEqual(status, discovery.MATCH_DIFFERENT)
        self.assertTrue(any("Different capacity" in r for r in reasons))

    def test_matching_capacity_passes(self):
        request = {**self.request, "capacity": "64GB"}
        status, _ = discovery.match_candidate(request, candidate("Fitbit Air 64 GB"))
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_unstated_capacity_needs_confirmation(self):
        request = {**self.request, "capacity": "64GB"}
        status, reasons = discovery.match_candidate(request, candidate("Fitbit Air"))
        self.assertEqual(status, discovery.MATCH_NEEDS_CONFIRMATION)
        self.assertTrue(any("Capacity" in r for r in reasons))

    def test_nameless_request_is_not_guessed(self):
        status, _ = discovery.match_candidate({"query": ""}, candidate("Anything"))
        self.assertEqual(status, discovery.MATCH_NEEDS_CONFIRMATION)

    def test_accepts_plain_dict_candidates(self):
        entry = candidate("Fitbit Air Tracker").to_dict()
        status, _ = discovery.match_candidate(self.request, entry)
        self.assertEqual(status, discovery.MATCH_SAME)

    # ---------------------------------------------------------------- B2: condition
    # A stated condition requirement must never be ignored. "same" is only allowed when
    # the listing confirms the requested condition.

    def test_used_request_new_candidate_is_not_same(self):
        request = {**self.request, "condition": "used"}
        status, reasons = discovery.match_candidate(request, candidate("Fitbit Air Tracker", condition="new"))
        self.assertEqual(status, discovery.MATCH_DIFFERENT)
        self.assertTrue(any("Different condition" in r for r in reasons))

    def test_new_request_used_candidate_is_not_same(self):
        request = {**self.request, "condition": "new"}
        status, reasons = discovery.match_candidate(request, candidate("Fitbit Air Tracker", condition="used"))
        self.assertEqual(status, discovery.MATCH_DIFFERENT)
        self.assertTrue(any("Different condition" in r for r in reasons))

    def test_unstated_condition_is_not_confirmed(self):
        request = {**self.request, "condition": "used"}
        status, reasons = discovery.match_candidate(request, candidate("Fitbit Air Tracker"))
        self.assertEqual(status, discovery.MATCH_NEEDS_CONFIRMATION)
        self.assertTrue(any("not stated" in r for r in reasons))

    def test_matching_condition_is_same(self):
        request = {**self.request, "condition": "new"}
        status, reasons = discovery.match_candidate(request, candidate("Fitbit Air Tracker", condition="new"))
        self.assertEqual(status, discovery.MATCH_SAME)
        self.assertTrue(any("Condition matched" in r for r in reasons))

    def test_condition_synonyms_are_understood(self):
        request = {**self.request, "condition": "second hand"}
        status, _ = discovery.match_candidate(request, candidate("Fitbit Air Tracker", condition="used"))
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_any_condition_behaves_like_no_condition(self):
        for value in ("any", None, ""):
            request = {**self.request, "condition": value}
            status, _ = discovery.match_candidate(request, candidate("Fitbit Air Tracker"))
            self.assertEqual(status, discovery.MATCH_SAME, msg=f"condition={value!r}")

    # ------------------------------------------------- accessories are not the product
    # Found by the live dual-retailer run: JB Hi-Fi's "Google Fitbit Air Active Band"
    # (NZD 69.99) outranked the real Fitbit Air (NZD 239.00) and read as its price.

    def test_accessory_for_the_product_is_rejected(self):
        for title in (
            "Google Fitbit Air Active Band [L] (Obsidian)",
            "google fitbit air active band l obsidian",
            "Google Fitbit Air Charging Cable Snow",
            "Fitbit Air Screen Protector",
        ):
            status, reasons = discovery.match_candidate(self.request, candidate(title))
            self.assertEqual(status, discovery.MATCH_DIFFERENT, msg=title)
            self.assertTrue(any("accessory" in r for r in reasons), msg=title)

    def test_asking_for_the_accessory_finds_it(self):
        request = {**self.request, "query": "Fitbit Air band"}
        status, reasons = discovery.match_candidate(request, candidate("Google Fitbit Air Active Band [L]"))
        self.assertEqual(status, discovery.MATCH_SAME)
        self.assertFalse(any("accessory" in r for r in reasons))

    def test_product_name_containing_charge_is_not_an_accessory(self):
        # "Fitbit Charge 6" is a real product; "charge" must not be an accessory word.
        request = {**self.request, "query": "Fitbit Charge 6"}
        status, _ = discovery.match_candidate(request, candidate("Fitbit Charge 6 Fitness Tracker - Black"))
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_the_tracker_itself_still_matches(self):
        status, _ = discovery.match_candidate(self.request, candidate("Google Fitbit Air Fitness Tracker - Obsidian"))
        self.assertEqual(status, discovery.MATCH_SAME)

    def test_dict_candidate_condition_is_respected(self):
        request = {**self.request, "condition": "used"}
        entry = candidate("Fitbit Air Tracker", condition="new").to_dict()
        status, _ = discovery.match_candidate(request, entry)
        self.assertEqual(status, discovery.MATCH_DIFFERENT)

    def test_unrecognised_condition_is_not_silently_dropped(self):
        # An uninterpretable requirement must not be treated as "no constraint".
        request = {**self.request, "condition": "vintage"}
        status, reasons = discovery.match_candidate(request, candidate("Fitbit Air Tracker"))
        self.assertEqual(status, discovery.MATCH_NEEDS_CONFIRMATION)
        self.assertTrue(any("not understood" in r for r in reasons))


class TestCompareOffers(unittest.TestCase):
    def _rows(self, offers, request):
        rows, notes = discovery.compare_offers(offers, request)
        return rows, notes

    def test_within_and_above_budget_are_labelled(self):
        request = {"budget": "200.00", "currency": "NZD"}
        offers = [
            {"retailer": "A", "title": "t", "url": "u1", "price": "199.00", "currency": "NZD", "in_stock": True},
            {"retailer": "B", "title": "t", "url": "u2", "price": "239.00", "currency": "NZD", "in_stock": True},
        ]
        rows, _ = self._rows(offers, request)
        self.assertEqual([r["within_budget"] for r in rows], [True, False])
        self.assertEqual(rows[0]["retailer"], "A")

    def test_currency_mismatch_is_excluded_not_compared(self):
        request = {"budget": "200.00", "currency": "NZD"}
        offers = [{"retailer": "A", "title": "t", "url": "u", "price": "100.00", "currency": "USD", "in_stock": True}]
        rows, notes = self._rows(offers, request)
        self.assertEqual(rows[0]["excluded_reason"], "currency_mismatch")
        self.assertIsNone(rows[0]["within_budget"])
        self.assertTrue(any("excluded" in n.lower() for n in notes))

    def test_missing_price_is_excluded(self):
        rows, _ = self._rows(
            [{"retailer": "A", "title": "t", "url": "u", "price": None, "currency": "NZD", "in_stock": True}],
            {"budget": "200.00", "currency": "NZD"},
        )
        self.assertEqual(rows[0]["excluded_reason"], "price_unknown")

    def test_unknown_shipping_and_condition_are_stated(self):
        rows, _ = self._rows(
            [{"retailer": "A", "title": "t", "url": "u", "price": "10.00", "currency": "NZD", "in_stock": True}],
            {"budget": None, "currency": "NZD"},
        )
        notes = " ".join(rows[0]["notes"])
        self.assertIn("Shipping cost not stated", notes)
        self.assertIn("Condition", notes)
        self.assertFalse(rows[0]["shipping_known"])

    def test_unknown_stock_is_labelled_and_not_buyable(self):
        rows, _ = self._rows(
            [{"retailer": "A", "title": "t", "url": "u", "price": "10.00", "currency": "NZD", "in_stock": None}],
            {"budget": None, "currency": "NZD"},
        )
        self.assertIsNone(rows[0]["in_stock"])
        self.assertFalse(rows[0]["buyable"])
        self.assertTrue(any("Stock not stated" in n for n in rows[0]["notes"]))

    def test_buyable_offers_rank_before_unknown_or_out_of_stock(self):
        offers = [
            {"retailer": "Out", "title": "t", "url": "u1", "price": "5.00", "currency": "NZD", "in_stock": False},
            {"retailer": "Unknown", "title": "t", "url": "u2", "price": "6.00", "currency": "NZD", "in_stock": None},
            {"retailer": "InStock", "title": "t", "url": "u3", "price": "9.00", "currency": "NZD", "in_stock": True},
        ]
        rows, _ = self._rows(offers, {"budget": None, "currency": "NZD"})
        self.assertEqual(rows[0]["retailer"], "InStock")

    def test_cheapest_wins_among_equally_buyable(self):
        offers = [
            {"retailer": "B", "title": "t", "url": "u1", "price": "239.00", "currency": "NZD", "in_stock": True},
            {"retailer": "A", "title": "t", "url": "u2", "price": "219.00", "currency": "NZD", "in_stock": True},
        ]
        rows, _ = self._rows(offers, {"budget": None, "currency": "NZD"})
        self.assertEqual([r["retailer"] for r in rows], ["A", "B"])


class TestSearchAndCompare(unittest.TestCase):
    def setUp(self):
        self.request = {
            "query": "Fitbit Air",
            "colour": "black",
            "capacity": None,
            "budget": "250.00",
            "currency": "NZD",
            "comparison": "lte",
        }

    def test_successful_search_matches_and_prices(self):
        url = "https://fake.example/p/fitbit-air-obsidian"
        shop = FakeRetailer(
            candidates=[candidate("Google Fitbit Air - Obsidian", url=url)],
            offers={url: quote(url, price="239.00")},
        )
        result = discovery.search_and_compare(self.request, retailers=[shop], web_candidates_fn=no_web)

        self.assertEqual(result["counts"]["same"], 1)
        self.assertEqual(result["counts"]["offers"], 1)
        self.assertEqual(result["offers"][0]["price"], "239.00")
        self.assertTrue(result["offers"][0]["within_budget"])
        self.assertEqual(result["sources"][0]["status"], "ok")

    def test_no_results_is_reported_not_invented(self):
        shop = FakeRetailer(candidates=[])
        result = discovery.search_and_compare(self.request, retailers=[shop], web_candidates_fn=no_web)
        self.assertEqual(result["counts"]["candidates"], 0)
        self.assertEqual(result["offers"], [])
        self.assertTrue(any("No offer could be compared" in n for n in result["notes"]))

    def test_partial_source_failure_keeps_the_working_source(self):
        url = "https://fake.example/p/ok"
        good = FakeRetailer(
            name="Good Shop",
            candidates=[candidate("Google Fitbit Air - Obsidian", url=url, retailer="Good Shop")],
            offers={url: quote(url)},
        )
        bad = FakeRetailer(name="Broken Shop", error=RetailerError("connection refused"))
        result = discovery.search_and_compare(self.request, retailers=[good, bad], web_candidates_fn=no_web)

        statuses = {s["retailer"]: s["status"] for s in result["sources"]}
        self.assertEqual(statuses["Good Shop"], "ok")
        self.assertEqual(statuses["Broken Shop"], "error")
        self.assertEqual(result["counts"]["offers"], 1)
        self.assertTrue(any("Broken Shop could not be searched" in n for n in result["notes"]))

    def test_detail_failure_is_recorded_per_candidate(self):
        url = "https://fake.example/p/boom"
        shop = FakeRetailer(
            candidates=[candidate("Google Fitbit Air - Obsidian", url=url)],
            offers={url: RetailerError("page changed")},
        )
        result = discovery.search_and_compare(self.request, retailers=[shop], web_candidates_fn=no_web)
        self.assertEqual(result["counts"]["offers"], 0)
        self.assertTrue(any("could not read the price" in n for n in result["notes"]))

    def test_disabled_retailer_is_reported_not_silently_skipped(self):
        shop = FakeRetailer(candidates=[])
        result = discovery.search_and_compare(self.request, retailers=[shop], web_candidates_fn=no_web)
        # A retailer in the registry but absent from the given list must surface.
        statuses = {s["retailer"]: s["status"] for s in result["sources"]}
        self.assertEqual(statuses.get("Mighty Ape"), "disabled")
        self.assertTrue(any("not enabled in this configuration" in n for n in result["notes"]))

    def test_different_products_are_excluded_from_pricing(self):
        url = "https://fake.example/p/other"
        shop = FakeRetailer(
            candidates=[candidate("Fitbit Charge 6 Fitness Tracker", url=url)],
            offers={url: quote(url)},
        )
        result = discovery.search_and_compare(self.request, retailers=[shop], web_candidates_fn=no_web)
        self.assertEqual(result["counts"]["different"], 1)
        self.assertEqual(result["counts"]["offers"], 0)
        self.assertEqual(shop.offer_calls, [])

    def test_detail_fetches_are_capped(self):
        urls = [f"https://fake.example/p/{i}" for i in range(6)]
        shop = FakeRetailer(
            candidates=[candidate(f"Google Fitbit Air - Obsidian {i}", url=u, sku=f"S{i}") for i, u in enumerate(urls)],
            offers={u: quote(u) for u in urls},
        )
        result = discovery.search_and_compare(self.request, retailers=[shop], max_details=2, web_candidates_fn=no_web)
        self.assertEqual(len(shop.offer_calls), 2)
        self.assertTrue(any("Only the first" in n for n in result["notes"]))

    def test_short_card_title_is_preferred_over_a_bullet_listed_detail_name(self):
        url = "https://fake.example/p/fitbit-air-obsidian"
        long_name = "Google Fitbit Air Fitness Tracker - Obsidian - Up to 7 Day Battery Life - 5ATM"
        shop = FakeRetailer(
            candidates=[candidate("Google Fitbit Air Fitness Tracker - Obsidian", url=url)],
            offers={url: quote(url, name=long_name)},
        )
        result = discovery.search_and_compare(self.request, retailers=[shop], web_candidates_fn=no_web)
        self.assertEqual(result["offers"][0]["title"], "Google Fitbit Air Fitness Tracker - Obsidian")
        # The page's own name is still reported, nothing is hidden.
        self.assertEqual(result["offers"][0]["detail_name"], long_name)

    def test_detail_name_is_kept_when_it_adds_information(self):
        url = "https://fake.example/p/x"
        shop = FakeRetailer(
            candidates=[candidate("Google Fitbit Air", url=url)],
            offers={url: quote(url, name="Google Fitbit Air Fitness Tracker - Obsidian")},
        )
        result = discovery.search_and_compare(self.request, retailers=[shop], web_candidates_fn=no_web)
        self.assertEqual(result["offers"][0]["title"], "Google Fitbit Air Fitness Tracker - Obsidian")

    def test_empty_query_does_no_search(self):
        shop = FakeRetailer(candidates=[])
        result = discovery.search_and_compare({"query": ""}, retailers=[shop], web_candidates_fn=no_web)
        self.assertEqual(shop.search_calls, 0)
        self.assertTrue(any("No product name" in n for n in result["notes"]))


class TestSelectOffer(unittest.TestCase):
    def _result(self):
        return {
            "offers": [
                # Only an explicit match verdict is selectable; a row without one
                # must be refused by select_offer.
                {"retailer": "A", "url": "u1", "price": "239.00", "excluded_reason": None,
                 "match": discovery.MATCH_SAME},
                {"retailer": "B", "url": "u2", "price": None, "excluded_reason": "price_unknown"},
            ]
        }

    def test_selects_by_one_based_index(self):
        self.assertEqual(discovery.select_offer(self._result(), 1)["url"], "u1")

    def test_out_of_range_is_refused(self):
        with self.assertRaises(ValueError):
            discovery.select_offer(self._result(), 0)
        with self.assertRaises(ValueError):
            discovery.select_offer(self._result(), 3)

    def test_excluded_offer_cannot_be_monitored(self):
        with self.assertRaises(ValueError):
            discovery.select_offer(self._result(), 2)

    # ------------------------------------------------ Stage B closeout: match verdict
    # A "needs_confirmation" offer must stay visible but must not be monitorable.

    def _confirmed(self, **over):
        base = {"retailer": "A", "url": "u1", "price": "239.00", "currency": "NZD",
                "excluded_reason": None, "match": discovery.MATCH_SAME,
                "title": "Google Fitbit Air (Obsidian)"}
        base.update(over)
        return base

    def _result_with(self, *offers):
        return {"offers": list(offers)}

    def test_confirmed_offer_is_selectable(self):
        result = self._result_with(self._confirmed())
        self.assertEqual(discovery.select_offer(result, 1)["url"], "u1")

    def test_unconfirmed_offer_cannot_be_monitored(self):
        # The real case: the user asked for black, this listing says "Berry".
        offer = self._confirmed(
            url="u2", title="Google Fitbit Air (Berry)", match=discovery.MATCH_NEEDS_CONFIRMATION
        )
        result = self._result_with(self._confirmed(), offer)
        with self.assertRaises(ValueError) as ctx:
            discovery.select_offer(result, 2)
        self.assertIn("could not be confirmed", str(ctx.exception))

    def test_unconfirmed_offer_is_still_listed(self):
        offer = self._confirmed(url="u2", match=discovery.MATCH_NEEDS_CONFIRMATION)
        result = self._result_with(self._confirmed(), offer)
        self.assertEqual(len(result["offers"]), 2)

    def test_match_status_is_carried_into_comparison_rows(self):
        rows, _ = discovery.compare_offers(
            [{"retailer": "A", "url": "u1", "price": "239.00", "currency": "NZD",
              "in_stock": True, "match": discovery.MATCH_NEEDS_CONFIRMATION}],
            {"budget": "250.00", "currency": "NZD"},
        )
        self.assertEqual(rows[0]["match"], discovery.MATCH_NEEDS_CONFIRMATION)

    def test_rows_without_a_match_field_default_to_same(self):
        rows, _ = discovery.compare_offers(
            [{"retailer": "A", "url": "u1", "price": "239.00", "currency": "NZD", "in_stock": True}],
            {"budget": "250.00", "currency": "NZD"},
        )
        self.assertEqual(rows[0]["match"], discovery.MATCH_SAME)


class TestSelectMultipleOffers(unittest.TestCase):
    """Selecting several confirmed offers for one task (Stage C)."""

    @staticmethod
    def _offer(shop, price="239.00", title="Google Fitbit Air (Obsidian)", **over):
        base = {"retailer": shop, "url": f"https://{shop}.example/air", "price": price,
                "currency": "NZD", "excluded_reason": None, "match": discovery.MATCH_SAME,
                "title": title}
        base.update(over)
        return base

    def _result(self, *offers):
        return {"offers": list(offers)}

    def test_selects_several_confirmed_offers(self):
        result = self._result(self._offer("PB Tech"), self._offer("JB Hi-Fi"))
        chosen = discovery.select_offers(result, [1, 2])
        self.assertEqual([o["retailer"] for o in chosen], ["PB Tech", "JB Hi-Fi"])

    def test_accepts_comma_style_string_indices(self):
        result = self._result(self._offer("PB Tech"), self._offer("JB Hi-Fi"))
        self.assertEqual(len(discovery.select_offers(result, ["1", "2"])), 2)

    def test_unconfirmed_offer_blocks_the_whole_selection(self):
        result = self._result(
            self._offer("PB Tech"),
            self._offer("JB Hi-Fi", title="Google Fitbit Air (Berry)",
                        match=discovery.MATCH_NEEDS_CONFIRMATION),
        )
        with self.assertRaises(ValueError) as ctx:
            discovery.select_offers(result, [1, 2])
        self.assertIn("could not be confirmed", str(ctx.exception))

    def test_different_colours_cannot_share_a_task(self):
        result = self._result(
            self._offer("PB Tech", title="Google Fitbit Air (Obsidian)"),
            self._offer("JB Hi-Fi", title="Google Fitbit Air (Berry)"),
        )
        with self.assertRaises(ValueError) as ctx:
            discovery.select_offers(result, [1, 2])
        self.assertIn("same product variant", str(ctx.exception))

    def test_different_capacities_cannot_share_a_task(self):
        result = self._result(
            self._offer("PB Tech", title="Phone 128GB Black"),
            self._offer("JB Hi-Fi", title="Phone 256GB Black"),
        )
        with self.assertRaises(ValueError) as ctx:
            discovery.select_offers(result, [1, 2])
        self.assertIn("same product variant", str(ctx.exception))

    def test_different_currencies_cannot_share_a_task(self):
        result = self._result(
            self._offer("PB Tech"),
            self._offer("JB Hi-Fi", currency="AUD"),
        )
        with self.assertRaises(ValueError) as ctx:
            discovery.select_offers(result, [1, 2])
        self.assertIn("different currencies", str(ctx.exception))

    def test_the_same_index_twice_is_refused(self):
        result = self._result(self._offer("PB Tech"), self._offer("JB Hi-Fi"))
        with self.assertRaises(ValueError) as ctx:
            discovery.select_offers(result, [1, 1])
        self.assertIn("more than once", str(ctx.exception))

    def test_excluded_offer_blocks_the_selection(self):
        result = self._result(
            self._offer("PB Tech"),
            self._offer("JB Hi-Fi", excluded_reason="price_unknown"),
        )
        with self.assertRaises(ValueError):
            discovery.select_offers(result, [1, 2])

    def test_a_non_numeric_choice_is_refused(self):
        result = self._result(self._offer("PB Tech"))
        with self.assertRaises(ValueError) as ctx:
            discovery.select_offers(result, ["x"])
        self.assertIn("not an offer number", str(ctx.exception))

    def test_same_colour_and_capacity_from_different_shops_is_allowed(self):
        # The real acceptance case: both shops sell the black Fitbit Air.
        result = self._result(
            self._offer("PB Tech", title="Google Fitbit Air Fitness Tracker - Obsidian"),
            self._offer("JB Hi-Fi", title="Google Fitbit Air (Obsidian)"),
        )
        self.assertEqual(len(discovery.select_offers(result, [1, 2])), 2)


if __name__ == "__main__":
    unittest.main()

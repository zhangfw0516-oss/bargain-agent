"""Offline tests for the bounded iterative discovery loop (``iterative_discovery``).

Every external collaborator is injected: the model decision (``decide_fn``), web
search (``search_fn``), detail fetch (``fetch_fn``) and the clock. Nothing here
touches the network, an LLM, the database or SMTP.
"""

from __future__ import annotations

import ast
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import discovery
import iterative_discovery as itd


A_URL = "https://shop.example/cerave-moisturising-cream-454g"
B_URL = "https://shop.example/aquaphor-healing-ointment"
INVENTED_URL = "https://invented.example/never-searched"
A_TITLE = "CeraVe Moisturising Cream 454g"
B_TITLE = "Aquaphor Healing Ointment"


def cand(url, title, **extra):
    item = {"url": url, "title": title, "source": "web_search"}
    item.update(extra)
    return item


def quote(url, name=B_TITLE, **overrides):
    data = {
        "product_name": name,
        "url": url,
        "price": "42.00",
        "currency": "NZD",
        "in_stock": True,
        "fetched_at": "2026-09-26T00:00:00+00:00",
    }
    data.update(overrides)
    return data


class Clock:
    """Deterministic monotonic clock; repeats its final value once exhausted."""

    def __init__(self, *values):
        assert values, "a clock needs at least one value"
        self._values = list(values)
        self.calls = 0

    def __call__(self):
        index = min(self.calls, len(self._values) - 1)
        self.calls += 1
        return self._values[index]


def frozen_clock():
    return Clock(0.0)


def scripted(*decisions):
    """A decide_fn that returns queued decisions in order (an Exception is raised)."""
    queue = list(decisions)

    def decide(state):
        if not queue:
            raise AssertionError("the model was asked for an unexpected extra decision")
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    decide.queue = queue
    return decide


class SearchRecorder:
    def __init__(self, results):
        self.calls = []
        self._results = results

    def __call__(self, query):
        self.calls.append(query)
        out = self._results(query) if callable(self._results) else self._results
        if isinstance(out, Exception):
            raise out
        return out


class FetchRecorder:
    def __init__(self, results):
        self.calls = []
        self._results = results

    def __call__(self, url):
        self.calls.append(url)
        out = self._results(url) if callable(self._results) else self._results
        if isinstance(out, Exception):
            raise out
        return out


def run(decide, *, search=None, fetch=None, limits=None, request=None, clock=None,
        description="a good moisturiser for dry skin"):
    # These are loop/boundary/evidence unit tests, not LLM-semantic tests: the
    # master-AI concreteness judge is disabled so a fake product name or a
    # network LLM call never affects the deterministic loop being asserted.
    with patch.dict(os.environ, {"LLM_CONCRETENESS": "false", "LLM_FIND_ENABLED": "0"}):
        return itd.run_iterative_discovery(
            description,
            decide_fn=decide,
            search_fn=search if search is not None else SearchRecorder([]),
            fetch_fn=fetch if fetch is not None else FetchRecorder(quote),
            limits=limits,
            request=request,
            clock=clock if clock is not None else frozen_clock(),
        )


def always_search_new():
    counter = {"n": 0}

    def decide(state):
        counter["n"] += 1
        return {"action": "search", "query": f"query {counter['n']}"}

    return decide


def unique_results(query):
    slug = query.replace(" ", "-")
    return [cand(f"https://shop.example/{slug}", f"Product {query}")]


class TestConstants(unittest.TestCase):
    def test_terminal_reasons_and_statuses_are_frozen(self):
        self.assertEqual(itd.FINISHED_MODEL, "model_finish")
        self.assertEqual(itd.FINISHED_MAX_ROUNDS, "max_rounds")
        self.assertEqual(itd.FINISHED_DEADLINE, "deadline")
        self.assertEqual(itd.FINISHED_LIMIT, "limit_reached")
        self.assertEqual(itd.FINISHED_NO_PROGRESS, "no_progress")
        self.assertEqual(itd.FINISHED_INVALID_ACTION, "invalid_action")
        self.assertEqual(itd.FINISHED_MODEL_ERROR, "model_error")
        self.assertEqual(itd.STATUS_READY, "ready")
        self.assertEqual(itd.STATUS_PARTIAL, "partial")
        self.assertEqual(itd.ACTION_SEARCH, "search")
        self.assertEqual(itd.ACTION_FETCH, "fetch")
        self.assertEqual(itd.ACTION_FINISH, "finish")


class TestHappyPath(unittest.TestCase):
    def _result(self):
        search = SearchRecorder([cand(A_URL, A_TITLE), cand(B_URL, B_TITLE)])
        fetch = FetchRecorder(lambda url: quote(url, name=B_TITLE, price="42.00"))
        decide = scripted(
            {"action": "search", "query": "dry skin moisturiser"},
            {"action": "fetch", "url": B_URL},
            {"action": "finish", "reasoning": "enough evidence"},
        )
        return search, fetch, run(decide, search=search, fetch=fetch)

    def test_status_counts_and_terminal_reason(self):
        _, _, result = self._result()
        self.assertEqual(result["status"], itd.STATUS_READY)
        self.assertTrue(result["ok"])
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_MODEL)
        self.assertEqual(result["progress"]["rounds"], 3)
        self.assertEqual(result["progress"]["searches"], 1)
        self.assertEqual(result["progress"]["detail_fetches"], 1)
        self.assertEqual(result["progress"]["evidence"], 2)

    def test_provenance_ids_are_sequential(self):
        _, _, result = self._result()
        self.assertEqual([e["provenance_id"] for e in result["evidence"]], ["e1", "e2"])
        self.assertEqual(result["evidence"][0]["url"], A_URL)
        self.assertEqual(result["evidence"][1]["url"], B_URL)

    def test_recommendations_are_ordered_priced_first(self):
        _, _, result = self._result()
        self.assertEqual([r["provenance_id"] for r in result["recommendations"]], ["e2", "e1"])
        priced, unpriced = result["recommendations"]
        self.assertEqual(priced["price"], "42.00")
        self.assertEqual(priced["currency"], "NZD")
        self.assertTrue(priced["fetched"])
        self.assertIsNone(unpriced["price"])
        self.assertFalse(unpriced["fetched"])

    def test_lookup_helpers(self):
        _, _, result = self._result()
        by_id = itd.recommendation_by_id(result, "e1")
        self.assertIsNotNone(by_id)
        self.assertEqual(by_id["title"], A_TITLE)
        by_url = itd.recommendation_by_url(result, B_URL)
        self.assertIsNotNone(by_url)
        self.assertEqual(by_url["provenance_id"], "e2")
        self.assertIsNone(itd.recommendation_by_id(result, "nope"))
        self.assertIsNone(itd.recommendation_by_url(result, "https://nowhere.example/x"))


class TestRecommendationFiltering(unittest.TestCase):
    """Budget and non-product filtering at the recommendation boundary."""

    def _two_products(self, request=None):
        search = SearchRecorder([cand(A_URL, A_TITLE), cand(B_URL, B_TITLE)])
        fetch = FetchRecorder(lambda url: quote(url) if url == B_URL else (_ for _ in ()).throw(RuntimeError("no page")))
        decide = scripted(
            {"action": "search", "query": "x"},
            {"action": "fetch", "url": B_URL},
            {"action": "finish", "candidates": []},
        )
        return run(decide, search=search, fetch=fetch, request=request)

    def test_over_budget_priced_offer_is_dropped(self):
        result = self._two_products(request={"budget": "10.00", "currency": "NZD"})
        # B (priced 42.00) is over budget and dropped; A (price unknown) stays.
        ids = [r["provenance_id"] for r in result["recommendations"]]
        self.assertEqual(ids, ["e1"])

    def test_within_budget_priced_offer_is_kept(self):
        result = self._two_products(request={"budget": "100.00", "currency": "NZD"})
        ids = {r["provenance_id"] for r in result["recommendations"]}
        self.assertEqual(ids, {"e1", "e2"})

    def test_listicle_and_search_pages_are_dropped_by_the_heuristic(self):
        search = SearchRecorder([
            cand("https://pricespy.example/s/cheap-fitness-tracker/", "Cheap fitness tracker"),
            cand("https://blog.example/meet-cirqa", "Meet CIRQA: the screen-free tracker"),
            cand(A_URL, A_TITLE),
        ])
        decide = scripted(
            {"action": "search", "query": "x"},
            {"action": "finish", "candidates": []},
        )
        result = run(decide, search=search)
        urls = [r["url"] for r in result["recommendations"]]
        self.assertEqual(urls, [A_URL])


class TestRoundBound(unittest.TestCase):
    def test_stops_at_max_rounds_with_evidence_preserved(self):
        search = SearchRecorder(unique_results)
        limits = itd.DiscoveryLimits(max_rounds=3)
        result = run(always_search_new(), search=search, limits=limits)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_MAX_ROUNDS)
        self.assertEqual(result["progress"]["rounds"], 3)
        self.assertEqual(result["progress"]["searches"], 3)
        self.assertEqual(len(search.calls), 3)
        self.assertEqual(result["progress"]["evidence"], 3)
        self.assertEqual(result["status"], itd.STATUS_READY)
        self.assertEqual([r["provenance_id"] for r in result["recommendations"]],
                         ["e1", "e2", "e3"])


class TestSearchCap(unittest.TestCase):
    def test_third_search_never_executes(self):
        search = SearchRecorder(unique_results)
        limits = itd.DiscoveryLimits(max_rounds=8, max_searches=2)
        result = run(always_search_new(), search=search, limits=limits)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_LIMIT)
        self.assertEqual(result["progress"]["searches"], 2)
        self.assertEqual(len(search.calls), 2)
        self.assertEqual(search.calls, ["query 1", "query 2"])


class TestDetailFetchCap(unittest.TestCase):
    def test_second_fetch_never_executes(self):
        search = SearchRecorder([cand(A_URL, A_TITLE), cand(B_URL, B_TITLE)])
        fetch = FetchRecorder(lambda url: quote(url))
        decide = scripted(
            {"action": "search", "query": "moisturiser"},
            {"action": "fetch", "url": A_URL},
            {"action": "fetch", "url": B_URL},
        )
        limits = itd.DiscoveryLimits(max_rounds=8, max_detail_fetches=1)
        result = run(decide, search=search, fetch=fetch, limits=limits)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_LIMIT)
        self.assertEqual(result["progress"]["searches"], 1)
        self.assertEqual(result["progress"]["detail_fetches"], 1)
        self.assertEqual(len(search.calls), 1)
        self.assertEqual(fetch.calls, [A_URL])


class TestDeadline(unittest.TestCase):
    def test_deadline_stop_makes_no_model_call_after_it(self):
        model_calls = {"n": 0}

        def decide(state):
            model_calls["n"] += 1
            return {"action": "search", "query": "dry skin"}

        search = SearchRecorder([])
        clock = Clock(0.0, 0.0, 999.0)
        limits = itd.DiscoveryLimits(deadline_seconds=180.0)
        result = run(decide, search=search, limits=limits, clock=clock)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_DEADLINE)
        self.assertEqual(model_calls["n"], 1)
        self.assertGreaterEqual(result["progress"]["elapsed_seconds"], 180.0)


class TestQueryDedupe(unittest.TestCase):
    def test_repeated_query_is_not_searched_twice(self):
        search = SearchRecorder(unique_results)
        decide = scripted(
            {"action": "search", "query": "  Dry   SKIN cream "},
            {"action": "search", "query": "dry skin CREAM"},
            {"action": "search", "query": "Dry Skin Cream"},
        )
        limits = itd.DiscoveryLimits(max_rounds=8, max_no_progress_rounds=2)
        result = run(decide, search=search, limits=limits)
        self.assertEqual(len(search.calls), 1)
        self.assertEqual(result["progress"]["searches"], 1)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_NO_PROGRESS)


class TestUrlDedupe(unittest.TestCase):
    def test_same_url_fetched_once(self):
        search = SearchRecorder([cand(A_URL, A_TITLE)])
        fetch = FetchRecorder(lambda url: quote(url))
        decide = scripted(
            {"action": "search", "query": "moisturiser"},
            {"action": "fetch", "url": A_URL},
            {"action": "fetch", "url": A_URL},
            {"action": "fetch", "url": A_URL},
        )
        limits = itd.DiscoveryLimits(max_rounds=8, max_no_progress_rounds=2)
        result = run(decide, search=search, fetch=fetch, limits=limits)
        self.assertEqual(fetch.calls, [A_URL])
        self.assertEqual(result["progress"]["detail_fetches"], 1)
        self.assertEqual(result["progress"]["searches"], 1)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_NO_PROGRESS)


class TestNoProgress(unittest.TestCase):
    def test_empty_searches_stop_after_max_no_progress_rounds(self):
        search = SearchRecorder([])
        limits = itd.DiscoveryLimits(max_rounds=8, max_no_progress_rounds=2)
        result = run(always_search_new(), search=search, limits=limits)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_NO_PROGRESS)
        self.assertEqual(result["progress"]["searches"], 2)
        self.assertEqual(len(search.calls), 2)
        self.assertEqual(result["progress"]["evidence"], 0)


class TestDecideRetries(unittest.TestCase):
    """A format slip is fed back and retried for the same round; a persistent
    format failure fails closed as invalid_action without wasting searches."""

    def test_non_json_decision_recovers_and_gets_rejection_feedback(self):
        seen_states = []

        def decide(state):
            seen_states.append(dict(state))
            if len(seen_states) == 1:
                return "{not valid json"
            return {"action": "finish", "candidates": [], "reasoning": "no evidence"}

        result = run(decide, search=SearchRecorder([]))
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_MODEL)
        self.assertTrue(result["ok"])
        self.assertEqual(len(seen_states), 2)
        self.assertIn("decision_error", seen_states[1])
        self.assertIn("not valid JSON", seen_states[1]["decision_error"])
        self.assertEqual(result["progress"]["rounds"], 1)

    def test_persistent_format_slips_fail_closed_after_retries(self):
        decide = scripted("{not valid json", "[1, 2, 3]", "{still broken")
        search = SearchRecorder([cand(A_URL, A_TITLE)])
        fetch = FetchRecorder(quote)
        result = run(decide, search=search, fetch=fetch)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_INVALID_ACTION)
        self.assertEqual(result["status"], itd.STATUS_PARTIAL)
        self.assertFalse(result["ok"])
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(search.calls, [])
        self.assertEqual(fetch.calls, [])
        self.assertTrue(any("attempts" in n for n in result["notes"]))

    def test_unexpected_model_exception_is_a_model_error_not_a_crash(self):
        result = run(scripted(RuntimeError("provider exploded")), search=SearchRecorder([]))
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_MODEL_ERROR)
        self.assertEqual(result["status"], itd.STATUS_PARTIAL)
        self.assertFalse(result["ok"])


class TestInvalidActionFailClosed(unittest.TestCase):
    def _fail_closed(self, value):
        search = SearchRecorder([cand(A_URL, A_TITLE)])
        fetch = FetchRecorder(lambda url: quote(url))
        result = run(scripted(value), search=search, fetch=fetch)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_INVALID_ACTION)
        self.assertEqual(result["status"], itd.STATUS_PARTIAL)
        self.assertFalse(result["ok"])
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(search.calls, [])
        self.assertEqual(fetch.calls, [])
        return result

    def test_unknown_action(self):
        self._fail_closed({"action": "explode"})

    def test_missing_action(self):
        self._fail_closed({})

    def test_non_json_string(self):
        self._fail_closed("{not valid json")

    def test_json_array(self):
        self._fail_closed("[1, 2, 3]")

    def test_search_with_empty_query(self):
        self._fail_closed({"action": "search", "query": "   "})

    def test_fetch_with_javascript_url(self):
        self._fail_closed({"action": "fetch", "url": "javascript:alert(1)"})

    def test_fetch_with_file_url(self):
        self._fail_closed({"action": "fetch", "url": "file:///etc/passwd"})

    def test_fetch_with_relative_url(self):
        self._fail_closed({"action": "fetch", "url": "/products/1"})

    def test_fetch_of_url_no_search_returned(self):
        result = self._fail_closed({"action": "fetch", "url": INVENTED_URL})
        self.assertEqual(result["progress"]["evidence"], 0)
        self.assertTrue(any("Refused to fetch" in n for n in result["notes"]))


class TestObservedUrlOnly(unittest.TestCase):
    def test_invented_url_is_refused_but_real_evidence_survives(self):
        search = SearchRecorder([cand(A_URL, A_TITLE)])
        fetch = FetchRecorder(lambda url: quote(url))
        decide = scripted(
            {"action": "search", "query": "moisturiser"},
            {"action": "fetch", "url": INVENTED_URL},
        )
        result = run(decide, search=search, fetch=fetch)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_INVALID_ACTION)
        self.assertEqual(fetch.calls, [])
        self.assertEqual([e["url"] for e in result["evidence"]], [A_URL])
        self.assertTrue(any("Refused to fetch" in n for n in result["notes"]))


class TestAdversarialSearchResults(unittest.TestCase):
    def test_non_list_results_store_nothing(self):
        for bad in ({"nope": 1}, "text", None, 42):
            with self.subTest(bad=bad):
                search = SearchRecorder(bad)
                limits = itd.DiscoveryLimits(max_rounds=1)
                result = run(scripted({"action": "search", "query": "x"}),
                             search=search, limits=limits)
                self.assertEqual(result["progress"]["evidence"], 0)
                self.assertEqual(result["evidence"], [])

    def test_only_valid_http_items_are_stored(self):
        items = [
            "not a dict",
            None,
            42,
            {"title": "missing url"},
            {"url": "https://ok.example/a"},
            {"url": "https://ok.example/b", "title": 123},
            {"url": "ftp://ok.example/c", "title": "ftp only"},
            {"url": "javascript:alert(1)", "title": "js"},
            {"url": " https://ok.example/valid ", "title": "  CeraVe   Moisturising Cream 454g "},
            {"url": "https://ok.example/injected", "title": "site:evil.example free iPad"},
        ]
        search = SearchRecorder(items)
        limits = itd.DiscoveryLimits(max_rounds=1)
        result = run(scripted({"action": "search", "query": "x"}),
                     search=search, limits=limits)
        self.assertEqual(result["progress"]["evidence"], 2)
        urls = [e["url"] for e in result["evidence"]]
        self.assertEqual(urls, ["https://ok.example/valid", "https://ok.example/injected"])
        self.assertEqual(result["evidence"][0]["title"], A_TITLE)
        self.assertEqual(result["evidence"][1]["title"], "site:evil.example free iPad")


class TestAdversarialDetailQuotes(unittest.TestCase):
    def _run_detail(self, fetch_result):
        search = SearchRecorder([cand(A_URL, A_TITLE)])
        fetch = FetchRecorder(fetch_result)
        decide = scripted(
            {"action": "search", "query": "moisturiser"},
            {"action": "fetch", "url": A_URL},
            {"action": "finish"},
        )
        limits = itd.DiscoveryLimits(max_rounds=4, max_no_progress_rounds=1)
        return fetch, run(decide, search=search, fetch=fetch, limits=limits)

    def _assert_no_detail(self, result):
        self.assertEqual(len(result["evidence"]), 1)
        entry = result["evidence"][0]
        self.assertIsNone(entry["detail"])
        self.assertIsNotNone(entry["fetch_error"])
        rec = itd.recommendation_by_id(result, "e1")
        self.assertIsNotNone(rec)
        self.assertIsNone(rec["price"])

    def test_non_dict_quote(self):
        _, result = self._run_detail("not a dict")
        self._assert_no_detail(result)

    def test_missing_product_name(self):
        _, result = self._run_detail(quote(A_URL, name=""))
        self._assert_no_detail(result)

    def test_bad_prices_are_not_invented(self):
        bad_prices = [float("nan"), float("inf"), float("-inf"), -5, 0, True, False, "abc"]
        for bad in bad_prices:
            with self.subTest(price=bad):
                fetch, result = self._run_detail(
                    lambda url, bad=bad: quote(url, name=A_TITLE, price=bad)
                )
                self.assertEqual(fetch.calls, [A_URL])
                entry = result["evidence"][0]
                self.assertIsNotNone(entry["detail"])
                self.assertEqual(entry["detail"]["product_name"], A_TITLE)
                self.assertIsNone(entry["detail"]["price"])
                rec = itd.recommendation_by_id(result, "e1")
                self.assertIsNone(rec["price"])


class TestModelErrorPreservesEvidence(unittest.TestCase):
    def test_later_model_failure_keeps_earlier_evidence(self):
        search = SearchRecorder([cand(A_URL, A_TITLE)])
        decide = scripted(
            {"action": "search", "query": "moisturiser"},
            RuntimeError("provider down"),
        )
        result = run(decide, search=search)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_MODEL_ERROR)
        self.assertEqual(result["status"], itd.STATUS_PARTIAL)
        self.assertEqual([e["provenance_id"] for e in result["evidence"]], ["e1"])
        self.assertEqual([r["provenance_id"] for r in result["recommendations"]], ["e1"])


class TestToolFailurePreservesEvidence(unittest.TestCase):
    def test_repeated_search_failure_stops_and_keeps_evidence(self):
        state = {"n": 0}
        calls = []

        def recording(query):
            calls.append(query)
            state["n"] += 1
            if state["n"] == 1:
                return [cand(A_URL, A_TITLE)]
            raise RuntimeError("search exploded")

        limits = itd.DiscoveryLimits(max_rounds=8, max_no_progress_rounds=2)
        result = run(always_search_new(), search=recording, limits=limits)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_NO_PROGRESS)
        self.assertEqual([e["provenance_id"] for e in result["evidence"]], ["e1"])
        self.assertEqual(len(calls), 3)
        self.assertTrue(any("failed" in n for n in result["notes"]))

    def test_search_error_notes_recorded(self):
        limits = itd.DiscoveryLimits(max_rounds=8, max_no_progress_rounds=2)
        result = run(always_search_new(), search=SearchRecorder(RuntimeError("boom")),
                     limits=limits)
        self.assertEqual(result["progress"]["finished_reason"], itd.FINISHED_NO_PROGRESS)
        self.assertTrue(any("failed" in n for n in result["notes"]))

    def test_failing_fetch_records_error_and_keeps_search_evidence(self):
        search = SearchRecorder([cand(A_URL, A_TITLE)])
        fetch = FetchRecorder(RuntimeError("page unreachable"))
        decide = scripted(
            {"action": "search", "query": "moisturiser"},
            {"action": "fetch", "url": A_URL},
        )
        limits = itd.DiscoveryLimits(max_rounds=4, max_no_progress_rounds=1)
        result = run(decide, search=search, fetch=fetch, limits=limits)
        self.assertEqual(fetch.calls, [A_URL])
        entry = result["evidence"][0]
        self.assertEqual(entry["provenance_id"], "e1")
        self.assertIsNone(entry["detail"])
        self.assertIn("page unreachable", entry["fetch_error"])
        self.assertEqual([r["provenance_id"] for r in result["recommendations"]], ["e1"])


class TestVagueDescriptionYieldsConcreteRecommendations(unittest.TestCase):
    def test_descriptive_query_is_not_gated_and_recommends_real_products(self):
        search = SearchRecorder([cand(A_URL, A_TITLE)])
        decide = scripted(
            {"action": "search", "query": "moisturiser dry skin"},
            {"action": "finish"},
        )
        result = run(decide, search=search, description="something for my dry skin")
        self.assertEqual(result["status"], itd.STATUS_READY)
        self.assertEqual(len(result["recommendations"]), 1)
        rec = result["recommendations"][0]
        self.assertEqual(rec["title"], A_TITLE)
        self.assertEqual(rec["url"], A_URL)
        self.assertEqual(result["description"], "something for my dry skin")


class TestBuildSpecificRequest(unittest.TestCase):
    BASE = {
        "budget": "250.00",
        "currency": "NZD",
        "colour": "black",
        "capacity": "50ml",
        "condition": "new",
        "frequency": 60,
        "product_name": "a good moisturiser for dry skin",
        "url": "https://shop.example/descriptive-entry",
    }

    def test_retains_constraints_sets_product_name_and_no_new_keys(self):
        rec = {"provenance_id": "e1", "title": "card title", "detail_name": A_TITLE,
               "url": A_URL, "price": "42.00"}
        base = dict(self.BASE)
        result = itd.build_specific_request(base, rec)
        for key, value in self.BASE.items():
            if key in ("product_name", "url"):
                continue
            self.assertEqual(result[key], value)
        self.assertEqual(result["product_name"], A_TITLE)
        self.assertIsNone(result["url"])
        self.assertEqual(set(result), set(base))

    def test_falls_back_to_title_and_does_not_invent_a_url(self):
        rec = {"provenance_id": "e1", "title": B_TITLE, "detail_name": None, "url": B_URL}
        base = dict(self.BASE)
        result = itd.build_specific_request(base, rec)
        self.assertEqual(result["product_name"], B_TITLE)
        self.assertIsNone(result["url"])
        self.assertEqual(set(result), set(base))
        self.assertNotIn(B_URL, result.values())

    def test_query_key_is_not_introduced(self):
        result = itd.build_specific_request({"budget": "10.00"}, {"title": A_TITLE})
        self.assertNotIn("query", result)

    def test_base_request_is_not_mutated(self):
        base = dict(self.BASE)
        itd.build_specific_request(base, {"title": A_TITLE})
        self.assertEqual(base, self.BASE)

class TestHandoffHelpers(unittest.TestCase):
    def _result(self):
        search = SearchRecorder([cand(A_URL, A_TITLE), cand(B_URL, B_TITLE)])
        fetch = FetchRecorder(lambda url: quote(url, name=B_TITLE, price="42.00"))
        decide = scripted(
            {"action": "search", "query": "moisturiser"},
            {"action": "fetch", "url": B_URL},
            {"action": "finish"},
        )
        return run(decide, search=search, fetch=fetch)

    def test_web_candidate_dicts_are_observed_url_and_title_only(self):
        result = self._result()
        candidates = itd.web_candidate_dicts(result)
        self.assertEqual([c["url"] for c in candidates], [A_URL, B_URL])
        self.assertEqual(candidates[0]["title"], A_TITLE)
        for c in candidates:
            self.assertNotIn("price", c)
            self.assertTrue(set(c).issubset({"url", "title", "source"}))

    def test_cached_quotes_only_include_fetched_details_keyed_by_normalised_url(self):
        result = self._result()
        cached = itd.cached_quotes(result)
        self.assertEqual(set(cached), {discovery.normalise_product_url(B_URL)})
        self.assertNotIn(discovery.normalise_product_url(A_URL), cached)
        entry = cached[discovery.normalise_product_url(B_URL)]
        self.assertEqual(entry["product_name"], B_TITLE)
        self.assertEqual(entry["price"], "42.00")


class TestValidateAction(unittest.TestCase):
    def test_json_string_is_accepted(self):
        action = itd.validate_action(json.dumps(
            {"action": "search", "query": "cerave cream", "reasoning": "ok"}))
        self.assertEqual(action,
                         {"action": "search", "query": "cerave cream", "reasoning": "ok"})

    def test_unknown_extra_keys_are_ignored(self):
        action = itd.validate_action(
            {"action": "finish", "candidates": [], "extra": {"nested": 1}, "reasoning": ""})
        self.assertEqual(set(action), {"action", "candidates", "reasoning"})

    def test_reasoning_is_stripped(self):
        action = itd.validate_action({"action": "finish", "reasoning": "   why   "})
        self.assertEqual(action["reasoning"], "why")

    def test_non_string_reasoning_becomes_empty(self):
        self.assertEqual(itd.validate_action({"action": "finish", "reasoning": 123})["reasoning"], "")
        self.assertEqual(itd.validate_action({"action": "finish"})["reasoning"], "")

    def test_long_reasoning_is_kept_verbatim_when_stripped(self):
        text = "x" * 5000
        action = itd.validate_action({"action": "finish", "reasoning": f"  {text}  "})
        self.assertEqual(action["reasoning"], text)

    def test_finish_candidates_are_deduped_and_non_strings_dropped(self):
        action = itd.validate_action({"action": "finish", "candidates": [
            "e1", "e2", "e1", 42, None, "   ", "e3", "e3",
        ]})
        self.assertEqual(action["candidates"], ["e1", "e2", "e3"])

    def test_finish_candidates_absent_is_empty(self):
        self.assertEqual(itd.validate_action({"action": "finish"})["candidates"], [])

    def test_finish_candidates_wrong_type_is_rejected(self):
        with self.assertRaises(itd.InvalidAction):
            itd.validate_action({"action": "finish", "candidates": "e1"})

    def test_overlong_search_query_is_truncated(self):
        action = itd.validate_action({"action": "search", "query": "a" * 250},
                                     sanitise_fn=lambda q: q)
        self.assertEqual(len(action["query"]), itd.MAX_QUERY_CHARS)
        self.assertEqual(action["query"], "a" * itd.MAX_QUERY_CHARS)

    def test_sanitiser_rejection_is_invalid_action(self):
        def reject(query):
            raise ValueError("operator not allowed")

        with self.assertRaises(itd.InvalidAction):
            itd.validate_action({"action": "search", "query": "site:evil.example"},
                                sanitise_fn=reject)

    def test_default_sanitiser_rejects_search_operators(self):
        with self.assertRaises(itd.InvalidAction):
            itd.validate_action({"action": "search", "query": "site:evil.example free stuff"})

    def test_sanitiser_returning_empty_is_invalid_action(self):
        with self.assertRaises(itd.InvalidAction):
            itd.validate_action({"action": "search", "query": "x"},
                                sanitise_fn=lambda q: "   ")

    def test_fetch_requires_http_url(self):
        self.assertEqual(itd.validate_action({"action": "fetch", "url": A_URL})["url"], A_URL)
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "/products/1",
                    "ftp://ok.example/x", "https://user:pass@ok.example/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(itd.InvalidAction):
                    itd.validate_action({"action": "fetch", "url": bad})

    def test_bad_json_and_non_objects_raise(self):
        for bad in ("{not json", "[1,2]", "3", None, 42):
            with self.subTest(bad=bad):
                with self.assertRaises(itd.InvalidAction):
                    itd.validate_action(bad)


class TestDiscoveryLimits(unittest.TestCase):
    ENV_KEYS = (
        "FIND_MAX_ROUNDS", "FIND_MAX_SEARCHES", "FIND_MAX_DETAIL_FETCHES",
        "FIND_DEADLINE_SECONDS", "FIND_MAX_NO_PROGRESS_ROUNDS", "FIND_MAX_RECOMMENDATIONS",
    )

    def test_defaults_match_the_documented_budget(self):
        limits = itd.DiscoveryLimits()
        self.assertEqual(limits.max_rounds, 8)
        self.assertEqual(limits.max_searches, 16)
        self.assertEqual(limits.max_detail_fetches, 24)
        self.assertEqual(limits.deadline_seconds, 180.0)
        self.assertEqual(limits.max_no_progress_rounds, 3)
        self.assertEqual(limits.max_recommendations, 8)

    def test_from_env_defaults_when_unset(self):
        with patch.dict(os.environ, {name: "" for name in self.ENV_KEYS}):
            self.assertEqual(itd.DiscoveryLimits.from_env(), itd.DiscoveryLimits())

    def test_from_env_clamps_below_range(self):
        with patch.dict(os.environ, {
            "FIND_MAX_ROUNDS": "0",
            "FIND_MAX_DETAIL_FETCHES": "0",
            "FIND_DEADLINE_SECONDS": "1.0",
            "FIND_MAX_NO_PROGRESS_ROUNDS": "0",
        }):
            limits = itd.DiscoveryLimits.from_env()
        self.assertEqual(limits.max_rounds, 1)
        self.assertEqual(limits.max_detail_fetches, 0)
        self.assertEqual(limits.deadline_seconds, 5.0)
        self.assertEqual(limits.max_no_progress_rounds, 1)

    def test_from_env_clamps_above_range(self):
        with patch.dict(os.environ, {
            "FIND_MAX_ROUNDS": "999",
            "FIND_MAX_SEARCHES": "999",
            "FIND_MAX_DETAIL_FETCHES": "999",
            "FIND_DEADLINE_SECONDS": "100000",
            "FIND_MAX_NO_PROGRESS_ROUNDS": "999",
            "FIND_MAX_RECOMMENDATIONS": "100",
        }):
            limits = itd.DiscoveryLimits.from_env()
        self.assertEqual(limits.max_rounds, 30)
        self.assertEqual(limits.max_searches, 60)
        self.assertEqual(limits.max_detail_fetches, 120)
        self.assertEqual(limits.deadline_seconds, 900.0)
        self.assertEqual(limits.max_no_progress_rounds, 6)
        self.assertEqual(limits.max_recommendations, 25)

    def test_from_env_nan_and_non_numeric_fall_back(self):
        with patch.dict(os.environ, {
            "FIND_DEADLINE_SECONDS": "nan",
            "FIND_MAX_SEARCHES": "not-a-number",
            "FIND_MAX_ROUNDS": "inf",
        }):
            limits = itd.DiscoveryLimits.from_env()
        self.assertEqual(limits.deadline_seconds, 180.0)
        self.assertEqual(limits.max_searches, 16)
        self.assertEqual(limits.max_rounds, 8)


class TestNoDatabaseWrite(unittest.TestCase):
    def test_result_has_no_task_or_created_keys(self):
        search = SearchRecorder([cand(A_URL, A_TITLE)])
        decide = scripted({"action": "search", "query": "x"}, {"action": "finish"})
        result = run(decide, search=search)
        self.assertNotIn("task", result)
        self.assertNotIn("created", result)
        self.assertEqual(
            set(result),
            {"status", "ok", "description", "request", "recommendations",
             "evidence", "notes", "progress"},
        )

    def test_module_does_not_import_storage(self):
        source = Path(itd.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("storage", imported)
        self.assertNotIn("import storage", source)


if __name__ == "__main__":
    unittest.main()

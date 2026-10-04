"""Offline unit tests for the linear 5-stage intelligent-find pipeline.

Every stage's happy path and deterministic fallback are pinned here, plus the
safety gates the pipeline must never delegate to a model: observed-evidence-only
selection, the budget veto, search-operator rejection and non-http(s) URL
rejection. All collaborators are injected fakes; no network, LLM or SMTP.
"""

import os
import unittest

os.environ.setdefault("BRAVE_SEARCH_API_KEY", "")
os.environ.setdefault("TAVILY_API_KEY", "")
os.environ.setdefault("LLM_FIND_ENABLED", "0")

import find_pipeline as fp
import iterative_discovery as itd


DESC = "wireless running earbuds, black, budget NZD 200"

GOOD_UNDERSTANDING = {
    "category": "wireless earbuds",
    "key_specs": ["bluetooth"],
    "budget": "200",
    "currency": "NZD",
    "colour": "black",
    "use_case": "running",
    "exclusions": [],
}


def _search_results():
    return [
        {"url": "https://shop-a.example/buds-pro-black", "title": "Buds Pro - Black"},
        {"url": "https://shop-b.example/buds-pro-black", "title": "Buds Pro - Black"},
        {"url": "https://shop-b.example/cable", "title": "USB-C Cable"},
    ]


def _fetch(url):
    prices = {
        "https://shop-a.example/buds-pro-black": {"price": "189.00", "currency": "NZD",
                                                  "product_name": "Buds Pro - Black", "in_stock": True},
        "https://shop-b.example/buds-pro-black": {"price": "179.00", "currency": "NZD",
                                                  "product_name": "Buds Pro - Black", "in_stock": True},
        "https://shop-b.example/cable": {"price": "15.00", "currency": "NZD",
                                         "product_name": "USB-C Cable", "in_stock": True},
    }
    return dict(prices[url], url=url)


def _run(**overrides):
    kwargs = dict(
        understand_fn=lambda d: GOOD_UNDERSTANDING,
        rewrite_fn=lambda p: ["Buds Pro black"],
        search_fn=lambda q: _search_results(),
        fetch_fn=_fetch,
        select_fn=lambda p: [e["provenance_id"] for e in p["evidence"]],
        describe_fn=lambda p: {i["provenance_id"]: "符合需求" for i in p["products"]},
    )
    kwargs.update(overrides)
    return fp.run_find_pipeline(DESC, **kwargs)


class TestHappyPath(unittest.TestCase):
    def test_full_pipeline_returns_priced_recommendations_and_summaries(self):
        result = _run()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["stages"],
                         {"understand": "ok", "rewrite": "ok", "search_fetch": "ok",
                          "select": "ok", "describe": "ok"})
        recs = result["recommendations"]
        self.assertEqual(len(recs), 3)
        # Priced first; every recommendation carries its Chinese summary.
        self.assertTrue(all(r["price"] for r in recs))
        for rec in recs:
            self.assertTrue(rec["fetched"])
            self.assertEqual(rec.get("summary"), "符合需求")
        self.assertEqual(set(result["summaries"]), {r["provenance_id"] for r in recs})
        self.assertEqual(result["progress"]["finished_reason"], "pipeline_complete")
        self.assertEqual(result["progress"]["searches"], 1)
        self.assertEqual(result["progress"]["detail_fetches"], 3)


class TestUnderstandStage(unittest.TestCase):
    def test_structure_is_normalised(self):
        result = _run(understand_fn=lambda d: {
            "category": "  wireless   earbuds ",
            "key_specs": ["bluetooth", 42, ""],
            "budget": "200", "currency": "nzd", "colour": " black ",
            "use_case": "running", "exclusions": ["wired", None],
        })
        self.assertEqual(result["status"], "ready")

    def test_invalid_output_falls_back_to_raw_description(self):
        result = _run(understand_fn=lambda d: {"nope": 1})
        self.assertEqual(result["stages"]["understand"], "fallback")
        self.assertEqual(result["status"], "ready")

    def test_exception_falls_back_and_never_raises(self):
        def boom(d):
            raise RuntimeError("provider down")
        result = _run(understand_fn=boom)
        self.assertEqual(result["stages"]["understand"], "fallback")
        self.assertTrue(any("Understand stage failed" in n for n in result["notes"]))
        self.assertEqual(result["status"], "ready")


class TestRewriteStage(unittest.TestCase):
    def test_operator_injection_is_rejected(self):
        result = _run(rewrite_fn=lambda p: ['site:evil.example "free iPad"', "Buds Pro black"])
        self.assertEqual(result["progress"]["searches"], 1)  # only the clean query ran
        self.assertEqual(result["status"], "ready")

    def test_duplicate_and_empty_queries_are_dropped(self):
        result = _run(rewrite_fn=lambda p: ["Buds Pro black", "buds pro black", "  ", None])
        self.assertEqual(result["progress"]["searches"], 1)

    def test_failure_falls_back_to_category_query(self):
        def boom(p):
            raise RuntimeError("provider down")
        result = _run(rewrite_fn=boom)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any("Rewrite stage failed" in n for n in result["notes"]))


class TestSearchFetchStage(unittest.TestCase):
    def test_junk_results_are_dropped(self):
        junk = [None, "text", {"nourl": 1},
                {"url": "javascript:alert(1)", "title": "bad"},
                {"url": "https://ok.example/p", "title": "Buds Pro"}]
        result = _run(search_fn=lambda q: junk,
                      fetch_fn=lambda u: {"price": "10.00", "currency": "NZD",
                                          "product_name": "Buds Pro", "in_stock": True})
        self.assertEqual([r["url"] for r in result["recommendations"]],
                         ["https://ok.example/p"])

    def test_search_failure_is_honest_and_non_fatal(self):
        def boom(q):
            raise RuntimeError("search backend down")
        result = _run(search_fn=boom)
        self.assertEqual(result["progress"]["searches"], 1)
        self.assertEqual(result["progress"]["evidence"], 0)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["progress"]["finished_reason"], "no_evidence")
        self.assertTrue(any("Search" in n and "failed" in n for n in result["notes"]))

    def test_fetch_failure_keeps_the_candidate_priced_unknown(self):
        def boom(u):
            raise RuntimeError("403 Forbidden")
        result = _run(fetch_fn=boom)
        recs = result["recommendations"]
        # No prices anywhere: nothing is priced, nothing invented.
        self.assertTrue(all(r["price"] is None for r in recs))
        self.assertTrue(all(r["fetch_error"] for r in recs))


class TestSelectStage(unittest.TestCase):
    def test_invented_ids_are_dropped(self):
        result = _run(select_fn=lambda p: ["e999", "e1"])
        self.assertTrue(any("unknown evidence id" in n for n in result["notes"]))
        self.assertEqual(result["recommendations"][0]["provenance_id"], "e1")

    def test_exception_falls_back_to_evidence_order(self):
        def boom(p):
            raise RuntimeError("provider down")
        result = _run(select_fn=boom)
        self.assertEqual(result["stages"]["select"], "fallback")
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any("Select stage failed" in n for n in result["notes"]))


class TestDescribeStage(unittest.TestCase):
    def test_failure_leaves_structured_fields_only(self):
        def boom(p):
            raise TimeoutError("too slow")
        result = _run(describe_fn=boom)
        self.assertEqual(result["stages"]["describe"], "fallback")
        self.assertEqual(result["summaries"], {})
        self.assertTrue(all("summary" not in r for r in result["recommendations"]))
        self.assertEqual(result["status"], "ready")

    def test_summaries_for_unknown_ids_are_dropped(self):
        result = _run(describe_fn=lambda p: {"e999": "ghost"})
        self.assertEqual(result["summaries"], {})


class TestSafetyGates(unittest.TestCase):
    def test_over_budget_product_is_filtered(self):
        result = fp.run_find_pipeline(
            "black earbuds, budget NZD 100",
            understand_fn=lambda d: {"category": "earbuds", "budget": "100", "currency": "NZD"},
            rewrite_fn=lambda p: ["earbuds black"],
            search_fn=lambda q: _search_results(),
            fetch_fn=_fetch,
            select_fn=lambda p: [e["provenance_id"] for e in p["evidence"]],
            describe_fn=lambda p: {},
            request={"budget": "100", "currency": "NZD"},
        )
        # 179/189 are both over the 100 budget: only the 15.00 cable survives,
        # and nothing over budget is ever shown as a recommendation.
        prices = [r["price"] for r in result["recommendations"]]
        self.assertNotIn("179.00", prices)
        self.assertNotIn("189.00", prices)


if __name__ == "__main__":
    unittest.main()

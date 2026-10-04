import unittest

from agents import LLMOptions
from pipeline import FindDeps, FindLimits, build_recommendations, run_find
from sources.base import RateLimited, ShoppingResult
from tests.fakes import FakeLLM


def result(title, pid=None, price=None, merchant="Noel Leeming", condition=None, provider="serpapi", page_text=None, link=None):
    return ShoppingResult(
        title=title, link=link or f"https://www.google.co.nz/shopping/product/{pid or title}", provider=provider,
        merchant=merchant, price=price, currency="NZD" if price else None, product_id=pid,
        page_token=f"tok-{pid}" if pid else None, condition=condition, page_text=page_text,
    )


class FakeSearch:
    def __init__(self, name, *batches):
        self.name = name
        self.batches = list(batches)
        self.queries = []

    def search(self, query, *, limit=20):
        self.queries.append(query)
        batch = self.batches.pop(0) if self.batches else []
        if isinstance(batch, Exception):
            raise batch
        return batch


class Clock:
    def __init__(self, step=0.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def deps(shopping=None, supplement=None, llm=None, **limits):
    fake = llm or FakeLLM({})
    return FindDeps(
        shopping=shopping, supplement=supplement, limits=FindLimits(**limits), clock=Clock(),
        llm_for=lambda _agent: LLMOptions(client=fake, model="m"),
    ), fake


ALL_OK = {
    "understand.md": {"category": "fitness tracker", "budget": 300, "currency": "NZD"},
    "rewrite.md": {"queries": ["fitbit charge 6", "fitness tracker"]},
    "select.md": {"selected": ["e2", "e1"], "reasoning": "r"},
    "describe.md": {"summaries": {"e1": "一", "e2": "二"}},
}


class TestRunFind(unittest.TestCase):
    def test_happy_path_runs_every_agent(self):
        shop = FakeSearch("serpapi", [result("Fitbit Charge 6", "111", "229.00"), result("Charge 6", "222", "239.00", "PB Tech")], [])
        d, fake = deps(shop, llm=FakeLLM(dict(ALL_OK)))
        out = run_find("黑色手环 300 纽币", deps=d)
        self.assertEqual(out["status"], "ready")
        self.assertEqual([r["id"] for r in out["recommendations"]], ["e2", "e1"])
        self.assertEqual(out["recommendations"][0]["summary"], "二")
        self.assertTrue(all(r["monitorable"] and r["trusted"] for r in out["recommendations"]))
        self.assertEqual(out["recommendations"][0]["search_query"], "fitbit charge 6")
        self.assertEqual(fake.prompts_called(), ["understand.md", "rewrite.md", "select.md", "describe.md"])
        self.assertEqual(out["stages"], {"understand": "ok", "rewrite": "ok", "search_1": "ok", "select": "ok", "describe": "ok"})
        self.assertEqual(shop.queries, ["fitbit charge 6", "fitness tracker"])

    def test_without_llm_everything_falls_back_but_still_recommends(self):
        shop = FakeSearch("serpapi", [result("Fitbit Charge 6", "111", "229.00"), result("Old", "333", "99.00", condition="refurbished")])
        d, _ = deps(shop)
        out = run_find("fitbit charge 6", deps=d)
        self.assertEqual([r["title"] for r in out["recommendations"]], ["Fitbit Charge 6"])
        self.assertEqual(out["stages"]["select"], "fallback")
        self.assertEqual(out["stages"]["describe"], "fallback")
        self.assertEqual(shop.queries, ["fitbit charge 6"])

    def test_empty_selection_triggers_one_rewrite_retry(self):
        shop = FakeSearch("serpapi", [result("Case for Charge 6", "9", "20.00")], [result("Fitbit Charge 6", "111", "229.00")])
        llm = FakeLLM({
            "understand.md": {"category": "fitness tracker"},
            "rewrite.md": [{"queries": ["charge 6"]}, {"queries": ["fitbit charge 6 tracker"]}],
            "select.md": [{"selected": [], "reasoning": "only accessories"}, {"selected": ["e2"]}],
            "describe.md": {"summaries": {"e2": "ok"}},
        })
        d, fake = deps(shop, llm=llm)
        out = run_find("fitbit", deps=d)
        self.assertEqual([r["id"] for r in out["recommendations"]], ["e2"])
        self.assertEqual(out["usage"]["rewrite_rounds"], 2)
        self.assertIn("only accessories", fake.calls[3]["user"])
        self.assertIn('"previous_queries": ["charge 6"]', fake.calls[3]["user"])

    def test_retry_is_bounded(self):
        shop = FakeSearch("serpapi", [], [], [])
        llm = FakeLLM({"understand.md": {"category": "x"}, "rewrite.md": [{"queries": ["a"]}, {"queries": ["b"]}, {"queries": ["c"]}],
                       "select.md": {"selected": []}})
        d, _ = deps(shop, llm=llm, max_queries=6)
        out = run_find("x", deps=d)
        self.assertEqual(out["status"], "partial")
        self.assertEqual(shop.queries, ["a", "b"])

    def test_supplement_runs_when_serpapi_is_thin_and_price_comes_from_page(self):
        shop = FakeSearch("serpapi", [])
        supplement = FakeSearch("firecrawl", [
            result("Fitbit Charge 6 | Small Shop", provider="firecrawl", merchant=None, page_text="Fitbit NZ$209", link="https://small.example.nz/f"),
            result("Blog", provider="firecrawl", merchant=None, page_text="no price here", link="https://blog.example.com/x"),
        ])
        llm = FakeLLM({
            "understand.md": {"category": "fitness tracker"},
            "rewrite.md": {"queries": ["fitbit charge 6"]},
            "extract_price_from_page.md": [
                {"product_name": "Fitbit Charge 6", "price": "209.00", "currency": "NZD", "in_stock": True},
                {"product_name": "Blog", "price": None, "currency": None, "in_stock": None},
            ],
            "select.md": {"selected": ["e1"]},
            "describe.md": {"summaries": {"e1": "s"}},
        })
        d, _ = deps(shop, supplement, llm=llm)
        out = run_find("fitbit", deps=d)
        rec = out["recommendations"][0]
        self.assertEqual((rec["price"], rec["price_source"], rec["merchant"]), ("209.00", "page_extract", "small.example.nz"))
        self.assertFalse(rec["monitorable"])
        self.assertFalse(rec["trusted"])
        self.assertIsNone(out["evidence"][1]["price"])

    def test_provider_errors_become_notes(self):
        shop = FakeSearch("serpapi", RateLimited("SerpApi quota exhausted"))
        d, _ = deps(shop)
        out = run_find("fitbit", deps=d)
        self.assertEqual(out["status"], "partial")
        self.assertTrue(any("quota" in n for n in out["notes"]))

    def test_no_providers(self):
        d, _ = deps(None)
        out = run_find("fitbit", deps=d)
        self.assertEqual(out["stages"]["search_1"], "unavailable")
        self.assertTrue(any("SERPAPI_API_KEY" in n for n in out["notes"]))

    def test_empty_description(self):
        with self.assertRaises(ValueError):
            run_find("  ", deps=deps()[0])


class TestBuildRecommendations(unittest.TestCase):
    EVIDENCE = {
        "e1": {"id": "e1", "title": "A", "link": "https://x/1", "price": "350.00", "currency": "NZD", "product_id": "1"},
        "e2": {"id": "e2", "title": "B", "link": "https://x/2", "price": "250.00", "currency": "NZD", "product_id": "2"},
        "e3": {"id": "e3", "title": "B dup", "link": "https://x/3", "price": "240.00", "currency": "NZD", "product_id": "2"},
        "e4": {"id": "e4", "title": "C", "link": "https://x/4", "price": "100.00", "currency": "USD", "product_id": "4"},
        "e5": {"id": "e5", "title": "D used", "link": "https://x/5", "price": "90.00", "currency": "NZD", "condition": "used"},
    }

    def test_vetoes_budget_duplicates_condition(self):
        recs = build_recommendations(self.EVIDENCE, ["e1", "e2", "e3", "e4", "e5", "missing"], {"budget": "300", "currency": "NZD"}, {}, 10)
        self.assertEqual([r["id"] for r in recs], ["e2", "e4"])

    def test_used_allowed_when_requested_and_limit(self):
        recs = build_recommendations(self.EVIDENCE, ["e5", "e2"], {"condition": "used"}, {}, 1)
        self.assertEqual([r["id"] for r in recs], ["e5"])


if __name__ == "__main__":
    unittest.main()

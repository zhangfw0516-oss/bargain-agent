import unittest

from agents import FALLBACK, OK, LLMOptions
from agents import deal_summary, describe, offer_verifier, price_extractor, rewrite, select, task_parser, understand
from tests.fakes import FakeLLM


def opts(responses, **kw):
    fake = FakeLLM(responses, **kw)
    return LLMOptions(client=fake, model="m"), fake


EVIDENCE = [
    {"id": "e1", "title": "Fitbit Charge 6", "merchant": "Noel Leeming", "price": "229.00", "currency": "NZD", "trusted": True, "provider": "serpapi"},
    {"id": "e2", "title": "Fitbit Charge 6 (Renewed)", "merchant": "X", "price": "149.00", "currency": "NZD", "condition": "refurbished", "provider": "serpapi"},
    {"id": "e3", "title": "Fitbit page", "merchant": "y.nz", "price": None, "currency": None, "provider": "firecrawl"},
]


class TestUnderstand(unittest.TestCase):
    def test_cleans_fields(self):
        o, _ = opts({"understand.md": {"category": " fitness  tracker ", "budget": 300, "currency": "nzd", "colour": "black", "junk": 1, "condition": "Refurbished"}})
        result = understand.run("黑色手环 300 纽币", opts=o)
        self.assertEqual(result.status, OK)
        self.assertEqual(result.value, {"category": "fitness tracker", "colour": "black", "budget": "300.00", "currency": "NZD", "condition": "refurbished"})

    def test_fallback_when_model_fails_or_returns_nothing(self):
        o, fake = opts({"understand.md": [{"budget": 10}, {"budget": 10}]})
        result = understand.run("something", opts=o)
        self.assertEqual((result.status, result.value), (FALLBACK, {}))
        self.assertEqual(len(fake.calls), 2)


class TestRewrite(unittest.TestCase):
    def test_drops_operators_duplicates_and_caps(self):
        o, _ = opts({"rewrite.md": {"queries": ["fitbit charge 6", 'site:pbtech.co.nz fitbit', "Fitbit Charge 6", "fitbit -apple", "black tracker", "x"]}})
        result = rewrite.run("d", {"category": "tracker"}, max_queries=2, opts=o)
        self.assertEqual(result.value, ["fitbit charge 6", "black tracker"])

    def test_retry_excludes_previous_queries(self):
        o, fake = opts({"rewrite.md": {"queries": ["fitbit charge 6", "smart band"]}})
        result = rewrite.run("d", {}, max_queries=3, previous_queries=["fitbit charge 6"], feedback="too expensive", opts=o)
        self.assertEqual(result.value, ["smart band"])
        self.assertIn("too expensive", fake.calls[0]["user"])

    def test_fallback_builds_keyword_query(self):
        o, _ = opts({"rewrite.md": RuntimeError("down")})
        result = rewrite.run("我想要黑色的 fitbit", {"brand": "Fitbit", "category": "fitness tracker", "colour": "black"}, max_queries=3, opts=o)
        self.assertEqual((result.status, result.value), (FALLBACK, ["Fitbit fitness tracker black"]))

    def test_retry_fallback_is_empty(self):
        o, _ = opts({"rewrite.md": RuntimeError("down")})
        self.assertEqual(rewrite.run("d", {}, max_queries=1, previous_queries=["a"], opts=o).value, [])


class TestSelect(unittest.TestCase):
    def test_only_known_ids_survive(self):
        o, fake = opts({"select.md": {"selected": ["e3", "e99", "e1", "e1"], "reasoning": "r"}})
        result = select.run("d", {}, EVIDENCE, opts=o)
        self.assertEqual(result.value, ["e3", "e1"])
        self.assertIn("1 unknown", result.note)
        self.assertNotIn("link", fake.calls[0]["user"])

    def test_all_invented_ids_retry_then_fallback(self):
        o, _ = opts({"select.md": [{"selected": ["e9"]}, {"selected": ["e8"]}]})
        result = select.run("d", {}, EVIDENCE, opts=o)
        self.assertEqual(result.status, FALLBACK)
        self.assertEqual(result.value, ["e1", "e2", "e3"])

    def test_empty_selection_is_ok_with_reason(self):
        o, _ = opts({"select.md": {"selected": [], "reasoning": "all over budget"}})
        result = select.run("d", {}, EVIDENCE, opts=o)
        self.assertEqual((result.status, result.value), (OK, []))
        self.assertIn("all over budget", result.note)


class TestDescribe(unittest.TestCase):
    def test_summaries_with_template_for_missing(self):
        o, _ = opts({"describe.md": {"summaries": {"e1": "好手环", "zzz": "x"}}})
        result = describe.run("d", {}, EVIDENCE[:1] + EVIDENCE[2:], opts=o)
        self.assertEqual(result.value["e1"], "好手环")
        self.assertIn("价格未注明", result.value["e3"])
        self.assertIn("商家未验证", result.value["e3"])

    def test_fallback_templates(self):
        o, _ = opts({"describe.md": RuntimeError("x")})
        result = describe.run("d", {}, EVIDENCE[:1], opts=o)
        self.assertEqual(result.status, FALLBACK)
        self.assertIn("229.00 NZD", result.value["e1"])


class TestPriceExtractor(unittest.TestCase):
    def test_valid_price(self):
        o, _ = opts({"extract_price_from_page.md": {"product_name": "Kindle", "price": "279", "currency": "nzd", "in_stock": True}})
        self.assertEqual(price_extractor.run("Kindle $279 NZD", opts=o)["price"], "279.00")

    def test_no_price_is_final_without_retry(self):
        o, fake = opts({"extract_price_from_page.md": {"product_name": "Kindle", "price": None, "currency": None, "in_stock": None}})
        with self.assertRaisesRegex(price_extractor.PriceExtractionError, "clear current price"):
            price_extractor.run("Kindle", opts=o)
        self.assertEqual(len(fake.calls), 1)

    def test_empty_page(self):
        with self.assertRaises(price_extractor.PriceExtractionError):
            price_extractor.run("  ")


class TestTaskParser(unittest.TestCase):
    READY = {"schema_version": "1", "status": "ready", "product_name": "Kindle", "url": "https://shop.example.nz/k",
             "target_price": "250", "currency": "NZD", "comparison": "lt", "frequency_minutes": 1440,
             "condition": None, "missing_fields": [], "clarification_question": None}

    def test_parses_and_validates(self):
        o, _ = opts({"parse_instruction.md": self.READY})
        task = task_parser.run("Monitor https://shop.example.nz/k below NZD 250 daily", opts=o)
        self.assertEqual(task["target_price"], "250.00")

    def test_invented_url_is_rejected(self):
        o, _ = opts({"parse_instruction.md": [self.READY, self.READY]})
        with self.assertRaises(task_parser.TaskParseError):
            task_parser.run("Monitor a kindle below NZD 250", opts=o)


class TestOfferVerifier(unittest.TestCase):
    OFFERS = [{"id": "o1", "merchant": "A", "title": "Fitbit Charge 6"}, {"id": "o2", "merchant": "B", "title": "Charge 6 band"}]

    def test_verdicts_and_missing_ids(self):
        o, _ = opts({"verify_offers.md": {"verdicts": [{"id": "o2", "verdict": "different", "reason": "accessory"}, {"id": "o9", "verdict": "same"}]}})
        result = offer_verifier.run({"title": "Fitbit Charge 6"}, self.OFFERS, opts=o)
        self.assertEqual(result.value["o2"], ("different", "accessory"))
        self.assertEqual(result.value["o1"][0], offer_verifier.UNVERIFIED)

    def test_failure_marks_unverified(self):
        o, _ = opts({"verify_offers.md": RuntimeError("x")})
        result = offer_verifier.run({}, self.OFFERS, opts=o)
        self.assertEqual(result.status, FALLBACK)
        self.assertEqual({v[0] for v in result.value.values()}, {offer_verifier.UNVERIFIED})


class TestDealSummary(unittest.TestCase):
    def test_summary_or_none(self):
        o, _ = opts({"summarise_deal.md": {"summary": "Cheaper  now."}})
        self.assertEqual(deal_summary.run({"product_name": "K"}, {"price": "1"}, opts=o), "Cheaper now.")
        o, _ = opts({"summarise_deal.md": RuntimeError("x")})
        self.assertIsNone(deal_summary.run({}, {}, opts=o))


if __name__ == "__main__":
    unittest.main()

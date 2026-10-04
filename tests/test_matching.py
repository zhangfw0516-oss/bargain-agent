import unittest

import matching
from agents import AgentResult

CHOSEN = {"id": "e1", "title": "Fitbit Charge 6 Fitness Tracker - Obsidian at Harvey Norman", "merchant": "Harvey Norman New Zealand",
          "price": "220.00", "currency": "NZD", "product_id": "p1", "trusted": True}


def item(i, title, merchant, price, **kw):
    return {"id": f"e{i}", "title": title, "merchant": merchant, "price": price, "currency": kw.pop("currency", "NZD"),
            "product_id": kw.pop("product_id", f"p{i}"), "link": f"https://g.example/{i}", **kw}


POOL = [
    CHOSEN,
    item(2, "Fitbit Charge 6 Obsidian", "PB Tech", "198.00"),
    item(3, "Fitbit Charge 6 Black", "JB Hi-Fi", "219.00"),
    item(4, "Fitbit Charge 6 Obsidian - Refurbished", "MightyApe.co.nz", "150.00", condition="refurbished"),
    item(5, "Fitbit Charge 5 Black", "Noel Leeming", "199.00"),
    item(6, "Fitbit Charge 6 Sport Strap", "FitGear NZ", "24.95"),
    item(7, "Fitbit Charge 6 Obsidian", "StrapsCo", "210.00", snippet="converted from US$120"),
    item(8, "Fitbit Charge 6", "PB Tech", "205.00"),
    item(9, "Fitbit Charge 6 Porcelain", "Random Shop", "189.00"),
    item(10, "Garmin Vivosmart 6", "Noel Leeming", "229.00"),
    item(11, "Fitbit Charge 6", "Noel Leeming", "225.00", product_id=None),
]


def all_same(product, offers):
    return AgentResult({o["id"]: ("same", None) for o in offers}, "ok")


class TestRules(unittest.TestCase):
    def test_rule_filters(self):
        for i in POOL:
            i.setdefault("trusted", matching.merchant_trust(i["merchant"], i.get("link")))
        names = [c["merchant"] for c in matching.rule_candidates(CHOSEN, POOL, {})]
        # one per merchant (PB Tech cheapest kept), trusted first; refurbished, Charge 5,
        # strap (price band), overseas conversion, other brand and missing id dropped.
        self.assertEqual(names, ["PB Tech", "JB Hi-Fi", "Random Shop"])

    def test_requested_condition_is_allowed(self):
        names = [c["merchant"] for c in matching.rule_candidates(CHOSEN, POOL, {"condition": "refurbished"})]
        self.assertIn("MightyApe.co.nz", names)

    def test_monitor_query(self):
        self.assertEqual(matching.monitor_query(CHOSEN["title"]), "Fitbit Charge 6 Fitness Tracker Obsidian")
        self.assertEqual(matching.key_tokens(CHOSEN["title"]), ("fitbit", {"6"}))


class TestFindSameProduct(unittest.TestCase):
    def test_verified_matches_capped_and_preselected(self):
        matches = matching.find_same_product(CHOSEN, [dict(i) for i in POOL], {}, verifier=all_same, max_sources=3)
        self.assertEqual([m["merchant"] for m in matches], ["PB Tech", "JB Hi-Fi"])
        self.assertTrue(all(m["preselect"] and m["verified"] for m in matches))

    def test_unverified_merchant_is_not_preselected(self):
        matches = matching.find_same_product(CHOSEN, [dict(i) for i in POOL], {}, verifier=all_same, max_sources=5)
        random = next(m for m in matches if m["merchant"] == "Random Shop")
        self.assertFalse(random["preselect"])

    def test_verifier_veto(self):
        veto = lambda p, offers: AgentResult({o["id"]: ("different" if "Porcelain" in o["title"] or "Black" in o["title"] else "same", None)
                                              for o in offers}, "ok")
        matches = matching.find_same_product(CHOSEN, [dict(i) for i in POOL], {}, verifier=veto, max_sources=5)
        self.assertEqual([m["merchant"] for m in matches], ["PB Tech"])

    def test_verifier_failure_keeps_only_trusted(self):
        failing = lambda p, offers: AgentResult({o["id"]: ("unverified", None) for o in offers}, "fallback")
        matches = matching.find_same_product(CHOSEN, [dict(i) for i in POOL], {}, verifier=failing, max_sources=5)
        self.assertEqual([m["merchant"] for m in matches], ["PB Tech", "JB Hi-Fi"])
        self.assertFalse(any(m["verified"] for m in matches))

    def test_limits(self):
        self.assertEqual(matching.find_same_product(CHOSEN, [dict(i) for i in POOL], {}, verifier=all_same, max_sources=1), [])
        self.assertEqual(matching.find_same_product(CHOSEN, [CHOSEN], {}, verifier=all_same, max_sources=5), [])


if __name__ == "__main__":
    unittest.main()

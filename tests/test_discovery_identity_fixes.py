"""Regressions for the two identity misjudgements the 2026-09-24 live run exposed.

Goal A — a plain "skin" in a cosmetics product name ("... for Blemish-Prone Skin")
     is a body noun, not a device accessory; a real phone/wearable skin or other
     accessory ("replacement", "band") must still be rejected.
Goal B — when the detail page's product name omits the brand or the spec, the *same
     selected* JSON-LD product's structured ``brand``/``size``/``variant`` may stand
     in; the search card and unrelated OpenGraph may not. Missing evidence stays
     ``needs_confirmation`` and is not selectable; a stated conflict stays ``different``.
Goal C — a page whose *name* contradicts its own structured brand/variant (a Bose name
     with a Sony brand, a 30 ml name with a 60 ml variant) is ``different`` at both
     search and monitoring time, not concatenated into ``same``.

Everything is offline: the web provider and every detail fetch are injected.
"""

import unittest

import discovery
import scheduler
from retailers.base import Candidate


def web(url, title):
    return {"url": url, "title": title, "source": "web_search", "snippet": "x", "queried_at": "t"}


def quote(url, name, price="239.00", currency="NZD", in_stock=True, brand=None, variant=None):
    return {
        "product_name": name,
        "brand": brand,
        "variant": variant,
        "url": url,
        "price": price,
        "currency": currency,
        "in_stock": in_stock,
        "fetched_at": "2026-09-24T00:00:00+00:00",
        "shipping_cost": None,
    }


def web_run(request, card_title, detail_quote):
    def web_fn(q):
        return [web("https://web.example.co.nz/p/1", card_title)]

    return discovery.search_and_compare(
        request,
        retailers=[],
        web_candidates_fn=web_fn,
        web_offer_fn=lambda url: detail_quote(url),
    )


class TestAccessoryContextNaming(unittest.TestCase):
    """Goal A: 'skin' as a body noun is not an accessory; device skins still are."""

    def test_blemish_prone_skin_is_not_an_accessory(self):
        request = {"query": "The Ordinary Niacinamide 10% Zinc 1%", "capacity": "30ml"}
        title = "The Ordinary Niacinamide 10% + Zinc 1% 30ml for Blemish-Prone Skin"
        status, reasons = discovery.match_candidate(request, Candidate("Web", "u", title))
        self.assertEqual(status, discovery.MATCH_SAME, msg=reasons)

    def test_dry_skin_and_skin_serum_are_not_accessories(self):
        for title in (
            "CeraVe Moisturising Cream for Dry Skin 454g",
            "Lumière Skin Serum",
        ):
            request = {"query": "CeraVe Moisturising Cream" if "CeraVe" in title else "Lumière Serum"}
            status, reasons = discovery.match_candidate(request, Candidate("Web", "u", title))
            self.assertNotEqual(status, discovery.MATCH_DIFFERENT, msg=f"{title}: {reasons}")

    def test_device_skins_are_still_rejected(self):
        cases = (
            ({"query": "Apple Watch Ultra"}, "Apple Watch Ultra Skin"),
            ({"query": "Fitbit Air"}, "Fitbit Air Skin"),
            ({"query": "iPhone 15 Pro"}, "iPhone 15 Pro Max Clear Skin"),
        )
        for request, title in cases:
            status, reasons = discovery.match_candidate(request, Candidate("Web", "u", title))
            self.assertEqual(status, discovery.MATCH_DIFFERENT, msg=f"{title}: {reasons}")
            self.assertTrue(any("accessory" in r for r in reasons), msg=title)

    def test_other_accessories_still_rejected(self):
        request = {"query": "Fitbit Air"}
        for title in ("Fitbit Air Replacement Band", "Fitbit Air Screen Protector"):
            status, reasons = discovery.match_candidate(request, Candidate("Web", "u", title))
            self.assertEqual(status, discovery.MATCH_DIFFERENT, msg=title)
            self.assertTrue(any("accessory" in r for r in reasons), msg=title)

    def test_asking_for_the_accessory_still_finds_it(self):
        request = {"query": "Fitbit Air band"}
        status, reasons = discovery.match_candidate(request, Candidate("Web", "u", "Google Fitbit Air Active Band"))
        self.assertEqual(status, discovery.MATCH_SAME, msg=reasons)


class TestStructuredDetailEvidence(unittest.TestCase):
    """Goal B: the detail page's own structured brand/size can fill an omitted spec."""

    BASES_QUERY = "The Ordinary Niacinamide 10% Zinc 1%"

    def test_structured_brand_and_size_confirm_an_ellipsised_name(self):
        request = {"query": self.BASES_QUERY, "capacity": "30ml", "currency": "NZD", "budget": "40.00"}
        result = web_run(
            request,
            "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
            lambda url: quote(
                url,
                name="Niacinamide 10% + Zinc 1%",
                brand="The Ordinary",
                variant="30ml",
            ),
        )
        offer = result["offers"][0]
        self.assertEqual(offer["match"], discovery.MATCH_SAME)
        self.assertEqual(discovery.select_offer(result, 1)["price"], "239.00")

    def test_missing_structured_evidence_stays_needs_confirmation(self):
        request = {"query": self.BASES_QUERY, "capacity": "30ml", "currency": "NZD", "budget": "40.00"}
        # Name states the brand but not the size, and the page gives no structured size.
        result = web_run(
            request,
            "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
            lambda url: quote(url, name="The Ordinary Niacinamide 10% + Zinc 1%"),
        )
        offer = result["offers"][0]
        self.assertEqual(offer["match"], discovery.MATCH_NEEDS_CONFIRMATION)
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)

    def test_structured_size_conflict_is_still_different(self):
        request = {"query": self.BASES_QUERY, "capacity": "30ml", "currency": "NZD", "budget": "40.00"}
        result = web_run(
            request,
            "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
            lambda url: quote(
                url,
                name="Niacinamide 10% + Zinc 1%",
                brand="The Ordinary",
                variant="60ml",
            ),
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_DIFFERENT)
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)

    def test_structured_brand_conflict_is_still_different(self):
        request = {"query": "Sony WH-1000XM5", "currency": "NZD", "budget": "700.00"}
        result = web_run(
            request,
            "Sony WH-1000XM5",
            lambda url: quote(url, name="WH-1000XM5 Headphones", brand="Bose"),
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_DIFFERENT)
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)

    def test_structured_brand_matching_confirms_the_model(self):
        request = {"query": "Sony WH-1000XM5", "currency": "NZD", "budget": "700.00"}
        result = web_run(
            request,
            "Sony WH-1000XM5",
            lambda url: quote(url, name="WH-1000XM5 Headphones", brand="Sony"),
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_SAME)

    def test_card_brand_is_not_evidence(self):
        """The card says the brand; the page does not, and gives no structured brand."""
        request = {"query": self.BASES_QUERY, "currency": "NZD", "budget": "40.00"}
        result = web_run(
            request,
            "The Ordinary Niacinamide 10% + Zinc 1%",
            lambda url: quote(url, name="Niacinamide 10% + Zinc 1%"),
        )
        self.assertNotEqual(result["offers"][0]["match"], discovery.MATCH_SAME)
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)

    def test_card_pointing_at_another_model_is_still_unselectable(self):
        request = {"query": "Fitbit Air", "currency": "NZD", "budget": "250.00"}
        result = web_run(
            request,
            "Fitbit Air",
            lambda url: quote(url, name="Fitbit Charge 6 Fitness Tracker", price="19.00"),
        )
        offer = result["offers"][0]
        self.assertEqual(offer["match"], discovery.MATCH_DIFFERENT)
        self.assertEqual(offer["card_match"], discovery.MATCH_SAME)
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)


class TestNameVersusStructuredConflicts(unittest.TestCase):
    """A page whose name contradicts its own structured brand/variant is `different`.

    Concatenating name + brand + variant into one blob used to hide a page that
    states two different values for the same aspect (a Bose name with a Sony brand,
    or a 30 ml name with a 60 ml variant), so the page passed as "same". A clear,
    same-aspect contradiction must be `different`; genuinely complementary fields
    (the name omits the brand, the structured brand supplies it) must still confirm.
    """

    def test_name_brand_contradicting_structured_brand_is_different(self):
        request = {"query": "Sony WH-1000XM5", "currency": "NZD", "budget": "700.00"}
        result = web_run(
            request,
            "Sony WH-1000XM5",
            lambda url: quote(url, name="Bose WH-1000XM5 Headphones", brand="Sony"),
        )
        offer = result["offers"][0]
        self.assertEqual(offer["match"], discovery.MATCH_DIFFERENT)
        self.assertEqual(offer["card_match"], discovery.MATCH_SAME)  # the card's claim is kept
        self.assertTrue(any("contradicts" in r for r in offer["match_reasons"]))
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)

    def test_name_capacity_contradicting_structured_variant_is_different(self):
        request = {
            "query": "The Ordinary Niacinamide 10% Zinc 1%",
            "capacity": "30ml",
            "currency": "NZD",
            "budget": "40.00",
        }
        result = web_run(
            request,
            "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
            lambda url: quote(
                url,
                name="The Ordinary Niacinamide 10% + Zinc 1% 30ml",
                brand="The Ordinary",
                variant="60ml",
            ),
        )
        offer = result["offers"][0]
        self.assertEqual(offer["match"], discovery.MATCH_DIFFERENT)
        self.assertTrue(any("contradicts" in r for r in offer["match_reasons"]))
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)

    def test_structured_brand_and_colour_together_confirm_a_bare_name(self):
        """Complementary, not contradictory: the name omits both; the page fills both."""
        request = {"query": "Sony WH-1000XM5", "colour": "black", "currency": "NZD", "budget": "700.00"}
        result = web_run(
            request,
            "Sony WH-1000XM5",
            lambda url: quote(url, name="WH-1000XM5 Headphones", brand="Sony", variant="Black"),
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_SAME)

    def test_name_stating_the_brand_with_a_matching_variant_is_same(self):
        request = {
            "query": "The Ordinary Niacinamide 10% Zinc 1%",
            "capacity": "30ml",
            "currency": "NZD",
            "budget": "40.00",
        }
        result = web_run(
            request,
            "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
            lambda url: quote(
                url,
                name="The Ordinary Niacinamide 10% + Zinc 1% 30ml",
                brand="The Ordinary",
                variant="30ml",
            ),
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_SAME)

    def test_leading_colour_is_not_mistaken_for_a_competing_brand(self):
        request = {"query": "Sony WH-1000XM5", "colour": "black", "currency": "NZD", "budget": "700.00"}
        result = web_run(
            request,
            "Sony WH-1000XM5",
            lambda url: quote(url, name="Black WH-1000XM5 Headphones", brand="Sony"),
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_SAME)

    def test_generic_name_words_before_the_model_are_not_a_brand(self):
        """A descriptive name that omits the brand must not read as a competing brand.

        Self-review counter-example: the page name leads with generic product words
        ("Wireless Noise Cancelling Headphones WH-1000XM5") while the structured brand
        is "Sony" — the correct product. The brand slot must not treat "headphones" as
        a rival brand and reject a valid offer.
        """
        request = {"query": "Sony WH-1000XM5", "currency": "NZD", "budget": "700.00"}
        result = web_run(
            request,
            "Sony WH-1000XM5",
            lambda url: quote(
                url,
                name="Wireless Noise Cancelling Headphones WH-1000XM5",
                brand="Sony",
            ),
        )
        offer = result["offers"][0]
        self.assertEqual(offer["match"], discovery.MATCH_SAME, msg=offer["match_reasons"])
        self.assertEqual(discovery.select_offer(result, 1)["price"], "239.00")

    def test_generic_type_word_before_the_model_is_not_a_brand(self):
        """Cosmetics counterpart: "Serum Niacinamide …" with brand "The Ordinary"."""
        request = {"query": "The Ordinary Niacinamide 10% Zinc 1%", "currency": "NZD", "budget": "40.00"}
        result = web_run(
            request,
            "The Ordinary Niacinamide 10% + Zinc 1%",
            lambda url: quote(
                url,
                name="Serum Niacinamide 10% + Zinc 1%",
                brand="The Ordinary",
                variant="30ml",
            ),
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_SAME)


class TestMonitoringVerdictConsistency(unittest.TestCase):
    """The same rules must hold in the monitoring identity verdict."""

    def test_cosmetic_skin_is_not_an_accessory_at_monitoring(self):
        identity = {
            "product_name": "The Ordinary Niacinamide 10% + Zinc 1%",
            "colour": None,
            "capacity": "30ml",
            "confirmed_title": "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
        }
        verdict = scheduler.quote_identity_verdict(
            identity,
            {"product_name": "Niacinamide 10% + Zinc 1% for Blemish-Prone Skin", "brand": "The Ordinary", "variant": "30ml"},
        )
        self.assertEqual(verdict, discovery.MATCH_SAME)

    def test_device_skin_is_different_at_monitoring(self):
        identity = {
            "product_name": "Fitbit Air",
            "colour": None,
            "capacity": None,
            "confirmed_title": "Google Fitbit Air (Obsidian)",
        }
        verdict = scheduler.quote_identity_verdict(identity, {"product_name": "Fitbit Air Skin"})
        self.assertEqual(verdict, discovery.MATCH_DIFFERENT)

    def test_structured_brand_fills_an_ellipsised_monitoring_name(self):
        identity = {
            "product_name": "The Ordinary Niacinamide 10% + Zinc 1%",
            "colour": None,
            "capacity": "30ml",
            "confirmed_title": "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
        }
        verdict = scheduler.quote_identity_verdict(
            identity,
            {"product_name": "Niacinamide 10% + Zinc 1%", "brand": "The Ordinary", "variant": "30ml"},
        )
        self.assertEqual(verdict, discovery.MATCH_SAME)

    def test_monitoring_still_requires_the_brand_without_structured_evidence(self):
        identity = {
            "product_name": "The Ordinary Niacinamide 10% + Zinc 1%",
            "colour": None,
            "capacity": "30ml",
            "confirmed_title": "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
        }
        verdict = scheduler.quote_identity_verdict(identity, {"product_name": "Niacinamide 10% + Zinc 1%"})
        self.assertEqual(verdict, discovery.MATCH_NEEDS_CONFIRMATION)

    def test_monitoring_name_brand_contradiction_is_different(self):
        identity = {
            "product_name": "Sony WH-1000XM5",
            "colour": None,
            "capacity": None,
            "confirmed_title": "Sony WH-1000XM5",
        }
        verdict = scheduler.quote_identity_verdict(
            identity, {"product_name": "Bose WH-1000XM5 Headphones", "brand": "Sony"}
        )
        self.assertEqual(verdict, discovery.MATCH_DIFFERENT)

    def test_monitoring_generic_name_words_are_not_a_competing_brand(self):
        """Monitoring mirrors discovery: a descriptive name omitting the brand still
        confirms, so a page confirmed at search time is not rejected on every check."""
        identity = {
            "product_name": "Sony WH-1000XM5",
            "colour": None,
            "capacity": None,
            "confirmed_title": "Sony WH-1000XM5",
        }
        verdict = scheduler.quote_identity_verdict(
            identity,
            {
                "product_name": "Wireless Noise Cancelling Headphones WH-1000XM5",
                "brand": "Sony",
            },
        )
        self.assertEqual(verdict, discovery.MATCH_SAME)

    def test_monitoring_name_capacity_contradiction_is_different(self):
        identity = {
            "product_name": "The Ordinary Niacinamide 10% + Zinc 1%",
            "colour": None,
            "capacity": "30ml",
            "confirmed_title": "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
        }
        verdict = scheduler.quote_identity_verdict(
            identity,
            {
                "product_name": "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
                "brand": "The Ordinary",
                "variant": "60ml",
            },
        )
        self.assertEqual(verdict, discovery.MATCH_DIFFERENT)

    def test_monitoring_variant_only_fills_an_undescribed_name(self):
        """Complementary, not contradictory: the name omits the size; the variant fills it."""
        identity = {
            "product_name": "Lumière Serum",
            "colour": None,
            "capacity": "30ml",
            "confirmed_title": "Lumière Serum 30ml",
        }
        verdict = scheduler.quote_identity_verdict(
            identity, {"product_name": "Lumière Serum", "variant": "30ml"}
        )
        self.assertEqual(verdict, discovery.MATCH_SAME)


if __name__ == "__main__":
    unittest.main()

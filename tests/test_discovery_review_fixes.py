"""Regressions for the three round-1 review items (discovery expansion).

Item 1 — the default (un-injected) path must actually use the web-search provider:
     with a key configured it searches; without one it reports "unavailable" and
     changes nothing for the built-in shops.
Item 2 — identity is re-decided from the detail page's own product name, so a card
     that pointed at a different model/colour/capacity cannot become a confirmed,
     monitorable offer, and a page that states nothing cannot be topped up from the
     card title.
Item 3 — URL dedupe drops only tracking parameters; same path with different
     variant/SKU parameters are different products and both stay visible.

Everything is offline: the Brave "sender" and every detail fetch are injected.
"""

import json
import os
import unittest
from unittest.mock import patch

import discovery
import search_providers.brave as brave
from retailers.base import Candidate, Retailer, RetailerError

GOOD_PAGE = json.dumps(
    {
        "web": {
            "results": [
                {
                    "url": "https://web.example.co.nz/fitbit-air-obsidian",
                    "title": "Fitbit Air (Obsidian)",
                    "description": "Snippet text that is never a price.",
                }
            ]
        }
    }
).encode("utf-8")


def sender_returning(body: bytes = GOOD_PAGE, status: int = 200):
    """A fake HTTP sender that records how often it was called."""
    calls: list[str] = []

    def send(url: str, headers: dict, timeout: float):
        calls.append(url)
        return status, body

    send.calls = calls  # type: ignore[attr-defined]
    return send


def quote(url, name="Fitbit Air (Obsidian)", price="239.00", currency="NZD", in_stock=True):
    return {
        "product_name": name,
        "url": url,
        "price": price,
        "currency": currency,
        "in_stock": in_stock,
        "fetched_at": "2026-09-24T00:00:00+00:00",
        "shipping_cost": None,
    }


class FakeRetailer(Retailer):
    name = "Fake Shop"
    domains = ("fake.example",)

    def __init__(self, name="Fake Shop", candidates=None, offers=None, error=None):
        self.name = name
        self._candidates = candidates or []
        self._offers = offers or {}
        self._error = error

    def search(self, query, *, limit=10):
        if self._error is not None:
            raise self._error
        return list(self._candidates)[:limit]

    def fetch_offer(self, url):
        value = self._offers.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RetailerError("no offer configured")
        return value


def cand(title, url, retailer="Fake Shop", **kw):
    return Candidate(retailer=retailer, url=url, title=title, sku=None, **kw)


def web(url, title):
    return {"url": url, "title": title, "source": "web_search", "snippet": "x", "queried_at": "t"}


#: The reviewer's exact counter-example scenario.
FITBIT_REQUEST = {"query": "Fitbit Air", "colour": "black", "currency": "NZD", "budget": "250.00"}


class TestDefaultWebProviderIsUsed(unittest.TestCase):
    """Item 1: the un-injected default must reach the real provider."""

    def _run(self, shop_candidates=None, shop_offers=None, *, include_web=True, request=None):
        shop = FakeRetailer(
            candidates=shop_candidates or [],
            offers=shop_offers or {},
        )
        # Default-path tests are about the Brave backend; Tavily is forced
        # unconfigured so a real TAVILY_API_KEY in .env can never leak a call.
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}):
            return discovery.search_and_compare(
                request or FITBIT_REQUEST,
                retailers=[shop],
                # web_candidates_fn deliberately NOT injected: this is the default path.
                web_offer_fn=lambda url: quote(url),
                include_web=include_web,
            )

    def test_configured_key_makes_the_default_path_call_the_provider(self):
        sender = sender_returning()
        shop_url = "https://fake.example/p/1"
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "BSA-fake-key"}), \
             patch.object(brave, "_default_sender", sender):
            result = self._run(
                shop_candidates=[cand("Fitbit Air (Obsidian)", shop_url)],
                shop_offers={shop_url: quote(shop_url)},
            )

        # The provider really ran — this is what used to be zero calls.
        self.assertEqual(len(sender.calls), 1)
        self.assertIn("q=Fitbit+Air", sender.calls[0])
        status = next(s for s in result["sources"] if s["retailer"] == discovery.WEB_SEARCH_SOURCE)
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["candidates"], 1)
        # ...and its candidate became a priced offer alongside the shop's.
        web_offers = [o for o in result["offers"] if o["source_kind"] == discovery.SOURCE_KIND_WEB]
        self.assertEqual(len(web_offers), 1)
        self.assertEqual(result["counts"]["offers"], 2)

    def test_no_key_means_no_call_and_the_shops_still_work(self):
        sender = sender_returning()
        shop_url = "https://fake.example/p/1"
        # Both pluggable backends are unconfigured here; .env is not consulted.
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "", "TAVILY_API_KEY": ""}), \
             patch.object(brave, "_default_sender", sender):
            result = self._run(
                shop_candidates=[cand("Fitbit Air (Obsidian)", shop_url)],
                shop_offers={shop_url: quote(shop_url)},
            )

        self.assertEqual(len(sender.calls), 0)
        status = next(s for s in result["sources"] if s["retailer"] == discovery.WEB_SEARCH_SOURCE)
        self.assertEqual(status["status"], "unavailable")
        self.assertIn("not configured", status["error"].lower())
        self.assertEqual(result["counts"]["offers"], 1)

    def test_include_web_false_never_touches_the_provider(self):
        sender = sender_returning()
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "BSA-fake-key"}), \
             patch.object(brave, "_default_sender", sender):
            result = self._run(include_web=False)
        self.assertEqual(len(sender.calls), 0)
        self.assertFalse(any(s["retailer"] == discovery.WEB_SEARCH_SOURCE for s in result["sources"]))

    def test_provider_failure_with_a_key_is_an_error_not_unavailable(self):
        sender = sender_returning(status=429)
        shop_url = "https://fake.example/p/1"
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "BSA-fake-key"}), \
             patch.object(brave, "_default_sender", sender):
            result = self._run(
                shop_candidates=[cand("Fitbit Air (Obsidian)", shop_url)],
                shop_offers={shop_url: quote(shop_url)},
            )
        status = next(s for s in result["sources"] if s["retailer"] == discovery.WEB_SEARCH_SOURCE)
        self.assertEqual(status["status"], "error")
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(result["counts"]["offers"], 1)  # the shop is unharmed

    def test_injected_provider_still_wins_over_the_default(self):
        """The injectable parameter keeps working for tests and offline runs."""
        sender = sender_returning()

        def injected(query):
            return [web("https://web.example.co.nz/x", "Fitbit Air (Obsidian)")]

        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "BSA-fake-key"}), \
             patch.object(brave, "_default_sender", sender):
            result = discovery.search_and_compare(
                FITBIT_REQUEST,
                retailers=[],
                web_candidates_fn=injected,
                web_offer_fn=lambda url: quote(url),
            )
        self.assertEqual(len(sender.calls), 0)
        self.assertEqual(result["counts"]["candidates"], 1)


class TestDetailIdentityDecides(unittest.TestCase):
    """Item 2: the detail page's own name decides the offer's verdict."""

    def _web_run(self, card_title, detail_name, request, price="239.00"):
        def web_fn(q):
            return [web("https://web.example.co.nz/p/1", card_title)]

        result = discovery.search_and_compare(
            request,
            retailers=[],
            web_candidates_fn=web_fn,
            web_offer_fn=lambda url: quote(url, name=detail_name, price=price),
        )
        return result

    def test_reviewer_repro_different_model_on_the_detail_page(self):
        """Card says "Fitbit Air", the page says "Fitbit Charge 6" — not a match.

        Before the fix this offer inherited the card's `same` and was selectable, so
        the user could have been alerted about a Fitbit Charge 6 at NZD 19.
        """
        request = {"query": "Fitbit Air", "currency": "NZD", "budget": "250.00"}
        result = self._web_run(
            "Fitbit Air", "Fitbit Charge 6 Fitness Tracker", request, price="19.00"
        )
        offer = result["offers"][0]
        self.assertEqual(offer["match"], discovery.MATCH_DIFFERENT)
        self.assertEqual(offer["card_match"], discovery.MATCH_SAME)  # the card's claim is kept for display
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)
        self.assertFalse(
            any(o["match"] == discovery.MATCH_SAME for o in result["offers"]),
            "no offer may be monitorable when the page names a different product",
        )

    def test_page_confirming_the_model_stays_selectable(self):
        result = self._web_run(
            "Fitbit Air (Obsidian)", "Fitbit Air (Obsidian)", FITBIT_REQUEST
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_SAME)
        self.assertEqual(discovery.select_offer(result, 1)["price"], "239.00")

    def test_page_without_a_name_is_needs_confirmation_never_topped_up(self):
        """A page that names nothing cannot borrow the card title's identity."""
        result = self._web_run("Fitbit Air (Obsidian)", "", FITBIT_REQUEST)
        offer = result["offers"][0]
        self.assertEqual(offer["match"], discovery.MATCH_NEEDS_CONFIRMATION)
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)

    def test_card_only_capacity_does_not_confirm_the_page(self):
        """Request and card say 30 ml, the page says only "Lumière Serum"."""
        request = {"query": "Lumière Serum", "capacity": "30ml", "currency": "NZD", "budget": "80.00"}
        result = self._web_run("Lumière Serum 30 ml", "Lumière Serum", request)
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_NEEDS_CONFIRMATION)

    def test_page_with_a_different_shade_is_rejected(self):
        request = {"query": "Velvet Lipstick Ruby", "colour": "red", "currency": "NZD", "budget": "40.00"}
        result = self._web_run(
            "Velvet Lipstick Ruby (classic red)", "Velvet Lipstick Rosewood", request
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_DIFFERENT)

    def test_page_selling_an_accessory_is_rejected(self):
        result = self._web_run(
            "Fitbit Air (Obsidian)", "Fitbit Air Active Band", FITBIT_REQUEST
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_DIFFERENT)

    def test_shop_adapter_detail_pages_are_re_verified_too(self):
        """The rule is not web-only: a shop page that names another model is rejected."""
        shop = FakeRetailer(
            candidates=[cand("Fitbit Air (Obsidian)", "https://fake.example/p/1")],
            offers={"https://fake.example/p/1": quote("https://fake.example/p/1", name="Fitbit Charge 6")},
        )
        result = discovery.search_and_compare(
            FITBIT_REQUEST, retailers=[shop], web_candidates_fn=lambda q: []
        )
        self.assertEqual(result["offers"][0]["match"], discovery.MATCH_DIFFERENT)
        with self.assertRaises(ValueError):
            discovery.select_offer(result, 1)

    def test_the_disagreement_is_reported_in_the_notes(self):
        result = self._web_run("Fitbit Air", "Fitbit Charge 6 Fitness Tracker", FITBIT_REQUEST)
        self.assertTrue(any("Fitbit Charge 6" in n for n in result["notes"]))


class TestUrlDedupeKeepsVariantParameters(unittest.TestCase):
    """Item 3: only tracking parameters may be dropped."""

    def test_normalise_url_drops_tracking_parameters(self):
        plain = discovery.normalise_product_url("https://shop.example.nz/p/1")
        tracked = discovery.normalise_product_url(
            "https://shop.example.nz/p/1?utm_source=news&utm_medium=email&gclid=abc"
        )
        self.assertEqual(plain, tracked)

    def test_normalise_url_is_order_insensitive_for_kept_parameters(self):
        first = discovery.normalise_product_url("https://shop.example.nz/p/1?sku=9&colour=black")
        second = discovery.normalise_product_url("https://shop.example.nz/p/1?colour=black&sku=9")
        self.assertEqual(first, second)

    def test_normalise_url_keeps_variant_and_unknown_parameters(self):
        thirty = discovery.normalise_product_url("https://shop.example.nz/serum?variant=30ml")
        sixty = discovery.normalise_product_url("https://shop.example.nz/serum?variant=60ml")
        self.assertNotEqual(thirty, sixty)
        unknown_a = discovery.normalise_product_url("https://shop.example.nz/p?ref=abc")
        unknown_b = discovery.normalise_product_url("https://shop.example.nz/p?ref=xyz")
        self.assertNotEqual(unknown_a, unknown_b)

    def test_normalise_url_still_matches_host_and_fragment_rules(self):
        self.assertEqual(
            discovery.normalise_product_url("https://www.shop.example.nz/p/1/"),
            discovery.normalise_product_url("https://shop.example.nz/p/1#reviews"),
        )

    def test_same_path_different_variants_are_both_shown(self):
        request = {"query": "Lumière Serum", "currency": "NZD", "budget": "80.00"}

        def web_fn(q):
            return [
                web("https://beauty.example.co.nz/serum?variant=30ml", "Lumière Serum 30 ml"),
                web("https://beauty.example.co.nz/serum?variant=60ml", "Lumière Serum 60 ml"),
            ]

        result = discovery.search_and_compare(
            request,
            retailers=[],
            web_candidates_fn=web_fn,
            web_offer_fn=lambda url: quote(url, name="Lumière Serum " + ("30 ml" if "30ml" in url else "60 ml")),
        )
        self.assertEqual(result["counts"]["candidates"], 2)
        self.assertEqual(result["counts"]["offers"], 2)
        self.assertEqual(
            {o["url"] for o in result["offers"]},
            {
                "https://beauty.example.co.nz/serum?variant=30ml",
                "https://beauty.example.co.nz/serum?variant=60ml",
            },
        )

    def test_tracking_only_difference_is_deduped_and_keeps_the_shop_attribution(self):
        shop_url = "https://fake.example/p/1"
        shop = FakeRetailer(
            candidates=[cand("Fitbit Air (Obsidian)", shop_url)],
            offers={shop_url: quote(shop_url)},
        )

        def web_fn(q):
            return [web(f"{shop_url}?utm_source=brave", "Fitbit Air (Obsidian)")]

        result = discovery.search_and_compare(
            FITBIT_REQUEST, retailers=[shop], web_candidates_fn=web_fn,
            web_offer_fn=lambda url: quote(url),
        )
        self.assertEqual(result["counts"]["candidates"], 1)
        self.assertEqual(result["candidates"][0]["source_kind"], discovery.SOURCE_KIND_RETAILER)
        self.assertEqual(result["candidates"][0]["also_found_by"], [discovery.WEB_SEARCH_SOURCE])


if __name__ == "__main__":
    unittest.main()

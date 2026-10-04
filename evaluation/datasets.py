"""Fixed, deterministic case sets for the offline Stage E evaluation.

Every case carries a ``provenance`` tag so a reader can tell what kind of evidence
it is:

``synthetic``
    Authored for this evaluation (the AeroBuds demo product, hand-written HTML
    edge cases, hand-authored expected verdicts). Proves the *rule* behaves, not
    any real-world accuracy.
``real_page_trace``
    Input text (product titles / handles / colours) recorded from real PB Tech and
    JB Hi-Fi captures documented in ``project_progress/HANDOFF.md`` (§5b, §15, §18,
    §19). The page itself is not re-fetched; the strings are copied from those
    recorded traces. Still not a live re-verification.
``existing_fixture``
    A file already in ``tests/fixtures/``. Those fixtures are themselves labelled
    "Fixture price for offline testing; not a live price" — they are trimmed or
    re-encoded captures, not live pages.

No case here invokes an LLM, Brave, a merchant site or SMTP. The expectations are
hand-authored from the project's documented rules; they are a conformance check of
the deterministic code, not independent ground truth.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

PROVENANCE_SYNTHETIC = "synthetic"
PROVENANCE_REAL_TRACE = "real_page_trace"
PROVENANCE_EXISTING_FIXTURE = "existing_fixture"

# ===========================================================================
# Track 1 — local instruction/schema validation (schemas.validate_search_request
#           and schemas.validate_task). This measures the *local guard*, not the
#           LLM's parsing quality.
# ===========================================================================


def _search(**over) -> dict:
    base = {
        "schema_version": "2",
        "status": "ready",
        "product_name": "Fitbit Air",
        "url": None,
        "budget": "200.00",
        "currency": "NZD",
        "colour": "black",
        "capacity": None,
        "condition": None,
        "comparison": "lte",
        "frequency_minutes": 60,
        "missing_fields": [],
        "clarification_question": None,
    }
    base.update(over)
    return base


def _task(**over) -> dict:
    base = {
        "schema_version": "1",
        "status": "ready",
        "product_name": "Kindle Paperwhite 16GB",
        "url": None,
        "target_price": "319.00",
        "currency": "NZD",
        "comparison": "lte",
        "frequency_minutes": 60,
        "condition": None,
        "missing_fields": [],
        "clarification_question": None,
    }
    base.update(over)
    return base


_TASK_URL = "https://www.pbtech.co.nz/product/WTHKIN0001"
_TASK_INSTRUCTION = f"monitor {_TASK_URL} below 319 NZD every 60 minutes"

#: ``expected`` for an accepted case: ``ok=True`` plus the normalised fields the
#: runner must verify. For a rejected case: ``ok=False``.
SCHEMA_CASES: list[dict] = [
    {
        "id": "sch_en_ready_nzd",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD 200 every 60 minutes",
        "data": _search(),
        "expected": {"ok": True, "status": "ready", "budget": "200.00", "missing_fields": []},
        "rationale": "Complete English request passes and keeps its normalised budget.",
    },
    {
        "id": "sch_zh_ready_nzd",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "帮我找 Fitbit Air 黑色 预算 200 纽币 每小时查一次",
        "data": _search(budget="200", colour="黑色", currency="NZD"),
        "expected": {"ok": True, "status": "ready", "budget": "200.00", "missing_fields": []},
        "rationale": "Chinese request passes; budget is normalised to two decimals.",
    },
    {
        "id": "sch_no_budget_clarifies",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black",
        "data": _search(
            budget=None, currency=None, status="needs_clarification",
            missing_fields=["budget"], clarification_question="What is your budget?",
        ),
        "expected": {"ok": True, "status": "needs_clarification", "missing_fields": ["budget"]},
        "rationale": "A search without a budget is still valid, but asks for one.",
    },
    {
        "id": "sch_budget_without_currency",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget 200",
        "data": _search(currency=None),
        "expected": {"ok": True, "status": "needs_clarification", "missing_fields": ["currency"]},
        "rationale": "A budget without a currency cannot be compared; currency is added to missing.",
    },
    {
        "id": "sch_invented_colour_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air budget NZD 200",
        "data": _search(colour="black"),
        "expected": {"ok": False},
        "rationale": "The model must not invent a colour the user never typed.",
    },
    {
        "id": "sch_invented_capacity_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD 200",
        "data": _search(capacity="128gb"),
        "expected": {"ok": False},
        "rationale": "The model must not invent a capacity either.",
    },
    {
        "id": "sch_unsupported_currency_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget EUR 200",
        "data": _search(currency="EUR"),
        "expected": {"ok": False},
        "rationale": "Only NZD/AUD/USD are supported.",
    },
    {
        "id": "sch_negative_budget_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD -5",
        "data": _search(budget="-5.00"),
        "expected": {"ok": False},
        "rationale": "A negative amount is not a valid decimal budget.",
    },
    {
        "id": "sch_three_decimal_budget_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD 200.999",
        "data": _search(budget="200.999"),
        "expected": {"ok": False},
        "rationale": "More than two decimal places is rejected, not rounded silently.",
    },
    {
        "id": "sch_frequency_too_low_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD 200 every 2 minutes",
        "data": _search(frequency_minutes=2),
        "expected": {"ok": False},
        "rationale": "Frequency below the 5-minute floor is rejected.",
    },
    {
        "id": "sch_frequency_bool_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD 200",
        "data": _search(frequency_minutes=True),
        "expected": {"ok": False},
        "rationale": "A bool is not a whole-minute integer.",
    },
    {
        "id": "sch_url_not_supplied_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD 200",
        "data": _search(url="https://www.pbtech.co.nz/product/WTHFIT114043"),
        "expected": {"ok": False},
        "rationale": "A URL the user never supplied may not be attached.",
    },
    {
        "id": "sch_url_supplied_accepted",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD 200 at https://www.pbtech.co.nz/product/WTHFIT114043",
        "data": _search(url="https://www.pbtech.co.nz/product/WTHFIT114043"),
        "expected": {"ok": True, "status": "ready", "budget": "200.00", "missing_fields": []},
        "rationale": "A public URL present in the instruction is accepted.",
    },
    {
        "id": "sch_extra_field_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD 200",
        "data": {**_search(), "extra": "nope"},
        "expected": {"ok": False},
        "rationale": "The field set is strict; unknown fields are rejected.",
    },
    {
        "id": "sch_bad_status_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air black budget NZD 200",
        "data": _search(status="maybe"),
        "expected": {"ok": False},
        "rationale": "Status must be ready / needs_clarification / unsupported.",
    },
    {
        "id": "sch_unsupported_without_explanation_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "book me a flight",
        "data": _search(status="unsupported", clarification_question=None),
        "expected": {"ok": False},
        "rationale": "An unsupported request must explain itself.",
    },
    {
        "id": "sch_unsupported_with_explanation_accepted",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "book me a flight",
        "data": _search(
            status="unsupported", product_name=None, budget=None, currency=None,
            colour=None, comparison=None, frequency_minutes=None,
            clarification_question="This assistant only monitors product prices.",
        ),
        "expected": {"ok": True, "status": "unsupported", "missing_fields": []},
        "rationale": "An explained unsupported request is accepted as unsupported.",
    },
    {
        "id": "sch_overlong_colour_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find Fitbit Air " + "z" * 41 + " budget NZD 200",
        "data": _search(colour="z" * 41),
        "expected": {"ok": False},
        "rationale": "A 41-character constraint is rejected as too long to search.",
    },
    {
        "id": "sch_overlong_product_name_rejected",
        "validator": "search",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find " + "x" * 121 + " budget NZD 200",
        "data": _search(product_name="x" * 121),
        "expected": {"ok": False},
        "rationale": "A product name longer than the 120-character query cap is rejected.",
    },
    {
        "id": "sch_task_ready_url",
        "validator": "task",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": _TASK_INSTRUCTION,
        "data": _task(url=_TASK_URL),
        "expected": {"ok": True, "status": "ready", "missing_fields": []},
        "rationale": "schema v1 monitor task with a supplied URL and target price passes.",
    },
    {
        "id": "sch_task_missing_price_clarifies",
        "validator": "task",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": _TASK_INSTRUCTION,
        "data": _task(url=_TASK_URL, target_price=None),
        "expected": {"ok": True, "status": "needs_clarification", "missing_fields": ["target_price"]},
        "rationale": "A task needs a target price; the missing field is recorded.",
    },
    {
        "id": "sch_task_price_not_string_rejected",
        "validator": "task",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": _TASK_INSTRUCTION,
        "data": _task(url=_TASK_URL, target_price=319),
        "expected": {"ok": False},
        "rationale": "Amounts are decimal strings, never floats/ints.",
    },
    {
        "id": "sch_task_bad_comparison_rejected",
        "validator": "task",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": _TASK_INSTRUCTION,
        "data": _task(url=_TASK_URL, comparison="gt"),
        "expected": {"ok": False},
        "rationale": "Only lt/lte are supported comparators.",
    },
]

# ---------------------------------------------------------------------------
# Track 1b — the offline *demo* lexical parser feeding the same validator.
# Clearly NOT the real LLM: it is a deterministic phrase parser used only by
# demo mode. Its numbers must never be reported as LLM accuracy.
# ---------------------------------------------------------------------------

DEMO_PARSER_CASES: list[dict] = [
    {
        "id": "demo_en_ready",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find AeroBuds Pro black budget NZD 200 every 60 minutes",
        "expected": {
            "status": "ready", "product_name": "AeroBuds Pro", "colour": "black",
            "budget": "200.00", "currency": "NZD", "frequency_minutes": 60,
        },
    },
    {
        "id": "demo_zh_ready",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "帮我找 Fitbit Air 黑色 预算 200 纽币 每小时查一次",
        "expected": {
            "status": "ready", "product_name": "Fitbit Air", "colour": "黑色",
            "budget": "200.00", "currency": "NZD", "frequency_minutes": 60,
        },
    },
    {
        "id": "demo_no_budget_clarifies",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "find AeroBuds Pro",
        "expected": {"status": "needs_clarification", "missing_fields": ["budget"]},
    },
    {
        "id": "demo_empty_clarifies",
        "provenance": PROVENANCE_SYNTHETIC,
        "instruction": "   ",
        "expected": {"status": "needs_confirmation_placeholder"},  # replaced below
    },
]
# The empty-instruction case clarifies on the product name.
DEMO_PARSER_CASES[-1]["expected"] = {
    "status": "needs_clarification", "missing_fields": ["product_name"],
}


# ===========================================================================
# Track 2 — product identity: same / different / needs_confirmation
# ===========================================================================

#: Positive-class confusion matrix is computed on "same"; the headline safety
#: metric is how many *wrong* listings were accepted as same.
IDENTITY_REQUEST_FITBIT = {
    "product_name": "Fitbit Air", "query": "Fitbit Air", "colour": "black",
    "capacity": None, "condition": None,
}
IDENTITY_REQUEST_AERO = {
    "product_name": "AeroBuds Pro", "query": "AeroBuds Pro", "colour": "black",
    "capacity": None, "condition": None,
}


def _c(title: str, **over) -> dict:
    base = {
        "retailer": "Eval Shop", "url": "https://shop.example/p/1", "title": title,
        "sku": None, "model": None, "variant": None, "brand": None, "condition": None,
    }
    base.update(over)
    return base


IDENTITY_CASES: list[dict] = [
    # ---- candidate level (search-card title) --------------------------------
    {
        "id": "id_card_obsidian_same", "level": "candidate", "provenance": PROVENANCE_REAL_TRACE,
        "request": IDENTITY_REQUEST_FITBIT,
        "candidate": _c("Google Fitbit Air Fitness Tracker - Obsidian"),
        "expected": "same", "rationale": "Real PB Tech card title; Obsidian aliases black.",
    },
    {
        "id": "id_card_lavender_different", "level": "candidate", "provenance": PROVENANCE_REAL_TRACE,
        "request": IDENTITY_REQUEST_FITBIT,
        "candidate": _c("Google Fitbit Air Fitness Tracker - Lavender"),
        "expected": "different", "rationale": "Real card; stated colour conflict.",
    },
    {
        "id": "id_card_inspire3_different", "level": "candidate", "provenance": PROVENANCE_REAL_TRACE,
        "request": IDENTITY_REQUEST_FITBIT,
        "candidate": _c("Fitbit Inspire 3 Fitness Tracker - Midnight Zen Black"),
        "expected": "different", "rationale": "Real card; wrong model, name lacks 'air'.",
    },
    {
        "id": "id_card_charge6_different", "level": "candidate", "provenance": PROVENANCE_REAL_TRACE,
        "request": IDENTITY_REQUEST_FITBIT,
        "candidate": _c("Fitbit Charge 6 Fitness Tracker - Black"),
        "expected": "different", "rationale": "Real card; wrong model despite matching colour.",
    },
    {
        "id": "id_card_berry_needs_confirmation", "level": "candidate", "provenance": PROVENANCE_REAL_TRACE,
        "request": IDENTITY_REQUEST_FITBIT,
        "candidate": _c("Google Fitbit Air Fitness Tracker - Berry"),
        "expected": "needs_confirmation", "rationale": "Real card; unknown colour word, cannot confirm black.",
    },
    {
        "id": "id_card_band_different", "level": "candidate", "provenance": PROVENANCE_REAL_TRACE,
        "request": IDENTITY_REQUEST_FITBIT,
        "candidate": _c("Google Fitbit Air Active Band"),
        "expected": "different", "rationale": "Real card; accessory, not the product.",
    },
    {
        "id": "id_card_cable_different", "level": "candidate", "provenance": PROVENANCE_REAL_TRACE,
        "request": IDENTITY_REQUEST_FITBIT,
        "candidate": _c("Google Fitbit Air Charging Cable Snow"),
        "expected": "different", "rationale": "Real card; accessory (cable/charging).",
    },
    {
        "id": "id_card_aerobuds_black_same", "level": "candidate", "provenance": PROVENANCE_SYNTHETIC,
        "request": IDENTITY_REQUEST_AERO, "candidate": _c("AeroBuds Pro Wireless Earbuds - Black"),
        "expected": "same", "rationale": "Demo card matching model and colour.",
    },
    {
        "id": "id_card_aerobuds_one_different", "level": "candidate", "provenance": PROVENANCE_SYNTHETIC,
        "request": IDENTITY_REQUEST_AERO, "candidate": _c("AeroBuds One Earbuds"),
        "expected": "different", "rationale": "Demo card missing 'pro'.",
    },
    {
        "id": "id_card_aerobuds_lavender_different", "level": "candidate", "provenance": PROVENANCE_SYNTHETIC,
        "request": IDENTITY_REQUEST_AERO, "candidate": _c("AeroBuds Pro Wireless Earbuds - Lavender"),
        "expected": "different", "rationale": "Demo card with a conflicting colour.",
    },
    {
        "id": "id_card_aerobuds_pro2_needs_confirmation", "level": "candidate", "provenance": PROVENANCE_SYNTHETIC,
        "request": IDENTITY_REQUEST_AERO, "candidate": _c("AeroBuds Pro 2 Earbuds - Black"),
        "expected": "same",
        "rationale": "Listing states a generation the user did not constrain: accepted as identifying evidence (Fitbit Air 2026 is the only Fitbit Air). A user-named generation still conflicts.",
    },
    {
        "id": "id_card_aerobuds_cover_different", "level": "candidate", "provenance": PROVENANCE_SYNTHETIC,
        "request": IDENTITY_REQUEST_AERO, "candidate": _c("AeroBuds Case Cover"),
        "expected": "different", "rationale": "Demo accessory (case/cover).",
    },
    {
        "id": "id_card_no_colour_needs_confirmation", "level": "candidate", "provenance": PROVENANCE_SYNTHETIC,
        "request": IDENTITY_REQUEST_AERO, "candidate": _c("AeroBuds Pro Wireless Earbuds"),
        "expected": "needs_confirmation", "rationale": "Colour requested but not stated.",
    },
    {
        "id": "id_card_condition_conflict_different", "level": "candidate", "provenance": PROVENANCE_SYNTHETIC,
        "request": {**IDENTITY_REQUEST_AERO, "condition": "new"},
        "candidate": _c("AeroBuds Pro Wireless Earbuds - Black", condition="used"),
        "expected": "different", "rationale": "Stated condition conflicts with the request.",
    },
    {
        "id": "id_card_condition_unstated_needs_confirmation", "level": "candidate", "provenance": PROVENANCE_SYNTHETIC,
        "request": {**IDENTITY_REQUEST_AERO, "condition": "new"},
        "candidate": _c("AeroBuds Pro Wireless Earbuds - Black", condition=None),
        "expected": "needs_confirmation", "rationale": "Condition requested but not stated.",
    },
    # ---- detail-page level (identity re-decided from the page) --------------
    {
        "id": "id_detail_generic_name_same", "level": "detail", "provenance": PROVENANCE_REAL_TRACE,
        "request": {"product_name": "Sony WH-1000XM5", "query": "Sony WH-1000XM5",
                    "colour": "black", "capacity": None, "condition": None},
        "entry": {"retailer": "Shop", "url": "https://shop.example/p", "condition": None},
        "quote": {"product_name": "Wireless Noise Cancelling Headphones WH-1000XM5",
                  "brand": "Sony", "variant": "Black"},
        "expected": "same",
        "rationale": "Real §20b regression: name omits brand, structured brand fills it.",
    },
    {
        "id": "id_detail_brand_conflict_different", "level": "detail", "provenance": PROVENANCE_REAL_TRACE,
        "request": {"product_name": "Sony WH-1000XM5", "query": "Sony WH-1000XM5",
                    "colour": None, "capacity": None, "condition": None},
        "entry": {"retailer": "Shop", "url": "https://shop.example/p", "condition": None},
        "quote": {"product_name": "Bose WH-1000XM5 Headphones", "brand": "Sony", "variant": None},
        "expected": "different",
        "rationale": "Real §20b regression: name says Bose, structured brand says Sony.",
    },
    {
        "id": "id_detail_wrong_model_different", "level": "detail", "provenance": PROVENANCE_REAL_TRACE,
        "request": IDENTITY_REQUEST_FITBIT,
        "entry": {"retailer": "Shop", "url": "https://shop.example/p", "condition": None},
        "quote": {"product_name": "Google Fitbit Charge 6", "brand": "Google", "variant": None},
        "expected": "different",
        "rationale": "Detail page names a different model than the card that led to it.",
    },
    {
        "id": "id_detail_no_name_needs_confirmation", "level": "detail", "provenance": PROVENANCE_SYNTHETIC,
        "request": IDENTITY_REQUEST_FITBIT,
        "entry": {"retailer": "Shop", "url": "https://shop.example/p", "condition": None},
        "quote": {"product_name": "", "brand": None, "variant": None},
        "expected": "needs_confirmation", "rationale": "A page naming nothing cannot be confirmed.",
    },
    {
        "id": "id_detail_ordinary_30ml_same", "level": "detail", "provenance": PROVENANCE_REAL_TRACE,
        "request": {"product_name": "The Ordinary Niacinamide 10% + Zinc 1%",
                    "query": "The Ordinary Niacinamide 10% + Zinc 1%",
                    "colour": None, "capacity": "30ml", "condition": None},
        "entry": {"retailer": "Shop", "url": "https://shop.example/p", "condition": None},
        "quote": {"product_name": "Niacinamide 10% + Zinc 1%", "brand": "The Ordinary",
                  "variant": "30ml"},
        "expected": "same",
        "rationale": "Real §18/§19: structured brand+size complement a name that omits them.",
    },
    {
        "id": "id_detail_ordinary_self_conflict_different", "level": "detail", "provenance": PROVENANCE_SYNTHETIC,
        "request": {"product_name": "The Ordinary Niacinamide 10% + Zinc 1%",
                    "query": "The Ordinary Niacinamide 10% + Zinc 1%",
                    "colour": None, "capacity": "30ml", "condition": None},
        "entry": {"retailer": "Shop", "url": "https://shop.example/p", "condition": None},
        "quote": {"product_name": "Niacinamide 30ml", "brand": "The Ordinary", "variant": "60ml"},
        "expected": "different",
        "rationale": "Page contradicts itself: name says 30ml, variant says 60ml.",
    },
    {
        "id": "id_detail_ordinary_wrong_capacity_different", "level": "detail", "provenance": PROVENANCE_SYNTHETIC,
        "request": {"product_name": "The Ordinary Niacinamide 10% + Zinc 1%",
                    "query": "The Ordinary Niacinamide 10% + Zinc 1%",
                    "colour": None, "capacity": "30ml", "condition": None},
        "entry": {"retailer": "Shop", "url": "https://shop.example/p", "condition": None},
        "quote": {"product_name": "Niacinamide 60ml", "brand": "The Ordinary", "variant": "60ml"},
        "expected": "different", "rationale": "Stated 60ml capacity conflicts with requested 30ml.",
    },
    # ---- monitoring level (quote classified against the stored identity) ----
    {
        "id": "id_mon_obsidian_same", "level": "monitor", "provenance": PROVENANCE_REAL_TRACE,
        "identity": {"product_name": "Fitbit Air", "colour": "black", "capacity": None,
                     "confirmed_title": "Google Fitbit Air Fitness Tracker - Obsidian"},
        "quote": {"product_name": "Google Fitbit Air (Obsidian)"},
        "expected": "same", "rationale": "Real confirmed identity vs real-style page name.",
    },
    {
        "id": "id_mon_lavender_different", "level": "monitor", "provenance": PROVENANCE_REAL_TRACE,
        "identity": {"product_name": "Fitbit Air", "colour": "black", "capacity": None,
                     "confirmed_title": "Google Fitbit Air Fitness Tracker - Obsidian"},
        "quote": {"product_name": "Google Fitbit Air Lavender"},
        "expected": "different", "rationale": "Known conflicting colour.",
    },
    {
        "id": "id_mon_berry_needs_confirmation", "level": "monitor", "provenance": PROVENANCE_REAL_TRACE,
        "identity": {"product_name": "Fitbit Air", "colour": "black", "capacity": None,
                     "confirmed_title": "Google Fitbit Air Fitness Tracker - Obsidian"},
        "quote": {"product_name": "Google Fitbit Air (Berry)"},
        "expected": "needs_confirmation", "rationale": "Unknown colour: not proof of a mismatch.",
    },
    {
        "id": "id_mon_band_different", "level": "monitor", "provenance": PROVENANCE_REAL_TRACE,
        "identity": {"product_name": "Fitbit Air", "colour": "black", "capacity": None,
                     "confirmed_title": "Google Fitbit Air Fitness Tracker - Obsidian"},
        "quote": {"product_name": "Google Fitbit Air Active Band"},
        "expected": "different", "rationale": "Accessory page must never trigger or end a deal.",
    },
    {
        "id": "id_mon_marketing_name_same", "level": "monitor", "provenance": PROVENANCE_REAL_TRACE,
        "identity": {"product_name": "Fitbit Air", "colour": "black", "capacity": None,
                     "confirmed_title": "Google Fitbit Air Fitness Tracker - Obsidian"},
        "quote": {"product_name": (
            "Google Fitbit Air Fitness Tracker - Obsidian - Up to 7 Day Battery Life "
            "- 5ATM Water Resistance - 24/7 Health Tracking")},
        "expected": "same",
        "rationale": "Real long marketing name; 'battery life' is a feature, not an accessory.",
    },
    {
        "id": "id_mon_charge6_needs_confirmation", "level": "monitor", "provenance": PROVENANCE_REAL_TRACE,
        "identity": {"product_name": "Fitbit Air", "colour": "black", "capacity": None,
                     "confirmed_title": "Google Fitbit Air Fitness Tracker - Obsidian"},
        "quote": {"product_name": "Google Fitbit Charge 6 Black"},
        "expected": "needs_confirmation",
        "rationale": "Model-completeness: one shared word ('fitbit') is not a match.",
    },
    {
        "id": "id_mon_generic_needs_confirmation", "level": "monitor", "provenance": PROVENANCE_SYNTHETIC,
        "identity": {"product_name": "Fitbit Air", "colour": "black", "capacity": None,
                     "confirmed_title": "Google Fitbit Air Fitness Tracker - Obsidian"},
        "quote": {"product_name": "Fitness Tracker"},
        "expected": "needs_confirmation", "rationale": "Missing all model words.",
    },
    {
        "id": "id_mon_legacy_none_same", "level": "monitor", "provenance": PROVENANCE_SYNTHETIC,
        "identity": None, "quote": {"product_name": "Anything At All"},
        "expected": "same", "rationale": "Legacy single-URL task has no identity gate.",
    },
]


# ===========================================================================
# Track 3 — static product-page quote extraction
# ===========================================================================


def _jsonld(script_json: str) -> bytes:
    html = (
        '<!DOCTYPE html><html><head><title>t</title>'
        f'<script type="application/ld+json">{script_json}</script>'
        "</head><body></body></html>"
    )
    return html.encode("utf-8")


def _offer_page(*, name, price, currency, availability, extra_product=""):
    avail = f',"availability":"{availability}"' if availability is not None else ""
    price_part = f'"price":"{price}"' if price is not None else ""
    currency_part = f',"priceCurrency":"{currency}"' if currency is not None else ""
    offer = "{" + ",".join(p for p in (price_part + currency_part + avail,) if p) + "}"
    script = (
        '{"@context":"https://schema.org","@type":"Product",'
        f'"name":"{name}",{extra_product}"offers":{offer}}}'
    )
    return _jsonld(script)


def _fixture(name: str) -> bytes:
    import json

    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return data["html"].encode("utf-8")


QUOTE_CASES: list[dict] = [
    {
        "id": "quote_fixture_kindle", "provenance": PROVENANCE_EXISTING_FIXTURE,
        "source": "tests/fixtures/pbtech_kindle.json",
        "html": _fixture("pbtech_kindle.json"),
        "url": "https://www.pbtech.co.nz/product/WTHKIN0001/kindle-paperwhite",
        "expected": {"price": "319.00", "currency": "NZD", "in_stock": True},
    },
    {
        "id": "quote_fixture_samsung", "provenance": PROVENANCE_EXISTING_FIXTURE,
        "source": "tests/fixtures/pbtech_samsung.json",
        "html": _fixture("pbtech_samsung.json"),
        "url": "https://www.pbtech.co.nz/product/WTHSAM0001/galaxy-a17",
        "expected": {"price": "433.00", "currency": "NZD", "in_stock": True},
    },
    {
        "id": "quote_fixture_3dprinter", "provenance": PROVENANCE_EXISTING_FIXTURE,
        "source": "tests/fixtures/pbtech_3dprinter.json",
        "html": _fixture("pbtech_3dprinter.json"),
        "url": "https://www.pbtech.co.nz/product/WTH3DP0001/k1-max",
        "expected": {"price": "1399.00", "currency": "NZD", "in_stock": True},
    },
    {
        "id": "quote_fixture_outofstock", "provenance": PROVENANCE_EXISTING_FIXTURE,
        "source": "tests/fixtures/pbtech_outofstock.json",
        "html": _fixture("pbtech_outofstock.json"),
        "url": "https://www.pbtech.co.nz/product/WTHIPH0001/iphone-16-pro",
        "expected": {"price": "2999.00", "currency": "NZD", "in_stock": False},
    },
    {
        "id": "quote_jsonld_array_single", "provenance": PROVENANCE_SYNTHETIC,
        "html": _jsonld(
            '[{"@context":"https://schema.org","@type":"Product","name":"Single Item",'
            '"offers":{"price":"10.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}]'
        ),
        "url": "https://shop.example/p/single",
        "expected": {"price": "10.00", "currency": "NZD", "in_stock": True},
    },
    {
        "id": "quote_brand_object_and_variant", "provenance": PROVENANCE_SYNTHETIC,
        "html": _offer_page(
            name="Sony WH-1000XM5", price="465.00", currency="NZD",
            availability="https://schema.org/InStock",
            extra_product='"brand":{"@type":"Brand","name":"Sony"},"size":"Black",',
        ),
        "url": "https://shop.example/p/sony",
        "expected": {"price": "465.00", "currency": "NZD", "in_stock": True},
    },
    {
        "id": "quote_price_with_comma", "provenance": PROVENANCE_SYNTHETIC,
        "html": _offer_page(name="Laptop", price="1,299.00", currency="NZD",
                            availability="https://schema.org/InStock"),
        "url": "https://shop.example/p/laptop",
        "expected": {"price": "1299.00", "currency": "NZD", "in_stock": True},
    },
    {
        "id": "quote_unknown_stock_is_none", "provenance": PROVENANCE_SYNTHETIC,
        "html": _offer_page(name="Mystery Item", price="55.00", currency="NZD", availability=None),
        "url": "https://shop.example/p/mystery",
        "expected": {"price": "55.00", "currency": "NZD", "in_stock": None},
    },
    {
        "id": "quote_preorder_is_not_buyable", "provenance": PROVENANCE_SYNTHETIC,
        "html": _offer_page(name="Preorder Item", price="99.00", currency="NZD",
                            availability="https://schema.org/PreOrder"),
        "url": "https://shop.example/p/preorder",
        "expected": {"price": "99.00", "currency": "NZD", "in_stock": False},
    },
    {
        "id": "quote_equivalent_offers_pick_one", "provenance": PROVENANCE_SYNTHETIC,
        "html": _jsonld(
            '{"@context":"https://schema.org","@type":"Product","name":"Duplicated Offer",'
            '"offers":[{"price":"20.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"},'
            '{"price":"20.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}]}'
        ),
        "url": "https://shop.example/p/dup",
        "expected": {"price": "20.00", "currency": "NZD", "in_stock": True},
    },
    {
        "id": "quote_opengraph_fallback", "provenance": PROVENANCE_SYNTHETIC,
        "html": (
            '<!DOCTYPE html><html><head><title>OG Item</title>'
            '<meta property="og:title" content="OG Item">'
            '<meta property="og:price:amount" content="42.00">'
            '<meta property="og:price:currency" content="NZD">'
            "</head><body></body></html>"
        ).encode("utf-8"),
        "url": "https://shop.example/p/og",
        "expected": {"price": "42.00", "currency": "NZD", "in_stock": None},
    },
    # ---- cases where extraction must REFUSE rather than guess ----------------
    {
        "id": "quote_refuse_ambiguous_products", "provenance": PROVENANCE_SYNTHETIC,
        "html": _jsonld(
            '{"@context":"https://schema.org","@type":"Product","name":"Alpha Thing",'
            '"offers":{"price":"10.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}'
        ) + _jsonld(
            '{"@context":"https://schema.org","@type":"Product","name":"Beta Thing",'
            '"offers":{"price":"99.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}}'
        ),
        "url": "https://shop.example/p/ambiguous",
        "expected": {"refuse": True},
    },
    {
        "id": "quote_refuse_missing_price", "provenance": PROVENANCE_SYNTHETIC,
        "html": _jsonld(
            '{"@context":"https://schema.org","@type":"Product","name":"No Price Item",'
            '"offers":{"priceCurrency":"NZD","availability":"https://schema.org/InStock"}}'
        ),
        "url": "https://shop.example/p/noprice",
        "expected": {"refuse": True},
    },
    {
        "id": "quote_refuse_missing_currency", "provenance": PROVENANCE_SYNTHETIC,
        "html": _jsonld(
            '{"@context":"https://schema.org","@type":"Product","name":"No Currency Item",'
            '"offers":{"price":"12.00","availability":"https://schema.org/InStock"}}'
        ),
        "url": "https://shop.example/p/nocur",
        "expected": {"refuse": True},
    },
    {
        "id": "quote_refuse_conflicting_offers", "provenance": PROVENANCE_SYNTHETIC,
        "html": _jsonld(
            '{"@context":"https://schema.org","@type":"Product","name":"Two Prices",'
            '"offers":[{"price":"10.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"},'
            '{"price":"12.00","priceCurrency":"NZD","availability":"https://schema.org/InStock"}]}'
        ),
        "url": "https://shop.example/p/conflict",
        "expected": {"refuse": True},
    },
    {
        "id": "quote_refuse_empty_page", "provenance": PROVENANCE_SYNTHETIC,
        "html": b"<!DOCTYPE html><html><head><title>Nothing</title></head><body></body></html>",
        "url": "https://shop.example/p/empty",
        "expected": {"refuse": True},
    },
]


# ===========================================================================
# Track 4 — two-source deal notification: dedup + failure isolation
# ===========================================================================
#: Implementations live in runner.py; these specs declare the expected outcome.
NOTIFICATION_SCENARIOS: list[dict] = [
    {
        "id": "notif_both_deal_one_email", "provenance": PROVENANCE_SYNTHETIC,
        "description": "Two sources both below target: exactly one email across two checks.",
        "expected": {"send_calls": 1, "check1_is_deal": True, "check2_is_deal": True,
                     "check2_notified": False, "sources_failed": 0},
    },
    {
        "id": "notif_equal_price_one_email", "provenance": PROVENANCE_SYNTHETIC,
        "description": "Two sources at the same price: still exactly one email.",
        "expected": {"send_calls": 1, "is_deal": True},
    },
    {
        "id": "notif_source_failure_isolated", "provenance": PROVENANCE_SYNTHETIC,
        "tags": ["failure_isolation"],  # a source actually raises here
        "description": "One source raises, the other deals: the check still succeeds once.",
        "expected": {"send_calls": 1, "is_deal": True, "sources_failed": 1, "sources_checked": 2},
    },
    {
        "id": "notif_failure_keeps_episode_open", "provenance": PROVENANCE_SYNTHETIC,
        "tags": ["failure_isolation"],  # a source actually raises here
        "description": "A source failing is not evidence the deal ended: no reset, no re-send.",
        "expected": {"send_calls": 1, "event_reset": False, "event_reset_skipped": True},
    },
    {
        "id": "notif_out_of_stock_not_chosen", "provenance": PROVENANCE_SYNTHETIC,
        "description": "A cheaper out-of-stock source must not be the chosen best offer.",
        "expected": {"send_calls": 1, "chosen_price": "245.00"},
    },
    {
        "id": "notif_all_above_target_resets", "provenance": PROVENANCE_SYNTHETIC,
        "description": "Both sources comparable and above target ends the episode without a new email.",
        "expected": {"send_calls": 1, "event_reset": True, "second_is_deal": False},
    },
    {
        "id": "notif_accessory_page_ignored", "provenance": PROVENANCE_SYNTHETIC,
        "description": "A cheap accessory page must not become the chosen deal.",
        "expected": {"send_calls": 1, "chosen_price": "239.00"},
    },
    {
        "id": "notif_send_retry_then_success", "provenance": PROVENANCE_SYNTHETIC,
        "tags": ["failure_isolation"],  # injected retryable send failure
        "description": "A retryable send failure is retried on the next check and then delivered once.",
        "expected": {"send_calls": 2, "first_notified": False, "second_notified": True,
                     "success_sends": 1},
    },
    {
        "id": "notif_permanent_send_failure", "provenance": PROVENANCE_SYNTHETIC,
        "tags": ["failure_isolation"],  # injected permanent send failure
        "description": "A permanent send failure is recorded once and not retried, without crashing.",
        "expected": {"send_calls": 1, "notified": False, "reason": "permanent_error"},
    },
]

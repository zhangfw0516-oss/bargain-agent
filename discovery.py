"""Product discovery: candidate aggregation, identity matching and comparison.

Stage B. Deterministic code decides *which candidates are the same product* and how
offers compare; nothing here asks a model for a verdict. A model may help phrase a
clarification question elsewhere, but it cannot override an identity conflict found
from the data.

Design rules:

- A missing value stays missing. An unknown colour/stock/shipping cost is reported as
  unknown, never treated as a match or as zero.
- Comparing different specs is worse than reporting nothing, so "cannot confirm" is a
  first-class outcome rather than a silent pass.
- One retailer failing must not hide the others; each source reports its own status.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit

from retailers import Candidate
from retailers import all_retailers
from retailers import enabled_retailers as default_enabled_retailers
from retailers.base import Retailer

logger = logging.getLogger(__name__)

#: Source labels for the discovery expansion (BROAD_PRODUCT_DISCOVERY.md).
WEB_SEARCH_SOURCE = "Brave Web Search"
SOURCE_KIND_RETAILER = "retailer_adapter"
SOURCE_KIND_WEB = "web_search"

#: Detail-fetch budget for one search run: at most this many detail pages in total,
#: and no more than WEB_DETAIL_CAP from web-search candidates / RETAILER_DETAIL_CAP
#: from the built-in shops. Unused quota of one class may flow to the other class,
#: but the total never grows.
DETAIL_CAP = 6
WEB_DETAIL_CAP = 3
RETAILER_DETAIL_CAP = 3


#: Query parameters that only identify *how a visitor arrived*, never *what is for
#: sale*. Dropping the whole query string would merge genuinely different listings:
#: `?variant=30ml` and `?variant=60ml` are two products, and `?sku=...` distinguishes
#: two listings on the same path. Only the keys below may be discarded; anything
#: unrecognised is kept, because a missed dedupe is a duplicate row while a wrong
#: dedupe is a product the user never gets to see.
TRACKING_PARAM_PREFIXES = ("utm_", "hsa_", "pk_")
TRACKING_PARAMS = frozenset(
    """
    gclid gclsrc dclid wbraid gbraid srsltid msclkid yclid twclid fbclid igshid
    mc_cid mc_eid _ga _gl _hsenc _hsmi vero_id oly_enc_id oly_anon_id
    spm scm epik s_kwcid ef_id li_fat_id mkt_tok trk
    """.split()
)


def _is_tracking_param(name: str) -> bool:
    lowered = (name or "").lower()
    if lowered.startswith(TRACKING_PARAM_PREFIXES):
        return True
    return lowered in TRACKING_PARAMS


def normalise_product_url(url: str) -> str:
    """Canonical form used to dedupe candidates across sources.

    Lowercased, www-stripped host + path, plus the query parameters that can identify
    a product, variant or listing — with tracking parameters removed and parameter
    order made irrelevant. Two entries with the same canonical form are the same
    product page found by different sources; entries that differ in any meaningful
    parameter stay separate and are both shown.
    """
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or ""
    while path.endswith("/") and len(path) > 1:
        path = path[:-1]
    if not host:
        return raw
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        pairs = []
    kept = sorted((k, v) for k, v in pairs if not _is_tracking_param(k))
    key = f"{host}{path}"
    if kept:
        key = f"{key}?{urlencode(kept)}"
    return key

MATCH_SAME = "same"
MATCH_DIFFERENT = "different"
MATCH_NEEDS_CONFIRMATION = "needs_confirmation"

#: Words describing the request rather than the product model.
STOPWORDS = frozenset(
    """
    a an the and or for with without in on of to my me i we want need please
    find search searching look looking buy cheapest cheaper cheap price priced
    under below over within budget max maximum notify notification alert remind
    reminder when stock available new used any if is it that this these those
    nzd aud usd dollars dollar nz$ from at by
    """.split()
)

COLOURS = frozenset(
    """
    black white blue red green pink purple silver grey gray gold teal orange
    yellow rose lavender navy mint beige brown
    """.split()
)

#: Marketing colour names that shops use instead of plain colours, and Chinese
#: words a user may type. Without this, "black" would not match a listing that says
#: "Obsidian" and a perfectly good match would be rejected.
COLOUR_ALIASES = {
    "obsidian": "black",
    "midnight": "black",
    "onyx": "black",
    "charcoal": "grey",
    "graphite": "grey",
    "space grey": "grey",
    "space gray": "grey",
    "titanium": "grey",
    "starlight": "white",
    "frost": "white",
    "pearl": "white",
    "cream": "white",
    "navy": "blue",
    "sky": "blue",
    "royal": "blue",
    "rose": "pink",
    "blush": "pink",
    "sand": "beige",
}

CHINESE_COLOURS = {
    "黑": "black",
    "白": "white",
    "蓝": "blue",
    "藍": "blue",
    "红": "red",
    "紅": "red",
    "绿": "green",
    "綠": "green",
    "粉": "pink",
    "银": "silver",
    "銀": "silver",
    "灰": "grey",
    "金": "gold",
    "紫": "purple",
    "橙": "orange",
    "黄": "yellow",
    "黃": "yellow",
    "棕": "brown",
    "米": "beige",
}

#: Words that mean the listing is an accessory *for* the product rather than the
#: product itself. "Google Fitbit Air Active Band" is not a Fitbit Air. Without this,
#: a cheap accessory outranks the real product in the comparison and the cheapest row
#: reads as the price of the thing the user asked for — a materially wrong comparison.
#:
#: Deliberately excludes "charge" (part of the real product name "Fitbit Charge 6").
#: A word is only treated as an accessory when the user did not ask for it: searching
#: "Fitbit Air band" must still find the band.
ACCESSORY_KEYWORDS = frozenset(
    """
    band strap cable cables charger charging dock cradle mount holder stand
    case cover sleeve skin pouch bag clip lanyard adapter adaptor protector
    screen filter battery accessory accessories replacement spare spares
    """.split()
)

_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(tb|gb|mb|ml|kg|l)\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _significant_tokens(text: str) -> list[str]:
    """Model-ish tokens: drop stopwords, colours and capacity units."""
    out: list[str] = []
    for token in _tokens(text):
        if token in STOPWORDS or token in COLOURS:
            continue
        if token.isdigit():
            continue
        out.append(token)
    return out


def _extract_capacity(text: str) -> str | None:
    match = _CAPACITY_RE.search(text or "")
    if not match:
        return None
    return f"{match.group(1)}{match.group(2).lower()}"


def _canonical_colour(value: Any) -> str | None:
    """Map a colour word (English, marketing name or Chinese) to a base colour."""
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    for chinese, base in CHINESE_COLOURS.items():
        if chinese in text:
            return base
    lowered = " ".join(text.split())
    if lowered in COLOUR_ALIASES:
        return COLOUR_ALIASES[lowered]
    tokens = _tokens(text)
    for token in tokens:
        if token in COLOUR_ALIASES:
            return COLOUR_ALIASES[token]
        if token in COLOURS:
            return {"gray": "grey"}.get(token, token)
    # "space grey" style: check adjacent token pairs.
    for first, second in zip(tokens, tokens[1:]):
        pair = f"{first} {second}"
        if pair in COLOUR_ALIASES:
            return COLOUR_ALIASES[pair]
    return None


def _extract_colour(text: str) -> str | None:
    return _canonical_colour(text)


#: Conditions a request may name. "any"/unset means the user does not care.
CONDITION_ALIASES = {
    "new": "new",
    "brand new": "new",
    "brand-new": "new",
    "sealed": "new",
    "used": "used",
    "second hand": "used",
    "secondhand": "used",
    "second-hand": "used",
    "pre-owned": "used",
    "preowned": "used",
    "pre owned": "used",
    "refurbished": "refurbished",
    "refurb": "refurbished",
    "renewed": "refurbished",
}

#: Values that mean "no condition constraint".
CONDITION_ANY = frozenset({"", "any", "unspecified", "either"})


def _normalise_condition(value: Any) -> str | None:
    """Map a condition word to one of new/used/refurbished, or None when unconstrained.

    Returning None means "the user did not constrain the condition", which is different
    from "the listing did not state it" — the latter is handled by the caller. An
    unrecognised value also returns None; :func:`_condition_is_constraining` is what
    distinguishes "no constraint" from "a constraint we failed to understand".
    """
    if value is None:
        return None
    text = " ".join(str(value).strip().lower().split())
    if not text or text in CONDITION_ANY:
        return None
    if text in CONDITION_ALIASES:
        return CONDITION_ALIASES[text]
    # Fall back to a token scan so "brand new, sealed" still resolves.
    tokens = _tokens(text)
    for token in tokens:
        if token in CONDITION_ALIASES:
            return CONDITION_ALIASES[token]
    for first, second in zip(tokens, tokens[1:]):
        pair = f"{first} {second}"
        if pair in CONDITION_ALIASES:
            return CONDITION_ALIASES[pair]
    return None


def _condition_is_constraining(value: Any) -> bool:
    """True when the request names a condition the user actually cares about.

    ``None``, ``""`` and ``"any"`` mean "no constraint"; anything else does constrain,
    even when it is a word this module does not recognise.
    """
    if value is None:
        return False
    return " ".join(str(value).strip().lower().split()) not in CONDITION_ANY


def _token_matches(query_token: str, title_tokens: Iterable[str]) -> bool:
    for candidate_token in title_tokens:
        if candidate_token == query_token:
            return True
        # Allow simple plurals/suffixes, but only for reasonably long tokens so that
        # short words like "air" do not match unrelated words.
        if len(query_token) >= 4 and candidate_token.startswith(query_token):
            return True
    return False


def _as_candidate(value: Candidate | dict[str, Any]) -> Candidate | SimpleNamespace:
    """Accept either a Candidate or the plain dict produced by `Candidate.to_dict()`."""
    if isinstance(value, Candidate):
        return value
    return SimpleNamespace(
        retailer=value.get("retailer"),
        url=value.get("url"),
        title=value.get("title") or "",
        sku=value.get("sku"),
        model=value.get("model"),
        variant=value.get("variant"),
        brand=value.get("brand"),
        condition=value.get("condition"),
    )


def _capacity_digit_tokens(*values: Any) -> set[str]:
    """The digit part of capacity values, so "64" in "64 GB" is not a generation."""
    digits: set[str] = set()
    for value in values:
        if not value:
            continue
        match = re.search(r"(\d+)", str(value))
        if match:
            digits.add(match.group(1))
    return digits


#: Feature phrases that legitimately contain accessory-ish words inside real
#: detail-page / listing names. "Up to 7 Day Battery Life" is a *feature* of the
#: confirmed product, not an accessory being sold (observed in the real PB Tech
#: product name, 2026-09-24). Everything else stays flagged.
FEATURE_PHRASES = frozenset({"battery life", "battery health"})

#: Accessory words that are *also* ordinary product nouns in the right context.
#: "skin" is a protective cover for a device ("iPhone 15 Pro Skin") but the normal
#: noun in skincare ("... for Blemish-Prone Skin", "Skin Serum"). Found by the
#: 2026-09-24 live acceptance: a real "The Ordinary Niacinamide 10% + Zinc 1% 30ml
#: for Blemish-Prone Skin" listing was rejected as an accessory.
#:
#: Only this contextual meaning is exempt. A bare device skin stays an accessory, and
#: every other accessory word is unaffected.
CONTEXTUAL_NOUN_ACCESSORY_WORDS = frozenset({"skin"})

#: Tokens that, immediately before/after a contextual-noun accessory word, mark it as
#: a body noun rather than a device cover. Deliberately descriptive ("prone", "dry",
#: "blemish") rather than generic ("the", "your") so "The Skin for iPhone" is not let
#: through.
_CONTEXTUAL_NOUN_BEFORE = frozenset({
    "prone", "dry", "oily", "sensitive", "normal", "combination", "mature",
    "blemish", "acne", "greasy", "aging", "ageing", "human",
})
_CONTEXTUAL_NOUN_AFTER = frozenset({
    "care", "serum", "serums", "cream", "creams", "lotion", "lotions", "cleanser",
    "cleansers", "toner", "toners", "moisturiser", "moisturisers", "moisturizer",
    "moisturizers", "treatment", "treatments", "barrier", "brightening", "hydration",
    "tone", "texture", "concerns", "health", "boost",
})


def _is_contextual_noun(tokens: list[str], index: int) -> bool:
    """True when a contextual-noun accessory word is a body noun here, not a cover."""
    word = tokens[index]
    if word not in CONTEXTUAL_NOUN_ACCESSORY_WORDS:
        return False
    before = tokens[index - 1] if index > 0 else ""
    after = tokens[index + 1] if index + 1 < len(tokens) else ""
    return before in _CONTEXTUAL_NOUN_BEFORE or after in _CONTEXTUAL_NOUN_AFTER


def flagged_accessory_words(tokens: Iterable[str], asked_tokens: Iterable[str]) -> list[str]:
    """Accessory words in ``tokens`` that the user did not ask for.

    A word is only an accessory when the user did not request it ("Fitbit Air band"
    must still find the band). :data:`FEATURE_PHRASES` ("battery life") and a
    contextual body noun ("... for Blemish-Prone Skin") are part of the product, not
    an accessory for sale. Everything else that names an accessory for the product is
    returned, so an accessory can never stand in for the product itself.
    """
    ordered = list(tokens)
    asked = set(asked_tokens or ())
    flagged: list[str] = []
    for index, word in enumerate(ordered):
        if word not in ACCESSORY_KEYWORDS or word in asked:
            continue
        following = ordered[index + 1] if index + 1 < len(ordered) else ""
        if f"{word} {following}" in FEATURE_PHRASES:
            continue
        if _is_contextual_noun(ordered, index):
            continue
        if word not in flagged:
            flagged.append(word)
    return flagged


def _adjacent_generation_tokens(tokens: list[str], model_tokens: set[str]) -> list[str]:
    """Pure-digit tokens that directly follow one of the model tokens.

    "Fitbit Air 2" -> ["2"] when {"air"} is the model-token set. Feature numbers in
    marketing text ("7 Day Battery Life") are not adjacent to a model token and are
    therefore not treated as a product generation.
    """
    generations: list[str] = []
    for i, tok in enumerate(tokens[:-1]):
        if tok in model_tokens and tokens[i + 1].isdigit():
            generations.append(tokens[i + 1])
    return generations


#: Generic product words that can sit directly before the model in a *name* without
#: ever being a brand claim: the product category, a shipping qualifier or a
#: marketing suffix. A real page name often leads with one of these — "Wireless
#: Noise Cancelling Headphones WH-1000XM5", "Serum Niacinamide 10% + Zinc 1%" — and
#: the structured ``brand`` legitimately appears nowhere in the name. Without this,
#: the brand slot would read "headphones"/"serum" as a competing brand and reject the
#: *correct* product (found in the 2026-09-24 self-review). Only words that are never
#: a brand belong here; it is deliberately a plain category/qualifier list, not a
#: model-token list, and it never suppresses a genuine brand (e.g. "Bose").
_GENERIC_NAME_WORDS = frozenset(
    """
    wireless wired bluetooth noise cancelling canceling headphones headphone
    earphones earphone earbuds earbud headset headsets speaker speakers soundbar
    tracker smartwatch smart fitness activity watch phone smartphone cell mobile
    tablet laptop notebook computer monitor display screen camera lens keyboard
    mouse printer scanner router modem vacuum cleaner blender mixer kettle toaster
    fryer machine maker coffee
    serum serums cream creams lotion lotions moisturiser moisturizer cleanser
    cleansers toner toners treatment shampoo conditioner oil spray gel balm mask
    powder stick palette foundation mascara lipstick perfume fragrance
    original genuine official authentic premium deluxe classic standard essential
    essentials advanced latest edition series generation gen version pack set kit
    bundle combo refill multipack mini lite
    """.split()
)


def _is_ignorable_brand_context(token: str) -> bool:
    """A name token that is never a brand claim: stopword, colour, digit, capacity or
    a generic product/qualifier word.

    Used so a *modifier* before the model ("Black WH-1000XM5", "New …",
    "Wireless … Headphones WH-1000XM5") is not mistaken for a competing brand.
    """
    return (
        token in STOPWORDS
        or token in COLOURS
        or token.isdigit()
        or token in _GENERIC_NAME_WORDS
        or bool(_CAPACITY_RE.fullmatch(token))
    )


def _name_brand_conflict(name: str, brand: str, model_tokens: Iterable[str]) -> str | None:
    """The brand the page's *name* states when it contradicts the page's structured brand.

    The name and the structured ``brand`` both come from the **same selected** JSON-LD
    Product, so they describe one product. Either the name names that brand, or it
    omits the brand and starts at the model — both are consistent. But a name whose
    *brand slot* (the meaningful token right before the model) states a **different**
    brand while the structured brand appears nowhere in the name is the page
    contradicting itself: the two fields must not be concatenated into a single
    "same" identity. Returns the conflicting token, or ``None`` when they agree.
    """
    brand_tokens = set(_tokens(brand))
    if not brand_tokens:
        return None
    name_tokens = _tokens(name)
    if not name_tokens or brand_tokens & set(name_tokens):
        return None
    model = set(model_tokens)
    for index, token in enumerate(name_tokens):
        if token not in model:
            continue
        before = index - 1
        while before >= 0 and _is_ignorable_brand_context(name_tokens[before]):
            before -= 1
        return name_tokens[before] if before >= 0 else None
    return None


def _page_self_conflicts(
    name: str,
    brand: Any,
    variant: Any,
    model_tokens: Iterable[str],
) -> list[str]:
    """Reasons the page's own name contradicts its own structured brand/variant.

    Concatenating name + structured fields into one blob hides a page that states a
    different value for the *same* aspect in two places. Only a clear, same-aspect
    contradiction is reported (a brand-vs-brand or capacity-vs-capacity or
    colour-vs-colour mismatch); complementary fields (name omits the brand, structured
    brand supplies it) are not conflicts and stay a valid confirmation.
    """
    reasons: list[str] = []
    brand_text = str(brand or "")
    variant_text = str(variant or "")

    conflicting_brand = _name_brand_conflict(name, brand_text, model_tokens)
    if conflicting_brand:
        reasons.append(
            f"the name says “{conflicting_brand}” but the structured brand says “{brand_text}”"
        )

    name_capacity = _extract_capacity(name)
    variant_capacity = _extract_capacity(variant_text)
    if name_capacity and variant_capacity and name_capacity != variant_capacity:
        reasons.append(
            f"the name says “{name_capacity}” but the structured variant says “{variant_capacity}”"
        )

    name_colour = _canonical_colour(name)
    variant_colour = _canonical_colour(variant_text)
    if name_colour and variant_colour and name_colour != variant_colour:
        reasons.append(
            f"the name says “{name_colour}” but the structured variant says “{variant_colour}”"
        )
    return reasons


def match_candidate(request: dict[str, Any], candidate: Candidate | dict[str, Any]) -> tuple[str, list[str]]:
    """Decide whether a candidate is the requested product.

    Returns ``(status, reasons)`` where status is one of
    :data:`MATCH_SAME`, :data:`MATCH_DIFFERENT`, :data:`MATCH_NEEDS_CONFIRMATION`.

    The rules are deliberately conservative: a stated mismatch is always
    "different"; a constraint the candidate does not state is "needs_confirmation".
    """
    candidate = _as_candidate(candidate)
    query = str(request.get("query") or request.get("product_name") or "")
    query_tokens = _significant_tokens(query)
    # `brand`, when present, is structured evidence the *same* selected JSON-LD product
    # stated (see `_detail_identity`); a search card never carries it.
    title = " ".join(
        filter(
            None,
            [
                candidate.title,
                getattr(candidate, "brand", None),
                candidate.model,
                candidate.variant,
                candidate.sku,
            ],
        )
    )
    title_tokens = _tokens(title)
    reasons: list[str] = []

    if not query_tokens:
        return MATCH_NEEDS_CONFIRMATION, ["The request does not name a product model."]

    missing = [t for t in query_tokens if not _token_matches(t, title_tokens)]
    if missing:
        return MATCH_DIFFERENT, [f"Candidate does not mention: {', '.join(missing)}"]

    reasons.append(f"Model matched ({', '.join(query_tokens)})")

    # Generation guard: "Fitbit Air 2" is never "Fitbit Air". A generation the user
    # named must be stated on the listing; a generation the listing names but the
    # request did not cannot be silently accepted either. Capacity numbers
    # ("Fitbit Air 64 GB") are excluded — they belong to the capacity check below.
    capacity_digits = _capacity_digit_tokens(request.get("capacity"), _extract_capacity(title))
    query_gen = {
        g for g in _adjacent_generation_tokens(_tokens(query), set(query_tokens))
        if g not in capacity_digits
    }
    title_gen = {
        g for g in _adjacent_generation_tokens(title_tokens, set(query_tokens))
        if g not in capacity_digits
    }
    # A stated, conflicting generation is a different product even before the
    # missing-generation check runs.
    if query_gen:
        conflicts = sorted(g for g in title_gen if g not in query_gen)
        if conflicts:
            return MATCH_DIFFERENT, reasons + [
                f"Different generation: listing says '{', '.join(conflicts)}'."
            ]
    unstated = [g for g in query_gen if g not in title_gen]
    if unstated:
        return MATCH_NEEDS_CONFIRMATION, reasons + [
            f"Generation '{', '.join(unstated)}' is not stated on this listing."
        ]
    if query_gen:
        reasons.append(f"Generation matched ({', '.join(sorted(query_gen))})")
    elif title_gen:
        # The listing states a generation the user did not constrain. That is
        # identifying evidence, not a conflict: accept it and let it flow into
        # the confirmed identity so monitoring stays pinned to this generation.
        reasons.append(f"Generation confirmed from listing ({', '.join(sorted(title_gen))})")

    # An accessory is not the requested product. Only reject words the user did not
    # ask for, so "Fitbit Air band" still finds the band. Feature phrases ("battery
    # life") and contextual body nouns ("... for Blemish-Prone Skin") are part of the
    # product, not an accessory for sale.
    unexpected = flagged_accessory_words(title_tokens, query_tokens)
    if unexpected:
        try:
            import config as _config
            if _config.llm_match_assist():
                import agent as _agent
                verdict, reason = _agent.match_product_aspect(
                    "accessory", None, title, query
                )
                if verdict == "same":
                    reasons.append(
                        f"Accessory wording is part of the product name: {reason}"
                    )
                    unexpected = set()
        except Exception:
            pass  # fail-safe: keep the deterministic rejection below
    if unexpected:
        return MATCH_DIFFERENT, reasons + [
            f"Listing is an accessory, not the product: {', '.join(sorted(unexpected))}."
        ]

    # LLM fallback: the hardcoded keyword list cannot catch every accessory type.
    # Only runs when the deterministic scan found nothing and the feature is enabled.
    # A failure keeps the deterministic "not an accessory" result (fail-safe).
    if not unexpected:
        try:
            import config as _config
            if _config.llm_match_assist():
                import agent as _agent
                verdict, reason = _agent.match_product_aspect(
                    "accessory", None, title, query
                )
                if verdict == "different":
                    return MATCH_DIFFERENT, reasons + [
                        f"Listing appears to be an accessory, not the product: {reason}"
                    ]
                # "same" and "needs_confirmation" both fall through to colour/capacity checks
        except Exception:
            pass  # fail-safe: keep the deterministic result

    requested_colour = _normalise_colour(request.get("colour"))
    if requested_colour:
        found = _extract_colour(title)
        if found is None:
            # LLM fallback: the hardcoded alias list cannot catch every marketing
            # colour name. Only runs when the deterministic extraction found no colour
            # and the feature is enabled. A failure keeps the deterministic
            # "needs_confirmation" result (fail-safe).
            try:
                import config as _config
                if _config.llm_match_assist():
                    import agent as _agent
                    verdict, reason = _agent.match_product_aspect(
                        "colour", requested_colour, title, query
                    )
                    if verdict == "same":
                        reasons.append(f"Colour matched via AI ({requested_colour}): {reason}")
                        found = requested_colour  # proceed as matched
                    elif verdict == "different":
                        return MATCH_DIFFERENT, reasons + [
                            f"Different colour (AI): {reason}"
                        ]
                    # needs_confirmation falls through to the return below
            except Exception:
                pass  # fail-safe: keep the deterministic result
            if found is None:
                return MATCH_NEEDS_CONFIRMATION, reasons + [f"Colour '{requested_colour}' is not stated on this listing."]
        if found != requested_colour:
            return MATCH_DIFFERENT, reasons + [f"Different colour: listing says '{found}'."]
        reasons.append(f"Colour matched ({requested_colour})")

    requested_capacity = _normalise_capacity(request.get("capacity"))
    if requested_capacity:
        found = _extract_capacity(title)
        if found is None:
            return MATCH_NEEDS_CONFIRMATION, reasons + [
                f"Capacity '{requested_capacity}' is not stated on this listing."
            ]
        if found != requested_capacity:
            return MATCH_DIFFERENT, reasons + [f"Different capacity: listing says '{found}'."]
        reasons.append(f"Capacity matched ({requested_capacity})")

    # B2: a condition requirement must never be silently ignored. A listing that states
    # a different condition is "different"; a listing that states nothing cannot confirm
    # the requirement and is "needs_confirmation" — unknown information is never allowed
    # to masquerade as a confirmed match.
    raw_condition = request.get("condition")
    if _condition_is_constraining(raw_condition):
        requested_condition = _normalise_condition(raw_condition)
        if requested_condition is None:
            # The user named a condition we cannot interpret, so we cannot claim it is
            # satisfied. Refusing to guess is the safe outcome.
            return MATCH_NEEDS_CONFIRMATION, reasons + [
                f"Condition '{raw_condition}' is not understood, so it could not be confirmed."
            ]
        found_condition = _normalise_condition(getattr(candidate, "condition", None))
        if found_condition is None:
            return MATCH_NEEDS_CONFIRMATION, reasons + [
                f"Condition '{requested_condition}' is not stated on this listing."
            ]
        if found_condition != requested_condition:
            return MATCH_DIFFERENT, reasons + [
                f"Different condition: listing says '{found_condition}'."
            ]
        reasons.append(f"Condition matched ({requested_condition})")

    return MATCH_SAME, reasons


def _normalise_colour(value: Any) -> str | None:
    return _canonical_colour(value)


def _normalise_capacity(value: Any) -> str | None:
    if not value:
        return None
    return _extract_capacity(str(value)) or str(value).strip().lower().replace(" ", "") or None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _preferred_title(card_title: str, detail_name: str) -> str:
    """Pick a readable product name without inventing one.

    Some shops append the whole marketing bullet list to the product name in the
    page's ``name`` field, e.g. ``"Google Fitbit Air - Obsidian - Up to 7 Day
    Battery Life - ..."``. When the search card's title is a prefix of the detail
    name and the remainder starts a new clause, the card title is the product name.
    Otherwise the detail name is kept because it carries information the card lacked.
    """
    if not detail_name:
        return card_title
    if not card_title:
        return detail_name
    if len(detail_name) > len(card_title) and detail_name.lower().startswith(card_title.lower()):
        remainder = detail_name[len(card_title):].lstrip()
        if remainder[:1] in ("-", "\u2013", "\u2014"):
            return card_title
    return detail_name


def _detail_identity(
    request: dict[str, Any],
    entry: dict[str, Any],
    quote: dict[str, Any],
) -> tuple[str, list[str]]:
    """Re-decide the product identity from the *detail page's* own evidence.

    The page's product name is the primary evidence. When that name omits the brand
    or the spec, the page's **own** structured JSON-LD fields for the *same selected
    product* — ``brand`` and ``variant`` (size/capacity/colour/model) — fill the gap;
    they travel on the quote from `scraper`, never from the search card and never from
    unrelated OpenGraph metadata. The search card's title is deliberately not
    consulted: merging it in would let a card that says "Fitbit Air" confirm a
    "Fitbit Charge 6" page, which is exactly the failure this exists to prevent.

    A conflict the page itself states is still ``different``; a gap neither the name
    nor the structured fields fill is still ``needs_confirmation`` rather than a
    silent pass. A page that names nothing cannot be confirmed either.

    A page that states a *different* value for the same aspect in two places — its name
    says "Bose" while its structured brand says "Sony", or its name says 30 ml while
    its structured variant says 60 ml — is a contradiction rather than a gap, and is
    ``different`` (see :func:`_page_self_conflicts`). Only genuinely complementary
    fields are allowed to stand in for one another.

    The condition is the one exception: it is never part of a page's product name, so
    the candidate's stated condition is retained as its only available evidence. It
    cannot mask a model mismatch — that check runs against the detail evidence alone.
    """
    name = " ".join(str(quote.get("product_name") or "").split())
    if not name:
        return MATCH_NEEDS_CONFIRMATION, [
            "The detail page did not state a product name, so its identity could not be confirmed."
        ]
    model_tokens = _significant_tokens(
        str(request.get("query") or request.get("product_name") or "")
    )
    conflicts = _page_self_conflicts(name, quote.get("brand"), quote.get("variant"), model_tokens)
    if conflicts:
        return MATCH_DIFFERENT, [
            f"The detail page contradicts itself: {reason}." for reason in conflicts
        ]
    detail_candidate = {
        "retailer": entry.get("retailer"),
        "url": entry.get("url"),
        "title": name,
        "sku": None,
        "model": None,
        # Same selected JSON-LD product only: these are the page's own structured
        # identity fields, not card or OpenGraph text.
        "brand": quote.get("brand"),
        "variant": quote.get("variant"),
        "condition": entry.get("condition"),
    }
    return match_candidate(request, detail_candidate)


def compare_offers(offers: list[dict[str, Any]], request: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Rank offers and label every uncertainty.

    Returns ``(rows, notes)``. Rows are sorted so that confirmed-buyable, within-budget
    offers come first. Rows excluded from the ranking (currency mismatch, unknown
    price) are not dropped silently: they are listed with an explicit reason.
    """
    currency = (request.get("currency") or "").upper() or None
    budget = _to_decimal(request.get("budget"))
    notes: list[str] = []
    rows: list[dict[str, Any]] = []

    for offer in offers:
        price = _to_decimal(offer.get("price"))
        offer_currency = (offer.get("currency") or "").upper() or None
        in_stock = offer.get("in_stock")
        row_notes: list[str] = []
        excluded: str | None = None

        if price is None:
            excluded = "price_unknown"
            row_notes.append("Price could not be confirmed on the page.")
        elif currency and offer_currency and offer_currency != currency:
            excluded = "currency_mismatch"
            row_notes.append(f"Priced in {offer_currency}, not {currency}; not compared directly.")
        elif currency and offer_currency is None:
            excluded = "currency_unknown"
            row_notes.append("Currency could not be confirmed.")

        if in_stock is False:
            row_notes.append("Out of stock at query time.")
        elif in_stock is None:
            row_notes.append("Stock not stated on the page; could not be confirmed.")

        shipping_cost = offer.get("shipping_cost")
        shipping_known = shipping_cost is not None
        if not shipping_known:
            row_notes.append("Shipping cost not stated; not included in the price shown.")

        condition = offer.get("condition")
        if not condition:
            row_notes.append("Condition (new/used) not stated.")

        within_budget: bool | None = None
        if budget is not None and price is not None and (not currency or not offer_currency or offer_currency == currency):
            within_budget = price <= budget

        rows.append(
            {
                "retailer": offer.get("retailer"),
                "title": offer.get("title") or offer.get("product_name"),
                "detail_name": offer.get("detail_name"),
                "url": offer.get("url"),
                "price": str(price.quantize(Decimal("0.01"))) if price is not None else None,
                "currency": offer_currency,
                "in_stock": in_stock,
                "shipping_cost": shipping_cost,
                "shipping_known": shipping_known,
                "condition": condition,
                "queried_at": offer.get("queried_at"),
                "within_budget": within_budget,
                "buyable": in_stock is True,
                "excluded_reason": excluded,
                "notes": row_notes,
                # Stage B closeout: the match verdict travels with the offer so the
                # display and the selection step cannot lose it. Only a confirmed
                # "same" offer may become a monitoring task. For a priced offer this
                # is the *detail page's* verdict (see `_detail_identity`).
                "match": offer.get("match") or MATCH_SAME,
                # The search card's verdict, kept for display so a user can see that
                # the page disagreed with the listing that led to it.
                "card_match": offer.get("card_match"),
                "match_reasons": offer.get("match_reasons"),
                # Where this offer's URL came from: a built-in shop adapter or a
                # web-search candidate. Kept on the row so the source attribution
                # survives into preview/confirm and the monitoring task.
                "source_kind": offer.get("source_kind") or SOURCE_KIND_RETAILER,
            }
        )

    ranked = [r for r in rows if r["excluded_reason"] is None]
    excluded_rows = [r for r in rows if r["excluded_reason"] is not None]
    ranked.sort(
        key=lambda r: (
            # Confirmed-same products rank above unconfirmed candidates; only then
            # buyability and price. A lower price on an unconfirmed listing must
            # never push a confirmed match down.
            0 if r["match"] == MATCH_SAME else 1,
            0 if r["buyable"] else 1,
            -(1 if r["within_budget"] else 0),
            _to_decimal(r["price"]) or Decimal("1e12"),
            str(r["retailer"]),
        )
    )
    if excluded_rows:
        notes.append(f"{len(excluded_rows)} offer(s) excluded from the ranking (see reasons).")
    if not ranked:
        notes.append("No offer could be compared: no candidate produced a usable price.")
    return ranked + excluded_rows, notes


def _default_web_candidates(query: str) -> list[dict[str, Any]]:
    """Web-search candidates from every configured search backend, merged.

    Each pluggable backend (Brave, Tavily, ...) is tried independently; an
    unconfigured backend is skipped and a failing backend never hides the others'
    results. Candidates are de-duplicated by URL, first backend wins. When no
    backend is configured at all, ``BraveNotConfigured`` is raised so the caller
    reports the same "web search unavailable" status as before (the built-in
    shops keep working).
    """
    import search_providers.brave as brave
    import search_providers.tavily as tavily

    backends = []
    if brave.is_configured():
        backends.append((WEB_SEARCH_SOURCE, brave.search))
    if tavily.is_configured():
        backends.append(("Tavily Web Search", tavily.search))
    if not backends:
        raise brave.BraveNotConfigured(
            "Web search is not configured (missing BRAVE_SEARCH_API_KEY or "
            "TAVILY_API_KEY); only the built-in shops were searched."
        )

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    last_error: Exception | None = None
    for name, fn in backends:
        try:
            raw = fn(query)
        except Exception as exc:  # noqa: BLE001 - one broken backend must not hide the others
            logger.warning("Web search backend %s failed: %s", name, exc)
            last_error = exc
            continue
        for item in raw:
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(item)
    if not merged and last_error is not None:
        # Every configured backend failed: surface the error so the caller reports
        # "web search errored" rather than silently returning zero candidates.
        raise last_error
    return merged


def _search_web_candidates(
    query: str,
    web_candidates_fn: Callable[[str], list[dict[str, Any]]] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the web-search source. Returns (source_status, candidate_entries).

    Independent from the retailer loop by construction: any failure produces a
    source status here and never touches the retailers' results. A missing provider
    and a provider that reports "not configured" are both *unavailable* (a
    configuration state), which is deliberately distinct from *error* (the provider
    ran and failed).
    """
    entry: dict[str, Any] = {"retailer": WEB_SEARCH_SOURCE, "status": "ok", "candidates": 0, "error": None}
    if web_candidates_fn is None:
        entry["status"] = "unavailable"
        entry["error"] = (
            "Not configured for this run (missing BRAVE_SEARCH_API_KEY); "
            "only the built-in shops were searched."
        )
        return entry, []

    try:
        raw = web_candidates_fn(query)
    except Exception as exc:  # noqa: BLE001 - one broken source must not hide the others
        import search_providers.brave as brave

        if isinstance(exc, brave.BraveNotConfigured):
            entry["status"] = "unavailable"
        else:
            entry["status"] = "error"
            logger.warning("Web search failed: %s", exc)
        entry["error"] = str(exc)
        return entry, []
    entry["candidates"] = len(raw)
    entries: list[dict[str, Any]] = []
    for item in raw:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        entries.append(
            {
                "retailer": WEB_SEARCH_SOURCE,
                "url": url,
                "title": title,
                "sku": None,
                "model": None,
                "variant": None,
                "condition": None,
                "source_kind": SOURCE_KIND_WEB,
                # match/reasons are filled in by the caller via match_candidate.
                "match": None,
                "reasons": None,
            }
        )
    return entry, entries


def search_and_compare(
    request: dict[str, Any],
    *,
    retailers: Iterable[Retailer] | None = None,
    search_fn: Callable[[Retailer, str], list[Candidate]] | None = None,
    offer_fn: Callable[[Retailer, str], dict[str, Any]] | None = None,
    web_candidates_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    web_offer_fn: Callable[[str], dict[str, Any]] | None = None,
    expand_fn: Callable[[dict[str, Any]], list[str]] | None = None,
    rank_fn: Callable[[list[dict[str, Any]], dict[str, Any]], list[dict[str, Any]]] | None = None,
    include_web: bool = True,
    limit_per_retailer: int = 8,
    max_details: int = DETAIL_CAP,
) -> dict[str, Any]:
    """Search every enabled retailer (and, when configured, the web), match
    candidates, and compare their offers.

    `search_fn` / `offer_fn` / `web_candidates_fn` / `web_offer_fn` are injectable
    so the whole flow can be tested offline without network access. When
    `web_candidates_fn` is not injected the real Brave client is used, which searches
    when a key is configured and reports "unavailable" when it is not.

    Web candidates are URLs only. Their price, currency and stock come exclusively
    from the candidate detail page (structured data first, then the LLM) — a search snippet is never
    a price. Both source classes fail independently: one broken source never turns
    the whole result into "no products found". Every priced offer's `match` verdict is
    re-derived from the detail page's own evidence — its product name, plus the
    *same selected* JSON-LD product's structured brand/size when the name omits them
    (`_detail_identity`) — so a card that pointed at a different product cannot become
    a confirmed, monitorable offer.
    """
    shop_list = list(retailers) if retailers is not None else list(default_enabled_retailers())
    query = str(request.get("query") or request.get("product_name") or "").strip()
    search_fn = search_fn or (lambda retailer, text: retailer.search(text, limit=limit_per_retailer))
    offer_fn = offer_fn or (lambda retailer, url: retailer.fetch_offer(url))
    web_offer_fn = web_offer_fn or _default_web_offer

    queried_at = datetime.now(timezone.utc).isoformat()
    sources: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    notes: list[str] = []
    unavailable: list[dict[str, Any]] = []


    if not query:
        notes.append("No product name was supplied, so nothing was searched.")
        return {
            "query": query, "currency": request.get("currency"), "budget": request.get("budget"),
            "queried_at": queried_at, "sources": [], "candidates": [],
            "offers": [], "notes": notes, "unavailable": [],
            "counts": {"candidates": 0, "same": 0, "needs_confirmation": 0, "different": 0, "offers": 0},
        }

    # --- built-in shops: unchanged behaviour, one independent source each. ---
    for retailer in shop_list:
        try:
            found = search_fn(retailer, query)
        except Exception as exc:  # a broken source must not hide the others
            logger.warning("Retailer %s search failed: %s", retailer.name, exc)
            sources.append({"retailer": retailer.name, "status": "error", "candidates": 0, "error": str(exc)})
            notes.append(f"{retailer.name} could not be searched: {exc}")
            continue
        sources.append({"retailer": retailer.name, "status": "ok", "candidates": len(found), "error": None})
        for candidate in found:
            status, reasons = match_candidate(request, candidate)
            entry = candidate.to_dict()
            entry["match"] = status
            entry["reasons"] = reasons
            entry["source_kind"] = SOURCE_KIND_RETAILER
            candidates.append(entry)

    # --- web-search source: candidates only; runs after the shops so that a
    # duplicate URL keeps the shop attribution (richer metadata, configured host). ---
    if include_web:
        # The default *is* the real provider (review round 1, item 1). Passing `None`
        # here used to mean "never search the web", so a configured key still produced
        # zero calls. When no test provider is injected, the Brave client decides for
        # itself: with a key it searches, without one it raises BraveNotConfigured and
        # the source is reported as unavailable while the shops keep working.
        web_fn = web_candidates_fn if web_candidates_fn is not None else _default_web_candidates

        # --- optional LLM query expansion: one search becomes several Brave calls. ---
        # The original query is always kept; expanded queries are appended. A failure
        # in expansion returns just the original query, so the caller never loses the
        # ability to search.
        queries = [query]
        if expand_fn is not None:
            try:
                expanded = expand_fn(request)
                if isinstance(expanded, list):
                    for q in expanded:
                        if isinstance(q, str) and q.strip() and q.strip().lower() != query.lower():
                            queries.append(q.strip())
            except Exception:
                logger.debug("Query expansion failed; using original query only.", exc_info=True)

        all_web_entries: list[dict[str, Any]] = []
        web_status: dict[str, Any] | None = None
        any_query_ok = False
        for q in queries:
            status_entry, entries = _search_web_candidates(q, web_fn)
            if status_entry["status"] == "ok":
                any_query_ok = True
            if web_status is None:
                web_status = status_entry
            elif status_entry["status"] != "ok" and web_status["status"] == "ok":
                # A later query failed; keep the first OK status but note the failure.
                notes.append(f"{WEB_SEARCH_SOURCE}: a later query failed ({status_entry['error']}).")
            elif status_entry["status"] == "ok" and web_status["status"] != "ok":
                # An *earlier* query failed but a later one succeeded: the source is
                # usable, keep its results, and report the earlier failure honestly
                # instead of discarding successful candidates.
                notes.append(
                    f"{WEB_SEARCH_SOURCE}: an earlier query failed "
                    f"({web_status['error']})."
                )
                web_status = status_entry
            all_web_entries.extend(entries)
        if web_status is None:
            web_status = {"retailer": WEB_SEARCH_SOURCE, "status": "ok", "candidates": 0, "error": None}
        # A run is only a failure when *no* query succeeded: a transient failure of one
        # expanded query must not hide the candidates a later query did return.
        if any_query_ok and web_status["status"] != "ok":
            web_status["status"] = "ok"
            web_status["error"] = None
        web_status["candidates"] = len(all_web_entries)
        sources.append(web_status)
        if web_status["status"] == "error":
            notes.append(f"{WEB_SEARCH_SOURCE} could not be searched: {web_status['error']}")
        elif web_status["status"] == "unavailable":
            notes.append(f"{WEB_SEARCH_SOURCE} was not searched: {web_status['error']}")
        else:
            for entry in all_web_entries:
                status, reasons = match_candidate(request, entry)
                entry["match"] = status
                entry["reasons"] = reasons
                candidates.append(entry)

    # --- cross-source dedupe on the canonical product URL. ---
    deduped: list[dict[str, Any]] = []
    seen_urls: dict[str, dict[str, Any]] = {}
    for entry in candidates:
        key = normalise_product_url(entry["url"])
        existing = seen_urls.get(key)
        if existing is not None:
            if (
                existing.get("source_kind") == SOURCE_KIND_RETAILER
                and entry.get("source_kind") == SOURCE_KIND_WEB
            ):
                existing.setdefault("also_found_by", []).append(WEB_SEARCH_SOURCE)
            continue
        seen_urls[key] = entry
        deduped.append(entry)
    candidates = deduped

    # A supported shop that this run did not search is reported, never silently
    # omitted: "we did not check it" is different from "it had nothing". This covers
    # both a shop disabled by configuration and one explicitly left out by the caller.
    active_names = {r.name for r in shop_list}
    for skipped in all_retailers():
        if skipped.name in active_names:
            continue
        sources.append(
            {
                "retailer": skipped.name,
                "status": "disabled",
                "candidates": 0,
                "error": "Not enabled in this configuration (add its domain to SCRAPER_ALLOWED_DOMAINS).",
            }
        )
        notes.append(
            f"{skipped.name} was not searched: it is not enabled in this configuration."
        )

    same = [c for c in candidates if c["match"] == MATCH_SAME]
    needs_confirmation = [c for c in candidates if c["match"] == MATCH_NEEDS_CONFIRMATION]
    different = [c for c in candidates if c["match"] == MATCH_DIFFERENT]

    # --- optional LLM candidate ranking: re-order so detail quota goes to the most
    # likely candidates first. Never removes candidates; failure keeps original order. ---
    if rank_fn is not None and len(same) + len(needs_confirmation) > 3:
        try:
            ranked = rank_fn(same + needs_confirmation, request)
            same = [c for c in ranked if c["match"] == MATCH_SAME]
            needs_confirmation = [c for c in ranked if c["match"] == MATCH_NEEDS_CONFIRMATION]
        except Exception:
            logger.debug("Candidate ranking failed; keeping original order.", exc_info=True)

    # --- detail-fetch budget: at most `max_details` pages in total, split between
    # web and built-in shops; unused quota of one class may flow to the other. ---
    to_price = _select_for_pricing(same + needs_confirmation, max_details)
    if len(same) + len(needs_confirmation) > len(to_price):
        notes.append(
            f"Only the first {len(to_price)} matching candidate(s) were priced; "
            "narrow the request to compare more."
        )

    by_retailer = {r.name: r for r in shop_list}
    offers: list[dict[str, Any]] = []
    for entry in to_price:
        is_web = entry.get("source_kind") == SOURCE_KIND_WEB
        if is_web:
            fetch = lambda url: web_offer_fn(url)  # noqa: E731
            label = WEB_SEARCH_SOURCE
        else:
            retailer = by_retailer.get(entry["retailer"]) or next(
                (r for r in shop_list if r.name == entry["retailer"]), None
            )
            if retailer is None:
                continue
            fetch = lambda url, r=retailer: offer_fn(r, url)  # noqa: E731
            label = entry["retailer"]
        try:
            quote = fetch(entry["url"])
        except Exception as exc:
            logger.warning("Detail fetch failed for %s: %s", entry["url"], exc)
            reason = str(exc)
            unavailable.append(
                {
                    "retailer": label,
                    "source_kind": entry.get("source_kind") or SOURCE_KIND_RETAILER,
                    "url": entry["url"],
                    "title": entry["title"],
                    "reason": reason,
                }
            )
            notes.append(f"{label}: could not read the price for “{entry['title']}” ({reason}).")
            continue
        detail_name = quote.get("product_name") or ""
        card_title = entry["title"] or ""

        # --- detail-page identity re-verification (review round 1, item 2) ---
        # The search card's title is a *claim about the page*, not evidence from it:
        # a card reading "Fitbit Air" can link to a "Fitbit Charge 6" page. Identity is
        # therefore decided again from the detail page's own evidence — its name, and
        # the same selected JSON-LD product's structured brand/size where the name
        # omits them — and that verdict is what travels on the offer, so a page whose
        # real product differs is `different` and `select_offer` refuses it. The card
        # title is never merged in to fill an identity the page did not state.
        detail_status, detail_reasons = _detail_identity(request, entry, quote)
        if detail_status != entry["match"]:
            verdict = (
                "does not match the requested product"
                if detail_status == MATCH_DIFFERENT
                else "could not be confirmed as the requested product"
            )
            evidence = (
                f"the page names “{detail_name}”" if detail_name
                else "the page states no product name"
            )
            notes.append(
                f"{label}: “{card_title}” {verdict} — {evidence} "
                f"({'; '.join(detail_reasons)})."
            )

        offers.append(
            {
                "retailer": label,
                "title": _preferred_title(card_title, detail_name),
                "detail_name": detail_name,
                "url": quote.get("url") or entry["url"],
                "price": quote.get("price"),
                "currency": quote.get("currency"),
                "in_stock": quote.get("in_stock"),
                "shipping_cost": quote.get("shipping_cost"),
                "condition": entry.get("condition"),
                "queried_at": quote.get("fetched_at") or queried_at,
                # The page's own identity decides selectability, not the card's.
                "match": detail_status,
                "card_match": entry["match"],
                "match_reasons": detail_reasons,
                "source_kind": entry.get("source_kind") or SOURCE_KIND_RETAILER,
                "also_found_by": entry.get("also_found_by"),
            }
        )

    rows, compare_notes = compare_offers(offers, request)
    notes.extend(compare_notes)

    return {
        "query": query,
        "currency": (request.get("currency") or "").upper() or None,
        "budget": request.get("budget"),
        "queried_at": queried_at,
        "sources": sources,
        "candidates": candidates,
        "offers": rows,
        "notes": notes,
        # Structured failure reasons for candidates whose detail page could not be
        # priced (no explicit Product, conflicting offers, access refused, unknown
        # identity, cross-host redirect, ...). Never filled from search snippets.
        "unavailable": unavailable,
        "counts": {
            "candidates": len(candidates),
            "same": len(same),
            "needs_confirmation": len(needs_confirmation),
            "different": len(different),
            "offers": len(rows),
        },
    }


def _default_web_offer(url: str) -> dict[str, Any]:
    """Detail fetch for a web candidate: host-restricted, with an LLM price fallback."""
    import scraper

    return scraper.fetch_quote_on_host_llm(url)


def _select_for_pricing(eligible: list[dict[str, Any]], max_details: int) -> list[dict[str, Any]]:
    """Pick which eligible candidates get their detail page fetched.

    Budget rules (BROAD_PRODUCT_DISCOVERY.md): one search fetches at most
    ``max_details`` (default 6) detail pages; no more than 3 from web candidates and
    no more than 3 from built-in shops. When one class has unused quota, the other
    class may use it — but the total stays capped, and confirmed-same candidates are
    always priced before needs-confirmation ones.
    """
    total = max(0, int(max_details))
    web_cap = min(WEB_DETAIL_CAP, total)
    retail_cap = min(RETAILER_DETAIL_CAP, total)

    def split(kind: str) -> list[dict[str, Any]]:
        return [c for c in eligible if (c.get("source_kind") or SOURCE_KIND_RETAILER) == kind]

    web_all = split(SOURCE_KIND_WEB)
    retail_all = split(SOURCE_KIND_RETAILER)
    web_pick = web_all[:web_cap]
    retail_pick = retail_all[:retail_cap]
    remaining = total - len(web_pick) - len(retail_pick)
    web_rest = web_all[web_cap:]
    retail_rest = retail_all[retail_cap:]
    # Transfer leftovers, built-in shops first (they carry the better metadata).
    while remaining > 0 and (retail_rest or web_rest):
        if retail_rest:
            retail_pick.append(retail_rest.pop(0))
        else:
            web_pick.append(web_rest.pop(0))
        remaining -= 1
    combined = retail_pick + web_pick
    # Hard total cap: per-class quotas can both be fully used while their sum still
    # exceeds ``max_details`` (e.g. 3 shop + 4 web). Trim to the documented total and
    # keep the built-in shops (better metadata) at the front when doing so.
    if len(combined) > total:
        combined = combined[:total]
    return combined


def _identity_signature(offer: dict[str, Any]) -> tuple:
    """Colour + capacity of an offer, for keeping one task to one product variant.

    Two offers may both be "same" for the request yet still be different variants —
    the request may simply not have constrained the colour. Those must not share a
    monitoring task, or the task would compare two different products.
    """
    text = " ".join(
        str(part) for part in (offer.get("title"), offer.get("detail_name")) if part
    )
    return (_extract_colour(text), _extract_capacity(text))


def select_offer(result: dict[str, Any], index: int) -> dict[str, Any]:
    """Return the offer a user picked by 1-based index, or raise ValueError.

    Two things can make an offer unmonitorable, and both are refused explicitly
    rather than silently accepted:

    - it was excluded from comparison (no usable price, wrong currency, ...);
    - it is only `needs_confirmation`, i.e. we could not prove it is the requested
      product. Monitoring something we could not identify would send an alert about
      the wrong item.
    """
    offers = result.get("offers") or []
    if index < 1 or index > len(offers):
        raise ValueError(f"Choose a number between 1 and {len(offers)}.")
    offer = offers[index - 1]
    if offer.get("excluded_reason"):
        raise ValueError(
            f"Offer {index} cannot be monitored: {offer['excluded_reason']}."
        )
    # Strict: only an explicit `same` counts as confirmed. A missing match field is
    # an unverified candidate, never an implicit confirmation.
    if offer.get("match") != MATCH_SAME:
        raise ValueError(
            f"Offer {index} cannot be monitored: it could not be confirmed as the product "
            f"you asked for ({offer.get('match') or 'no match verdict'}). Only confirmed "
            "matches can be monitored."
        )
    return offer


def select_offers(result: dict[str, Any], indices: Iterable[int]) -> list[dict[str, Any]]:
    """Return several confirmed offers to monitor together in one task.

    Refuses the whole selection when it would mix products: different colours or
    capacities, a `needs_confirmation` offer, an excluded offer, or different
    currencies (they could not be compared against one threshold).
    """
    chosen: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for raw in indices:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{raw!r} is not an offer number.") from None
        if index in seen_indices:
            raise ValueError(f"Offer {index} was selected more than once.")
        seen_indices.add(index)
        chosen.append(select_offer(result, index))

    if not chosen:
        raise ValueError("Select at least one offer.")

    currencies = {offer.get("currency") for offer in chosen}
    if len(currencies) > 1:
        raise ValueError(
            "These offers are priced in different currencies "
            f"({', '.join(sorted(str(c) for c in currencies))}); they cannot share one threshold."
        )

    signatures = {_identity_signature(offer) for offer in chosen}
    if len(signatures) > 1:
        raise ValueError(
            "These offers are not the same product variant (different colour or capacity). "
            "Narrow the request, or monitor them in separate tasks."
        )
    return chosen

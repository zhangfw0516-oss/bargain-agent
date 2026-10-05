"""Find the same product at other merchants, within hard limits.

Google Shopping lists each merchant's offer as its own product, so "compare every
seller" means grouping listings ourselves. Guards, in order:

1. Candidates come only from listings already found (plus at most one extra search
   the caller may pass in).
2. Code pre-filter: same brand and model numbers as the chosen title, new (or the
   requested condition), same currency and not a converted overseas price, and a
   price within 50%–150% of the chosen one (drops accessories and bundles).
3. One listing per merchant (the cheapest).
4. The offer-verifier agent keeps only ``same``; when it is unavailable, only
   rule-passing trusted merchants are kept.
5. At most ``max_sources - 1`` matches (the chosen listing is the first source);
   trusted merchants first, and only they are pre-selected.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from agents.offer_verifier import SAME, UNVERIFIED
from pricing import normalise_merchant, merchant_trust

#: Words that describe a category or colour, not a specific product.
GENERIC = {
    "the", "and", "with", "for", "new", "nz", "buy", "sale", "black", "white", "silver", "grey", "gray", "blue",
    "red", "green", "pink", "gold", "smart", "watch", "band", "tracker", "fitness", "wireless", "bluetooth",
    "edition", "version", "case", "aluminium", "aluminum", "inch", "in",
}
MAX_VERIFY = 12
PRICE_BAND = (Decimal("0.5"), Decimal("1.5"))

Verifier = Callable[[dict[str, Any], list[dict[str, Any]]], Any]


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").casefold())


def key_tokens(title: str) -> tuple[str | None, set[str]]:
    """(brand, model-number tokens) of a product title."""
    words = tokens(monitor_query(title))
    brand = next((w for w in words if w not in GENERIC and not w.isdigit()), None)
    digits = [w for w in words if any(c.isdigit() for c in w)]
    return brand, set(digits[:3])


def monitor_query(title: str) -> str:
    """A short search query for a product title ('... at Harvey Norman' dropped)."""
    text = re.sub(r"\s+at\s+[A-Z][\w .&'-]*$", "", title or "")
    text = re.sub(r"[(\[].*?[)\]]", " ", text)
    words = text.replace("|", " ").replace(" - ", " ").split()
    return " ".join(words[:8])


def _price(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount.is_finite() and amount > 0 else None


def rule_candidates(chosen: dict[str, Any], pool: list[dict[str, Any]], need: dict[str, Any]) -> list[dict[str, Any]]:
    brand, digits = key_tokens(chosen.get("title") or "")
    base = _price(chosen.get("price"))
    wanted = need.get("condition")
    chosen_merchant = normalise_merchant(chosen.get("merchant") or "")
    best: dict[str, dict[str, Any]] = {}
    for item in pool:
        if not item.get("product_id") or item.get("product_id") == chosen.get("product_id"):
            continue
        price = _price(item.get("price"))
        if price is None or item.get("currency") != chosen.get("currency"):
            continue
        if "converted from" in (item.get("snippet") or ""):
            continue
        if item.get("condition") and item["condition"] != wanted:
            continue
        if base is not None and not (base * PRICE_BAND[0] <= price <= base * PRICE_BAND[1]):
            continue
        words = set(tokens(item.get("title") or ""))
        if brand and brand not in words:
            continue
        if not digits.issubset(words):
            continue
        merchant = normalise_merchant(item.get("merchant") or "")
        if not merchant or merchant == chosen_merchant:
            continue
        if merchant not in best or price < _price(best[merchant]["price"]):
            best[merchant] = item
    ranked = sorted(best.values(), key=lambda i: (not i.get("trusted"), _price(i["price"])))
    return ranked


def find_same_product(
    chosen: dict[str, Any],
    pool: list[dict[str, Any]],
    need: dict[str, Any] | None = None,
    *,
    verifier: Verifier | None,
    max_sources: int,
) -> list[dict[str, Any]]:
    need = need or {}
    limit = max(0, max_sources - 1)
    if limit == 0:
        return []
    for item in pool:
        item.setdefault("trusted", merchant_trust(item.get("merchant"), item.get("link")))
    candidates = rule_candidates(chosen, pool, need)[:MAX_VERIFY]
    if not candidates:
        return []
    verdicts: dict[str, str] = {}
    if verifier is not None:
        offers = [
            {"id": f"m{i}", "merchant": c.get("merchant"), "title": c.get("title"),
             "condition": c.get("condition") or "new/unknown", "price": c.get("price"), "currency": c.get("currency")}
            for i, c in enumerate(candidates)
        ]
        product = {
            "title": chosen.get("title"),
            "constraints": {k: need[k] for k in ("brand", "model", "colour") if need.get(k)},
            "condition": need.get("condition") or "new",
        }
        result = verifier(product, offers)
        verdicts = {f"m{i}": result.value.get(f"m{i}", (UNVERIFIED, None))[0] for i in range(len(candidates))}
    kept = []
    for i, cand in enumerate(candidates):
        verdict = verdicts.get(f"m{i}", UNVERIFIED)
        if verdict == SAME or (verdict == UNVERIFIED and cand.get("trusted")):
            kept.append({
                **{k: cand.get(k) for k in ("id", "title", "merchant", "price", "currency", "product_id", "page_token", "link", "trusted")},
                "verified": verdict == SAME,
                "preselect": bool(cand.get("trusted")),
            })
        if len(kept) >= limit:
            break
    return kept

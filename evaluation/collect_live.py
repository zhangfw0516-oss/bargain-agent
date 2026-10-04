"""Collect a real price-accuracy sample: Google Shopping price vs the merchant's page.

For each query: one SerpApi google_shopping search (pick the first new, NZD,
trusted-merchant listing), one google_immersive_product lookup (the merchant's page
link), then the merchant page is read with Firecrawl's schema extraction (falling
back to Scrape.do + the price-extractor agent). The two prices are compared.

    python -m evaluation.collect_live            # print the plan, make no calls
    python -m evaluation.collect_live --run      # make the calls (costs credits)

Results go to ``evaluation/live_results/<date>.json`` (no keys) and a summary is
printed. This measures real price agreement, the proposal's price-extraction metric.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from agents import price_extractor
from matching import key_tokens, tokens
from pricing import _norm_name, merchant_trust
from sources.base import PageText, ProductRef, ProviderError
from sources.firecrawl import Firecrawl
from sources.scrapedo import ScrapeDo
from sources.serpapi import SerpApi

OUT_DIR = Path(__file__).with_name("live_results")

QUERIES = [
    "Fitbit Charge 6", "Apple AirPods Pro 2", "Sony WH-1000XM5", "Samsung Galaxy Watch7", "Kindle Paperwhite",
    "JBL Flip 6", "Logitech MX Master 3S", "Nintendo Switch OLED", "Dyson V8 vacuum", "Philips Airfryer XXL",
    "Breville Barista Express", "Garmin Forerunner 265", "Bose QuietComfort Earbuds II", "Samsung T7 SSD 1TB",
    "Apple iPad 10th generation", "Xiaomi Smart Band 9", "Google Pixel Buds Pro 2",
]


def plan(n: int) -> dict[str, int]:
    return {"queries": n, "serpapi_calls": 2 * n, "firecrawl_pages_max": n, "scrapedo_pages_max": n}


def _pick(results) -> Any:
    for r in results:
        if r.product_id and r.page_token and r.price and r.currency == "NZD" and not r.condition \
                and "converted from" not in (r.snippet or "") and merchant_trust(r.merchant, r.link):
            return r
    return None


def _store_link(offers, merchant: str) -> tuple[str | None, str | None]:
    want = _norm_name(merchant or "")
    for o in offers:
        if o.link and _norm_name(o.merchant) == want:
            return o.link, o.price
    first = next((o for o in offers if o.link), None)
    return (first.link, first.price) if first else (None, None)


def _read_page(url: str, firecrawl: Firecrawl, scrapedo: ScrapeDo) -> tuple[dict | None, str, str | None]:
    errors = []
    for reader in (scrapedo, firecrawl):
        if not reader.is_configured():
            continue
        try:
            page = reader.extract(url)
            data = price_extractor.run(page.text, url=url) if isinstance(page, PageText) else page
            return data, reader.name, None
        except (ProviderError, price_extractor.PriceExtractionError, ValueError) as exc:
            errors.append(f"{reader.name}: {exc}")
    return None, "", "; ".join(errors) or "no page reader configured"


def collect(queries: list[str]) -> dict[str, Any]:
    serp, firecrawl, scrapedo = SerpApi(), Firecrawl(), ScrapeDo()
    rows = []
    for query in queries:
        row: dict[str, Any] = {"query": query}
        try:
            listing = _pick(serp.search(query, limit=40), query)
            if listing is None:
                row["error"] = "no new NZD listing from a trusted merchant"
                rows.append(row)
                continue
            row.update(merchant=listing.merchant, title=listing.title, google_price=listing.price)
            offers, _ = serp.offers(ProductRef(listing.product_id, listing.page_token, None, listing.title))
            link, store_price = _store_link(offers, listing.merchant)
            row.update(store_link=link, store_list_price=store_price)
            if not link:
                row["error"] = "no merchant page link"
                rows.append(row)
                continue
            data, reader, error = _read_page(link, firecrawl, scrapedo)
            _compare(row, data, reader, error)
        except ProviderError as exc:
            row["error"] = str(exc)
        rows.append(row)
        print(f"{query}: {row.get('merchant')} google={row.get('google_price')} page={row.get('page_price')} "
              f"{'OK' if row.get('exact_match') else row.get('error') or 'MISMATCH'}", flush=True)
    return summarise(rows, serp.calls)


def _compare(row: dict[str, Any], data: dict[str, Any] | None, reader: str, error: str | None) -> None:
    row.update(reader=reader)
    for key in ("page_price", "page_currency", "page_in_stock", "exact_match", "within_1pct", "error"):
        row.pop(key, None)
    if data is None:
        row["error"] = error
        return
    row.update(page_price=data["price"], page_currency=data["currency"], page_in_stock=data["in_stock"])
    g, pg = Decimal(row["google_price"]), Decimal(data["price"])
    row["exact_match"] = data["currency"] == "NZD" and g == pg
    row["within_1pct"] = data["currency"] == "NZD" and abs(g - pg) <= g * Decimal("0.01")


def recheck(path: Path) -> dict[str, Any]:
    """Re-read the merchant pages of a saved run (no SerpApi calls)."""
    previous = json.loads(path.read_text(encoding="utf-8"))
    firecrawl, scrapedo = Firecrawl(), ScrapeDo()
    rows = previous["rows"]
    for row in rows:
        if not row.get("store_link") or not row.get("google_price"):
            continue
        data, reader, error = _read_page(row["store_link"], firecrawl, scrapedo)
        _compare(row, data, reader, error)
        print(f"{row['query']}: google={row['google_price']} page={row.get('page_price')} "
              f"{'OK' if row.get('exact_match') else row.get('error') or 'MISMATCH'}", flush=True)
    report = summarise(rows, 0)
    report["rechecked_from"] = path.name
    return report


def summarise(rows: list[dict[str, Any]], serpapi_calls: int = 0) -> dict[str, Any]:
    compared = [r for r in rows if "exact_match" in r]
    merchants = sorted({r["merchant"] for r in compared})
    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "queries": len(rows),
        "compared": len(compared),
        "exact_match_rate": round(sum(r["exact_match"] for r in compared) / len(compared), 4) if compared else None,
        "within_1pct_rate": round(sum(r["within_1pct"] for r in compared) / len(compared), 4) if compared else None,
        "merchants": merchants,
        "serpapi_calls": serpapi_calls,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.collect_live")
    parser.add_argument("--run", action="store_true", help="Make the real calls (costs credits).")
    parser.add_argument("--limit", type=int, default=len(QUERIES))
    parser.add_argument("--recheck", type=Path, default=None,
                        help="Re-read the merchant pages of a saved run (page readers only, no SerpApi).")
    args = parser.parse_args(argv)
    if args.recheck:
        report = recheck(args.recheck)
        out = args.recheck.with_name(args.recheck.stem + "-recheck.json")
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=1))
        print("Saved", out)
        return 0
    queries = QUERIES[: max(1, args.limit)]
    print("Planned calls:", plan(len(queries)))
    if not args.run:
        print("Dry run: no calls made. Add --run to collect.")
        return 0
    report = collect(queries)
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{date.today().isoformat()}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=1))
    print("Saved", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

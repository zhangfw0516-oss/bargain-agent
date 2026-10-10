"""Build demo.db: a few monitors with 30 days of price history, for presentations.

    venv\\Scripts\\python.exe -m demo.seed_demo            # reuse demo/listings.json
    venv\\Scripts\\python.exe -m demo.seed_demo --refresh  # search again (4 Serper searches)

The products, merchants, Google product ids and the latest prices are real (one
Google Shopping search per product, saved in demo/listings.json). The earlier price
points are generated around those real prices, so the history chart and the 30-day
insight have something to show without weeks of monitoring. The web UI marks the
demo database with a banner (``webapp.py --demo``). "Check now" still checks live.
"""

from __future__ import annotations

import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import pricing  # noqa: E402
import storage  # noqa: E402

DEMO_DB = ROOT / "demo.db"
LISTINGS = Path(__file__).with_name("listings.json")
DAYS = 30
EVERY_DAYS = 2          # 15 checks, so every merchant fits in the detail view's history
MAX_MERCHANTS = 3

PRODUCTS = [
    {"query": "Fitbit Charge 6", "name": "Fitbit Charge 6 Fitness Tracker",
     "must": ["fitbit", "charge 6"], "exclude": ["band", "strap", "case", "protector", "charger", "cable"]},
    {"query": "Philips 3000 Series Airfryer", "name": "Philips 3000 Series Airfryer",
     "must": ["philips", "3000"], "exclude": ["basket", "liner", "accessor", "kit", "paper"]},
    {"query": "Kindle Paperwhite 16GB", "name": "Amazon Kindle Paperwhite 16GB",
     "must": ["kindle", "paperwhite"], "exclude": ["signature", "kids", "case", "cover", "protector", "sleeve"]},
    {"query": "Sony WH-1000XM5", "name": "Sony WH-1000XM5 Noise Cancelling Headphones",
     "must": ["sony", "xm5"], "exclude": ["case", "pad", "cushion", "cable", "stand", "refurb"]},
]


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fetch_listings() -> dict:
    providers = pricing_providers()
    out = {}
    for spec in PRODUCTS:
        results = providers.shopping.search(spec["query"], limit=40)
        picked: dict[str, dict] = {}
        for r in results:
            title = (r.title or "").lower()
            if not r.price or r.currency != "NZD" or r.condition or not r.product_id:
                continue
            if not all(word in title for word in spec["must"]) or any(word in title for word in spec["exclude"]):
                continue
            if not pricing.merchant_trust(r.merchant, r.link):
                continue
            key = pricing.normalise_merchant(r.merchant or "")
            if key not in picked or Decimal(r.price) < Decimal(picked[key]["price"]):
                picked[key] = {"title": r.title, "merchant": r.merchant, "price": r.price, "link": r.link,
                               "product_id": r.product_id}
        rows = sorted(picked.values(), key=lambda x: Decimal(x["price"]))
        if rows:  # same product, not a bundle or a different size: within 25% of the cheapest
            low = Decimal(rows[0]["price"])
            rows = [x for x in rows if Decimal(x["price"]) <= low * Decimal("1.25")][:MAX_MERCHANTS]
        out[spec["query"]] = rows
        print(f"{spec['query']}: " + (", ".join(f"{x['merchant']} {x['price']}" for x in rows) or "no trusted listing"))
    return {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "products": out}


def pricing_providers():
    from sources.registry import build_providers

    real_db = config.database_path()  # the searches count against the normal daily limit
    storage.init_db(real_db)
    return build_providers(before_call=pricing.quota_guard(real_db))


def history(current: Decimal, rng: random.Random) -> list[Decimal]:
    """Price at each check, oldest first, ending at the real current price."""
    regular = current * Decimal(str(rng.uniform(1.06, 1.14)))
    sale = current * Decimal(str(rng.uniform(0.95, 0.99)))
    checks = DAYS // EVERY_DAYS
    sale_start, sale_end = checks // 3, checks // 3 + 3
    prices = []
    for i in range(checks):
        if i >= checks - 3:
            price = current
        elif sale_start <= i < sale_end:
            price = sale
        else:
            price = regular * Decimal(str(rng.uniform(0.985, 1.0)))
        prices.append(Decimal(round(price)) - Decimal("0.01") if price > 50 else price)
    prices[-3:] = [current] * 3
    return prices


def seed(listings: dict) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(DEMO_DB) + suffix).unlink(missing_ok=True)
    storage.init_db(DEMO_DB)
    email = config._get("DEFAULT_NOTIFY_EMAIL") or "demo@example.com"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    last_check = now - timedelta(hours=3)
    times = [last_check - timedelta(days=EVERY_DAYS * k) for k in range(DAYS // EVERY_DAYS)][::-1]
    for n, spec in enumerate(PRODUCTS):
        rows = listings["products"].get(spec["query"]) or []
        if not rows:
            continue
        best = Decimal(rows[0]["price"])
        target = money(Decimal(int(best * Decimal("0.92"))))
        sources = [{"url": r["link"], "retailer": r["merchant"], "source_kind": storage.SOURCE_SERPAPI,
                    "product_id": r["product_id"], "search_query": spec["query"]} for r in rows]
        task_id = storage.create_task_with_sources({
            "product_name": spec["name"], "url": rows[0]["link"], "target_price": target, "currency": "NZD",
            "comparison": "lte", "frequency_minutes": 1440, "notify_email": email, "status": "active",
            "condition": "new", "next_check_at": (last_check + timedelta(days=1)).isoformat(),
            "identity": {"confirmed_title": rows[0]["title"]},
        }, sources, db_path=DEMO_DB)
        db_sources = storage.list_task_sources(task_id, db_path=DEMO_DB)
        series = {s["id"]: history(Decimal(r["price"]), random.Random(f"{spec['query']}|{r['merchant']}"))
                  for s, r in zip(db_sources, rows)}
        last_quote = None
        for i, at in enumerate(times):
            run = uuid.uuid4().hex[:12]
            for s, r in zip(db_sources, rows):
                last_quote = storage.save_quote(task_id, {
                    "fetched_at": (at + timedelta(seconds=db_sources.index(s))).isoformat(),
                    "price": money(series[s["id"]][i]), "currency": "NZD", "in_stock": True,
                    "product_name": r["title"], "retailer": r["merchant"], "url": r["link"],
                    "identity_verdict": "same", "check_run_id": run, "trusted": True,
                }, db_path=DEMO_DB, source_id=s["id"])
        with storage._transaction(DEMO_DB) as conn:
            conn.execute("UPDATE tasks SET last_check_at = ?, last_quote_id = ? WHERE id = ?",
                         (last_check.isoformat(), last_quote, task_id))
            conn.execute("UPDATE task_sources SET last_checked_at = ? WHERE task_id = ?",
                         (last_check.isoformat(), task_id))
        print(f"monitor {task_id}: {spec['name']} · {len(rows)} merchant(s) · target {target}")
    print(f"Demo database: {DEMO_DB}")


def main() -> int:
    if "--refresh" in sys.argv[1:] or not LISTINGS.exists():
        LISTINGS.write_text(json.dumps(fetch_listings(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    seed(json.loads(LISTINGS.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

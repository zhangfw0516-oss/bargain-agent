# Evaluation

The proposal's three accuracy dimensions: price extraction, instruction parsing and notification triggering.

- Offline suite: `python -m evaluation` (`evaluation/runner.py`, cases in `evaluation/datasets.py`).
- Real model: `python -m evaluation --live`.
- Real prices: `python -m evaluation.collect_live` (Google Shopping vs merchant page).
- Raw results: `evaluation/live_results/` (no keys).

## 1. Real price accuracy (2026-10-05)

`collect_live.py`: 17 queries for common NZ products. For each one it took the first new, NZD, trusted-merchant listing (1 SerpApi search), got the merchant page link from Google's product page (1 SerpApi immersive lookup), read the merchant page, and compared the page price with Google Shopping's price.

| Run | Page reading | Compared | Exact match | Within 1% |
| --- | --- | --- | --- | --- |
| First (`2026-10-05.json`) | Firecrawl JSON extraction | 15 | **9 (60%)** | 60% |
| Re-read after fixes (`2026-10-05-recheck.json`) | Scrape.do: JSON-LD in code, else AI on page text | 14 | **14 (100%)** | 100% |

Merchants: PB Tech, Noel Leeming, Heathcotes, MightyApe (including Marketplace sellers) and Harvey Norman. 2 queries had no suitable trusted listing. Cost: 32 SerpApi calls, 30 Firecrawl pages, about 15 Scrape.do requests.

Why the first run missed 6, and what changed:

| Cause | Example | Fix |
| --- | --- | --- |
| Superscript cents read as one number | Noel Leeming "$ 448 00" read as 44800 | Use the page's JSON-LD offer price when there is exactly one; check that an AI price is written on the page |
| Instalment taken as the price | Heathcotes Dyson: 424 ÷ 4 = 106 (Afterpay) | Prompts exclude instalment/finance amounts |
| RRP/"was" price taken | Heathcotes Breville 2799 vs 2595 | Prompts exclude was/RRP prices; JSON-LD has the selling price |
| Text read of marketplace pages | MightyApe Marketplace items $2 below Google | JSON-LD matches Google exactly; the $2 was an extraction artefact |
| Price before GST | PB Tech "$172.17 +GST / $198.00 inc GST" | Prompts ask for the tax-inclusive price |

In the re-read, Harvey Norman's page had no usable JSON-LD and Scrape.do text gave no clear price. In normal use, Firecrawl is the next reader in the chain, and in the first run it read $220 correctly. The default extractor order is now `scrapedo, firecrawl, tavily`.

## 2. Real model (`--live`, deepseek-flash, 2026-10-05)

| Track | Cases | Result |
| --- | --- | --- |
| Page price extraction (agent on page text) | 13 | 13/13 exact; **false-price rate 0** (the injection, no-price, RRP-range and ex-GST cases all handled) |
| Instruction parsing (task-parser agent) | 16 | 15/16 full match; 98.1% of fields correct |
| Offer price parsing (code) | 26 | 100% |
| Notification triggers (real scheduler) | 10 scenarios / 19 steps | precision 1.0, recall 1.0, **unexpected-send rate 0** |

Notes:
- The remaining instruction miss (i07) has only a URL and no product name. The model asked for clarification, so no task was created.
- Cases where a task must not be created (invented URL, 1-minute frequency, EUR) count as correct for any non-ready outcome: rejected by validation, a clarification question, or declined.
- The first live run found two dataset problems (an instruction without a product name, and that "rejected"-only rule) plus one real prompt problem. A bare "$" on an NZ page was refused; the price agent now gets the page URL and treats "$" on a .nz site as NZD.

## 3. End-to-end with a real email (2026-10-05)

Real Find for "Fitbit Charge 6 运动手环，300纽币以内，不要翻新" → recommendations from PB Tech ($198) and Harvey Norman ($220). Harvey Norman was confirmed as the same product by the verifier and pre-selected, so one task watched 2 merchants.

- Check 1: the deal triggered (PB Tech $198 ≤ $218); **1 email accepted by SMTP**.
- Check 2: still a deal, no second email.
- Cost: 6 Serper searches.

Whether the email arrived still has to be confirmed in the recipient's inbox.

## Offline suite

Model answers are replayed through the real agents and validators, so this measures validation and decision logic, not model quality. All tracks pass.

## Still to do

- The proposal asks for 20+ products: extend `QUERIES` (the relevance filter now avoids accessories) when the SerpApi quota allows.
- A one-week continuous monitoring run with real notifications.
- Extend the instruction set towards the proposal's 50+ commands.

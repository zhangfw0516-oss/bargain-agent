# Known issues

Current as of 2026-10-04 (refactor branch `refactor/serpapi-ai-pipeline`).

## Live verification (2026-10-04)

Verified with real calls (scrubbed recordings in `tests/fixtures/serpapi/*recorded*`, `tests/fixtures/firecrawl/*recorded*`):

- SerpApi `google_shopping` (gl=nz) and `google_immersive_product` work; **`google_product` is retired by Google** and is no longer used.
- Most Shopping results are single-seller listings; the immersive page for them lists one store, so "all sellers" is often one or a few.
- Firecrawl search and JSON scrape work. Its extractor first returned the **ex-GST** price on a PB Tech page; both prompts now ask for the tax-inclusive price and returned NZ$198.00.
- Scrape.do fetch works; text + JSON-LD through the price agent returned NZ$198.00.

- A real Find run (deepseek-flash) passed all five stages after capping the select input at 30 pre-ranked listings (60 listings had truncated its answer).

- 2026-10-05: Serper verified (same productIds as SerpApi); real multi-merchant Find → task → one email → no duplicate; real price sample 14/14 exact after the JSON-LD fix; `--live` evaluation 13/13 page prices, 15/16 instructions (see `docs/EVALUATION.md`).

Still unverified: Tavily Extract; inbox arrival of the test email; a week-long monitoring run.

## Behaviour to be aware of

- Monitoring runs only while something runs it: the web UI monitor, `main.py run`, or `check-due.bat` from Windows Task Scheduler (see README). A sleeping computer misses checks; the next run catches up.
- A drop-percent alert compares with the reference price (first check, then each alert's price); a slow decline triggers once it accumulates past the percentage.

- Most Google Shopping results are one merchant's listing; multi-merchant comparison comes from same-product matching (at most `MONITOR_MAX_SOURCES`, default 5) and one shared search per check.
- Serper results carry no shipping, store link or second-hand field (condition is read from the title); shipping appears only when SerpApi detail is available.
- A merchant listing that drops out of the shared search is looked up on SerpApi at most twice per check; otherwise it is a source error for that check.
- Harvey Norman pages have no usable JSON-LD for Scrape.do; Firecrawl (next in the chain) reads them.

- Google Shopping offers carry no stock field (stores say e.g. "In stock online"); an offer is treated as in stock unless its details say out of stock / sold out.
- A bare `$` is read as NZD only in Google Shopping results for `gl=nz`; on product pages a bare `$` without a stated currency is not a usable price.
- With `ALERT_TRUSTED_ONLY=true`, a product source whose only sellers are unverified never alerts and never resets its episode.
- Product tasks check at most every `MONITOR_MIN_FREQUENCY_MINUTES` (default 360) to protect the SerpApi quota; existing tasks keep their stored frequency but the floor applies when the next check is scheduled.
- Legacy tasks migrated from retailer/web-search sources become `url` sources and need a page extractor key.
- Exactly-once email is not guaranteed if the process dies after SMTP accepts a message and before the event is marked sent (same as before the refactor); `release-send` is the manual recovery.

## Not done

- Real-service evaluation (20+ products / 3+ retailers, one-week monitoring) — see `docs/EVALUATION.md`.
- MCP-based providers (the provider protocols allow adding them later).

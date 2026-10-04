# Known issues

Current as of 2026-10-04 (refactor branch `refactor/serpapi-ai-pipeline`). The pre-refactor list is in `archive/project_progress/KNOWN_ISSUES.md`.

## Live verification (2026-10-04)

Verified with real calls (scrubbed recordings in `tests/fixtures/serpapi/*recorded*`, `tests/fixtures/firecrawl/*recorded*`):

- SerpApi `google_shopping` (gl=nz) and `google_immersive_product` work; **`google_product` is retired by Google** and is no longer used.
- Most Shopping results are single-seller listings; the immersive page for them lists one store, so "all sellers" is often one or a few.
- Firecrawl search and JSON scrape work. Its extractor first returned the **ex-GST** price on a PB Tech page; both prompts now ask for the tax-inclusive price and returned NZ$198.00.
- Scrape.do fetch works; text + JSON-LD through the price agent returned NZ$198.00.

Still unverified: Tavily Extract (no current key tested), the understand/rewrite/select/describe/verify prompts on real model output (run `python -m evaluation --live` and real Find runs).

## Behaviour to be aware of

- Google Shopping offers carry no stock field (stores say e.g. "In stock online"); an offer is treated as in stock unless its details say out of stock / sold out.
- A bare `$` is read as NZD only in Google Shopping results for `gl=nz`; on product pages a bare `$` without a stated currency is not a usable price.
- With `ALERT_TRUSTED_ONLY=true`, a product source whose only sellers are unverified never alerts and never resets its episode.
- Product tasks check at most every `MONITOR_MIN_FREQUENCY_MINUTES` (default 360) to protect the SerpApi quota; existing tasks keep their stored frequency but the floor applies when the next check is scheduled.
- Legacy tasks migrated from retailer/web-search sources become `url` sources and need a page extractor key.
- Exactly-once email is not guaranteed if the process dies after SMTP accepts a message and before the event is marked sent (same as before the refactor); `release-send` is the manual recovery.

## Not done

- Real-service evaluation (20+ products / 3+ retailers, one-week monitoring) — see `docs/EVALUATION.md`.
- MCP-based providers (the provider protocols allow adding them later).

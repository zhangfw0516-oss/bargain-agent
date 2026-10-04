# Known issues

Current as of 2026-10-04 (refactor branch `refactor/serpapi-ai-pipeline`). The pre-refactor list is in `archive/project_progress/KNOWN_ISSUES.md`.

## Needs live verification

- **SerpApi response shapes are unverified.** `tests/fixtures/serpapi/*.json` were written from SerpApi documentation, not recorded. Before relying on monitoring, run one `google_shopping`, one `google_immersive_product` and one `google_product` request for an NZ product, scrub the key, replace the fixtures and fix any parser drift in `sources/serpapi.py`.
- **Which offers endpoint works for NZ** (`google_immersive_product` by page token vs `google_product` by id) is not confirmed; `SerpApi.offers` tries both and re-searches by product id.
- **Tavily Extract and Firecrawl request/response shapes** (`/extract`, `/v1/search`, `/v1/scrape` with `formats: ["json"]`) are also documented-not-recorded.
- **Prompts for the new agents** (`understand`, `rewrite`, `select`, `describe`, `verify_offers`) have only been exercised with replayed answers. Run `python -m evaluation --live` and a few real Find runs.

## Behaviour to be aware of

- Google Shopping offers carry no stock field; an offer is treated as in stock unless its details say out of stock / sold out.
- A bare `$` is read as NZD only in Google Shopping results for `gl=nz`; on product pages a bare `$` without a stated currency is not a usable price.
- With `ALERT_TRUSTED_ONLY=true`, a product source whose only sellers are unverified never alerts and never resets its episode.
- Product tasks check at most every `MONITOR_MIN_FREQUENCY_MINUTES` (default 360) to protect the SerpApi quota; existing tasks keep their stored frequency but the floor applies when the next check is scheduled.
- Legacy tasks migrated from retailer/web-search sources become `url` sources and need a page extractor key.
- Exactly-once email is not guaranteed if the process dies after SMTP accepts a message and before the event is marked sent (same as before the refactor); `release-send` is the manual recovery.

## Not done

- Real-service evaluation (20+ products / 3+ retailers, one-week monitoring) — see `docs/EVALUATION.md`.
- MCP-based providers (the provider protocols allow adding them later).

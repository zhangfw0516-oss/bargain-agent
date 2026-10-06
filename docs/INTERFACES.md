# Module interfaces

Current as of the SerpApi / multi-agent refactor (2026-10-04). The pre-refactor version is in `archive/docs/pre-refactor/`.

## LLM — `llm.py`

| Function | Contract |
| --- | --- |
| `call_json(prompt_name, payload, *, validate=None, agent=None, client=None, model=None, retries=1)` | Loads `prompts/<prompt_name>`, sends `payload` (dicts as JSON) with `response_format=json_object`, parses JSON, runs `validate(data)`. Empty, truncated, non-JSON or invalid output is retried with the error fed back. Raises `LLMError` (never contains secrets). |
| `make_client(agent=None) -> (client, model)` | Uses `config.llm_settings(agent)`: `LLM_*_<AGENT>` overrides, else global `LLM_*`. |
| `is_configured(agent=None) -> bool` | All three settings present. |

## Agents — `agents/`

Every agent takes `opts: LLMOptions(client, model)` (tests inject fakes) and never fetches, writes or sends.

| Agent | Call | Output | On model failure |
| --- | --- | --- | --- |
| understand | `run(description)` | `AgentResult(need dict)`: category, brand, model, key_specs, budget, currency, colour, use_case, condition, exclusions | `{}` / `fallback` |
| rewrite | `run(description, need, *, max_queries, previous_queries=None, feedback=None)` | `AgentResult([query])` — no search operators | keyword query from need/description |
| select | `run(description, need, evidence)` | `AgentResult([evidence id])` — only server ids | priced, new, trusted first |
| describe | `run(description, need, recommendations)` | `AgentResult({id: 中文简介})` | template text |
| task_parser | `run(instruction) -> dict` | `schemas.validate_task` output | raises `TaskParseError` |
| price_extractor | `run(page_text) -> dict` | `schemas.validate_price_extraction` output | raises `PriceExtractionError` (also for "no clear price") |
| offer_verifier | `run(product, offers)` | `AgentResult({offer id: (same/different/needs_confirmation/unverified, reason)})` | all `unverified` |
| deal_summary | `run(task, quote) -> str or None` | email summary | `None` |

## Find pipeline — `pipeline.py`

`run_find(description, *, deps=None, constraints=None) -> dict` with keys `status` (`ready`/`partial`), `description`, `need`, `queries`, `recommendations`, `evidence`, `stages`, `notes`, `usage`.

A recommendation: `id, title, link, merchant, price, currency, price_source (serpapi/page_extract/None), product_id, page_token, thumbnail, condition, provider, trusted, search_query, monitorable, summary`.

`FindDeps(shopping, supplement, limits: FindLimits, clock, llm_for)` injects providers, limits and per-agent LLM options. `build_recommendations(evidence, order, need, constraints, limit)` applies the code-owned vetoes (budget in the same currency, unrequested second-hand, duplicate product ids).

## Providers — `sources/`

| Protocol | Method | Implementations |
| --- | --- | --- |
| `ShoppingSearch` | `search(query, *, limit) -> [ShoppingResult]` | `SerpApi` (google_shopping), `Firecrawl` (supplement; carries `page_text`, no price) |
| `OfferSource` | `offers(ProductRef) -> ([Offer], ProductRef)` | `SerpApi` (google_immersive_product by page token; on failure re-search by product id; the listing itself when Google shows no store list — `google_product` was retired by Google) |
| `PageExtractor` | `extract(url) -> PageText or validated price dict` | `Firecrawl` (JSON-schema scrape), `TavilyExtract`, `ScrapeDo` (raw HTML → text + JSON-LD) |

Errors: `NotConfigured`, `AuthError`, `RateLimited`, `QuotaExceeded`, `ProviderTimeout`, `Unavailable` (all `ProviderError`). `request_json` maps HTTP status and rebuilds transport errors without URLs. `registry.build_providers(before_call)` assembles configured providers; `before_call(name)` runs before each billable call.

## Matching — `matching.py`

`find_same_product(chosen, pool, need, *, verifier, max_sources) -> [match]` — same product at other merchants: rule pre-filter (`rule_candidates`), one listing per merchant, verifier veto, capped at `max_sources - 1`; each match has `id, title, merchant, price, currency, product_id, page_token, link, trusted, verified, preselect`. `monitor_query(title)` builds the shared search query.

## Pricing — `pricing.py`

- `observe_source(source, task, *, providers, db_path, llm_opts, context) -> [quote]` — raises `ObservationError`. Product sources read their listing from one shared search per check (`CheckContext`), then at most two SerpApi detail lookups.
- `quota_guard(db_path)` — counts calls in `api_usage`; SerpApi limited by `SERPAPI_DAILY_MAX_CALLS`.
- `merchant_trust(merchant, link, trusted=None)`, `is_safe_public_url(url)`, `host_of(url)`.

Quote dict: `product_name, retailer, url, price, currency, in_stock, condition, fetched_at, trusted, error` (+ set by the scheduler: `source_id, source_kind, check_run_id, identity_verdict, quote_id`).

## Scheduler — `scheduler.py`

`check_task(task, *, observer=None, fetcher=None, verifier=_DEFAULT, send_fn=notifier.notify_deal, max_retries=None, db_path=None, should_abort=None)`

1. Task lock (`storage.acquire_task_lock`), skip if not active or no enabled sources.
2. Observe every enabled source (one source may yield many seller quotes).
3. Re-read the task; `assign_identity_verdicts` (condition pre-filter, `url` sources `same`, product offers `same` unless the verifier downgrades; cached in `offer_verdicts`).
4. Best = cheapest comparable, in-stock, `same`/`unverified`, trusted (if `ALERT_TRUSTED_ONLY`) quote.
5. Deal (price at or below the target, or `drop_percent` below the task's reference price) → `try_begin_send(config_version, source_id)` → one email per episode; after a drop alert the reference becomes the alerted price. Otherwise reset the episode only when `_reset_blocker` returns `None`.
6. Next check after `frequency_minutes` (at least `MONITOR_MIN_FREQUENCY_MINUTES` for product sources).

`TaskScheduler` runs once per process; `_perform_check` is the override point. `check_due_tasks(db_path)` is the one-shot used by `main.py check-due` (Windows Task Scheduler).

## Storage — `storage.py` (schema version 1)

Tables: `tasks`, `task_sources` (`source_kind` = `serpapi_product`/`url`, `product_id`, `page_token`, `search_query`, `consecutive_empty`), `quotes` (+ `condition`, `identity_verdict`, `check_run_id`, `trusted`), `notification_events`, `task_locks`, `offer_verdicts`, `api_usage`. `init_db` is idempotent; the first v1 migration backs up an existing database to `<db>.pre-v1.bak` and turns legacy `retailer_adapter`/`web_search` sources into `url` sources.

## Web API — `webapp.py`

All POSTs need matching `Host`, `Origin`, `csrftoken` cookie and `X-CSRF-Token`; bodies ≤ 64 KiB.

| Route | Purpose |
| --- | --- |
| `GET /api/tasks` | tasks with latest seller quotes, monitor status, email warning, today's API usage |
| `GET /api/tasks/{id}`, `/history` | detail, history, events |
| `GET /api/setup/status` | missing keys only, provider status |
| `POST /api/find` `{description, async?}` | run the pipeline; returns `token` + recommendations, or `{job}` with `async: true` (no writes) |
| `GET /api/find/status?job=` | stages reached so far, then the find response |
| `POST /api/find/refine` `{token, message, async?}` | refine an earlier find ("only black"): keeps its listings, ≤ 2 new searches, agents re-select; returns a new token and `followups` |
| `POST /api/tasks/{id}/sources/shipping` `{source_id, shipping}` | set or clear (`""`) a merchant's shipping cost |
| `POST /api/find/preview` `{token, rec_id}` | the product's sellers + `matches` at other merchants (≤ 1 extra search), suggested target (no writes) |
| `POST /api/find/confirm` `{token, rec_id, target_price, comparison, currency, frequency_minutes, extra_ids, drop_percent}` | creates the task with the chosen listing plus selected matches (capped); idempotent per token + rec |
| `POST /api/tasks/add` `{instruction}` or explicit fields | direct URL task |
| `POST /api/tasks/{id}/check` `{confirm: true}` | check now (may email) |
| `POST /api/check/due` `{confirm: true}` | check all due tasks |
| `POST /api/tasks/{id}/pause|resume|stop|modify` | lifecycle / conditions |
| `POST /api/tasks/{id}/sources/enable|disable` `{source_id}` | source toggle (ownership checked) |
| `POST /api/monitor/start` `{confirm: true}`, `/stop` | continuous monitoring (once per process) |
| `POST /api/setup` `{DEFAULT_NOTIFY_EMAIL}` | the only setting writable from the page |

`Services` injects `find`, `product_offers`, `observer`, `verifier`, `send_fn`, `parse_task`, `provider_status`.

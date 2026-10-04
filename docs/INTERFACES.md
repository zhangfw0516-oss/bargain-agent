# Bargain Agent — Module Interfaces

This document describes the public interfaces between the components of the M40
monitoring foundation. All money values are passed as decimal strings; timestamps
are UTC ISO-8601 strings.

## `config` — Environment configuration

Loads settings from `.env`. Raises `ConfigError` with actionable guidance when a
required value is missing or invalid. Numeric values must be finite and positive;
size and retry values must be within sane bounds.

Key functions:

- `llm_api_key() -> str`, `llm_base_url() -> str`, `llm_model() -> str`, `llm_timeout() -> float`
- `smtp_host() -> str`, `smtp_port() -> int`, `smtp_user() -> str`, `smtp_password() -> str`
- `smtp_from() -> str` (defaults to SMTP_USER)
- `smtp_timeout() -> float`
- `notification_max_retries() -> int` (0–20)
- `database_path() -> Path`
- `allowed_scraper_domains() -> list[str]` (default: `["pbtech.co.nz"]`)
- `scraper_timeout() -> float` (also the **total** fetch deadline)
- `scraper_max_response_bytes() -> int` (1–100,000,000)

## `schemas` — Validation

- `validate_task(data: dict, instruction: str) -> dict`
  Validates an LLM parser response against schema v1. The URL must appear literally
  in the instruction (matched with printable ASCII only, so CJK punctuation after a
  URL does not break extraction). Missing required fields downgrade the status to
  `needs_clarification`. Raises `ValueError` on structurally invalid fields.

- `validate_search_request(data: dict, instruction: str) -> dict`
  Validates schema v2 — a **search** request, deliberately a different shape from a
  monitor task. It does not require a URL (finding one is the point of a search), it
  requires `product_name` and `frequency_minutes`, and it refuses a `colour`/`capacity`
  the user never stated. A budget without a currency becomes a clarification. A search
  request is never permission to monitor anything.

- `validate_quote(quote: dict) -> dict`
  Requires a finite, **positive** decimal price, a supported currency, a fetch
  timestamp and an `in_stock` value in `{True, False, None}`. Used inside the scrape
  path so an unusable quote cannot reach comparison.

Supported currencies: `NZD`, `AUD`, `USD`. Comparisons: `lt` (below), `lte` (at most).

## `agent` — LLM instruction parsing

- `parse_user_instruction(user_instruction: str, *, client=None, model: str | None = None) -> dict`
  Sends the instruction to the configured OpenAI-compatible chat completions API
  (DeepSeek-compatible) with `response_format={"type":"json_object"}`. Injects
  `client` and `model` for offline testing. Empty or truncated responses are retried
  at most twice; schema/validation failures raise immediately. Raises `ParserError`
  on empty input, missing config, API failure or validation failure.

- `summarise_parsed_task(task: dict) -> str`
  Returns a human-readable summary for CLI confirmation.

## `scraper` — Price fetching

- `fetch_quote(url: str) -> dict`
  Validates the URL (scheme, host, port, allow-list, resolved IPs), then fetches with
  **automatic redirects disabled**, following `Location` headers manually up to 5 hops
  and re-validating every hop before sending a request.

  A monotonic-clock deadline covers DNS admission, DNS resolution, redirects and the
  download. DNS goes through `BoundedDnsResolver`, a fixed-size pool
  (`max_concurrent=4`): admission waits count against the deadline, a still-running
  lookup keeps its slot instead of being replaced, and a saturated pool fails lookups
  inside their deadline instead of queueing. The number of live resolver threads is
  therefore hard-bounded.

  Product selection is three-state: **none** (no JSON-LD product → a self-contained
  OpenGraph fallback with `in_stock = None`), **unique**, or **ambiguous** (refused).
  When a JSON-LD product is selected, the quote is built exclusively from it — missing
  price or currency fails the fetch rather than being filled in from unrelated page
  metadata. Several offers are accepted only when every identity-relevant field
  (price, currency, availability, condition, seller, SKU, MPN) matches.

  Raises `ScraperError` on unsafe targets, ambiguous identity, non-equivalent offers,
  incomplete JSON-LD quotes, size/deadline violations, and missing data.

Quote dict (as returned by `validate_quote`):

```json
{
  "product_name": "...",
  "variant": null,
  "url": "...",
  "retailer": "pbtech.co.nz",
  "price": "319.00",
  "currency": "NZD",
  "in_stock": true,
  "fetched_at": "2026-09-23T04:38:00+00:00",
  "shipping_cost": null,
  "error": null
}
```

## `retailers` — Per-shop adapters (Stage B)

One adapter per supported shop. Retailer-specific page parsing lives here and nowhere
else; the scheduler, the LLM layer and the CLI never parse a shop's HTML.

- `Candidate` — `retailer`, `url`, `title`, `sku`, `model`, `variant`, `price`,
  `currency`, `in_stock`, `condition`, `queried_at`. Every field except
  `retailer`/`url`/`title` may be `None`; a search page often shows no price, and an
  unknown value must stay unknown.
- `Retailer` (abstract) — `name`, `domains`, `search(query, *, limit=10) -> list[Candidate]`,
  `fetch_offer(url) -> dict`. `search` raises `RetailerError` when the page cannot be
  read or parsed; an empty list means "the search worked and found nothing".
- `retailers.all_retailers()`, `retailers.enabled_retailers(allowed_domains=None)`,
  `retailers.get_retailer(name)`, `retailers.domain_allowed(host, allowed)`.

Adapters:

| Adapter | Shops | Search page | Detail |
| --- | --- | --- | --- |
| `PbTechRetailer` | `pbtech.co.nz` | `/search?sf=<query>`, `div.js-product-card` + `a.js-product-link[data-product-code]` | `scraper.fetch_quote` |
| `JbHiFiRetailer` | `jbhifi.co.nz` | `/search?q=<query>`, server-rendered `initData` JSON → Shopify product `handle` | `scraper.fetch_quote` |
| `MightyApeRetailer` | `mightyape.co.nz` | `/search?query=<query>` (**unverified**), JSON-LD `ItemList` first, product links under `/product/` as fallback | `scraper.fetch_quote` |

PB Tech card structure was verified live on 2026-09-23. The product name is
`h2.js-main-categoty-product-title-breakdown`; the adjacent `h3.np_title` holds
**marketing bullets**, so generic heading selectors must not be used.

JB Hi-Fi NZ (verified live 2026-09-24): the visible result grid is client-rendered, but
the page server-renders an `initData` object inside a plain `<script>` listing matched
products by Shopify `handle`; candidates are built as `/products/<handle>` and priced from
the product page's JSON-LD. No result-grid selectors are involved, so a theme change to
the grid cannot break discovery. Its search is **fuzzy** — even a nonsense query returns
matches — so the adapter never reports "no results"; it returns the candidates and lets
deterministic matching reject them.

Mighty Ape status (B1, 2026-09-24): the domain spelling was corrected to
`mightyape.co.nz`. The host is reachable, but the shop's bot-protection service
(response header `server: DataDome`) refuses every automated request with **HTTP 403**,
for both the site root and the search path. That control is not bypassed, so the search
path, the query parameter and the page structure remain **unverified assumptions**
(`mightyape.SEARCH_PATH` / `SEARCH_QUERY_PARAM`), and the offline fixture is synthetic.
At runtime the adapter raises `RetailerError` and discovery reports the source as failed
with the HTTP error, rather than implying the shop had no results.

`scraper.fetch_html(url) -> (final_url, body)` is the shared safe fetch (allow-list,
per-hop redirect validation, resolved-IP checks, size cap, monotonic deadline) that
adapters must use instead of calling `requests` directly.

## `discovery` — Candidate matching and comparison (Stage B)

Deterministic: a model never decides identity here.

- `match_candidate(request, candidate) -> (status, reasons)` where status is
  `"same"` / `"different"` / `"needs_confirmation"`. A stated mismatch is always
  `different`; a constraint the listing does not state is `needs_confirmation`, never an
  assumed match. Marketing colour names (`Obsidian` → black) and Chinese colour words
  are mapped before comparing.
  A requested **condition** (`new`/`used`/`refurbished`, plus synonyms) follows the same
  rule: a listing that states a different condition is `different`, a listing that states
  none is `needs_confirmation`, and a condition word the module cannot interpret is
  `needs_confirmation` rather than being treated as "no constraint" (B2). `None`,
  `""` and `"any"` mean no constraint.
  An **accessory is not the product**: when the candidate title contains an
  `ACCESSORY_KEYWORDS` word that the user's query did not mention — "Fitbit Air Active
  Band", "Fitbit Air Charging Cable" — the match is `different`. Without this, a cheap
  accessory outranks the real product and the cheapest row reads as the product's price.
  Asking for the accessory explicitly ("Fitbit Air band") still finds it. The word list
  deliberately excludes `charge`, which is part of the real product name "Fitbit Charge 6".
- `compare_offers(offers, request) -> (rows, notes)` — labels `within_budget`,
  `buyable`, and unknown shipping/condition; offers whose currency mismatches or whose
  price is unknown are listed with an `excluded_reason` instead of being compared.
- `search_and_compare(request, *, retailers=None, search_fn=None, offer_fn=None,
  limit_per_retailer=8, max_details=5) -> dict` — searches each enabled shop (one
  failing source never hides the others), matches candidates, prices the best few, and
  returns `sources`, `candidates`, `offers`, `notes`, `counts`. Injectable
  `search_fn`/`offer_fn` make the whole flow testable offline.
- `select_offer(result, index) -> offer` — 1-based; refuses an excluded offer.

## `notifier` — Email alerts

- `send_email_notification(to_email, subject, body, *, from_email=None, max_retries=None) -> dict`
  Sends via SMTP STARTTLS (e.g. port 587) or SSL (port 465). Returns
  `{"success": bool, "attempts": int, "error": str | None, "retryable": bool}`.
  Classification is by SMTP status code first: **4xx is transient** (retried with
  backoff) and **5xx is permanent**. `SMTPRecipientsRefused` is judged per recipient
  code, TLS/certificate errors stay permanent, and unrecognised errors are treated as
  permanent. `max_retries` defaults to **0** so the scheduler owns the overall budget;
  `notify_deal` always passes `0`. The connection is closed on TLS/send failures and
  error strings are stripped of configured credentials. Raises `NotifierError` for
  an invalid recipient.

- `notify_deal(to_email: str, task: dict, quote: dict) -> dict`
  Builds a deal-alert email and makes a single SMTP attempt.

## `storage` — SQLite persistence

- `init_db(db_path=None)` — creates tables and indexes (idempotent).
- `create_task(task: dict, db_path=None) -> int`
- `get_task(task_id, db_path=None) -> dict | None`
- `list_tasks(status=None, db_path=None) -> list[dict]`
- `update_task_status(task_id, status, db_path=None)`
- `update_next_check(task_id, next_check_at, db_path=None)`
- `update_last_check(task_id, quote_id, db_path=None)`
- `save_quote(task_id, quote, db_path=None) -> int`
- `get_quote(quote_id, db_path=None) -> dict | None`
- `create_event(task_id, quote_id, db_path=None) -> int`
- `get_active_event(task_id, db_path=None) -> dict | None` — returns the newest event
  in `pending`, `sent` or `failed`; a `failed` event is treated as exhausted.
- `update_event(event_id, status, error=None, attempts=None, sent_at=None, db_path=None)`
- `reset_active_events(task_id, db_path=None) -> int` — ends the current deal episode:
  moves `pending`, `sent` **and** `failed` events to `reset` and returns the count.
  Call this only for a *verified comparable* exit from the deal band.
- `list_events(task_id=None, db_path=None) -> list[dict]`

Send permit — the shared boundary between "stop" and "send" (G1), now owner-scoped (H1):

- `try_begin_send(task_id, owner, db_path=None) -> bool` — a single conditional
  `UPDATE` that grants the right to send only while the task is still `active` and
  `send_in_flight = 0`, recording `owner` as the holder. Returns False otherwise, so a
  check never sends after a stop has landed.
- `end_send(task_id, owner, db_path=None) -> bool` — releases the permit **only if
  `owner` still holds it**. Returns False for a stale owner, so an old worker cannot
  free a newer permit.
- `get_send_permit(task_id, db_path=None) -> dict | None` — current
  `status`/`send_in_flight`/`send_owner`/`last_send_started_at`, for inspection.
- `clear_send_permit(task_id, db_path=None) -> str` — human recovery for a permit left
  by a process that died mid-send. Returns `"cleared"`, `"not_in_flight"` or
  `"missing"`; it does **not** change task status, so a stopped task does not regain
  sending rights. Call only after every monitoring process has exited.
- `set_task_status_atomic(task_id, status, *, next_check_at=None, db_path=None, block_on_send=False) -> str`
  — returns `"changed"`, `"in_flight"` (a send had already started; the status is
  still updated but the caller must report that an email may be on its way) or
  `"missing"`. Pause/stop pass `block_on_send=True`; resume does not.

Cross-process task claim (F2):

- `acquire_task_lock(task_id, owner, db_path=None, stale_seconds=900) -> bool` — atomic
  claim via `INSERT OR IGNORE` on the `task_locks` primary key. `owner` should contain
  the resolved database path plus a process/thread token. Locks older than
  `stale_seconds` are reclaimed so a crashed process cannot block a task forever.
- `release_task_lock(task_id, owner, db_path=None)` — only the recorded owner can release.
- `list_task_locks(db_path=None) -> list[dict]`

Event status values: `pending`, `sent`, `failed`, `reset`.

## `scheduler` — Periodic checks

- `is_deal(task: dict, quote: dict) -> bool`
  Decimal-safe comparison. Requires a comparable quote (no error, price and currency
  present, currency matching the task, known stock) and `in_stock=True`.

- `check_task(task, *, fetcher=scraper.fetch_quote, send_fn=notifier.notify_deal, max_retries=None, db_path=None, should_abort=None) -> dict`
  Runs one check inside a database-level per-task claim keyed by the resolved database
  path and task id. A call that cannot claim returns
  `{"skipped": True, "reason": "lock_held"}`; a task that is not active returns
  `{"skipped": True, "reason": "task_<status>"}` without fetching.

  Sending is gated by `storage.try_begin_send`. Three outcomes are possible and all
  are reported explicitly:

  - `should_abort()` returns True first → `notification_cancelled_reason =
    "scheduler_stopping"`;
  - the permit is refused (task no longer active, or another send in flight) →
    `notification_cancelled_reason = "send_permit_denied"`;
  - the permit is granted → the send runs and the permit is always released.

  A permit already granted is never revoked: a message handed to SMTP cannot be
  recalled, and the CLI reports that instead of claiming a clean stop.
  Behaviour:
  - deal → create or reuse the active event within the retry budget; `exhausted`
    events are not retried.
  - valid comparable non-deal quote → end the episode (`event_reset: True`,
    `events_reset: <count>`), clearing `pending`, `sent` and `failed` events so a later
    deal gets a fresh budget.
  - fetch failure / missing data / currency mismatch / unknown stock → **no reset**;
    reported as `event_reset_skipped` with `event_reset_reason`.
  Returns `{"task_id", "quote_id", "is_deal", "notified", "duration_ms", ...}`.

- `TaskScheduler(db_path=None, max_workers=5, max_backlog=None, shutdown_timeout=5.0)`

  Lifecycle contract (G2, tightened in H2):

  - **One run per process, and never restartable.** `run_loop()` claims a process-wide
    slot; a second start in the same process — the same object, a new object, or two
    threads racing the same object — raises `SchedulerError`. To run again, exit the
    program and start it again. Coordination across processes is the database's job.
  - **Reservations are released by Future completion callbacks**, so success, failure,
    submission failure *and* cancellation all give the slot back (idempotently).
  - **Shutdown**: queued work is cancelled, running checks consult `should_abort`
    before requesting a send permit, and the loop waits up to `shutdown_timeout` for
    reservations to drain. If workers are still running when that window closes, the
    process slot stays held so no new scheduler starts beside them. A notification
    already handed to SMTP is not recalled.

  - `run_once(task_id) -> dict` — immediate single check; raises `SchedulerError` if the
    task does not exist, is not active, or is already claimed by another process.
  - `run_loop()` — blocking loop; ticks every minute, reserves each due task at most
    once, and submits to a thread pool bounded by `max_workers`. Work beyond
    `max_backlog` (default `2 × max_workers`) is deferred to the next tick.
  - `pending_count() -> int` — reserved (queued + running) task count.
  - `stop()` — signals the loop to exit; the drain happens in `run_loop`'s finally block.

## `cli` — User commands

Subcommands:

- `add <instruction> [--email <email>] [--yes]` — parse the instruction, show a
  summary and require confirmation before creating an active task. `--yes` skips the
  prompt and is equivalent to confirming the displayed summary. Cancel, EOF or a
  non-ready parse creates nothing. A `new`/`used`/`refurbished` condition is refused
  (exit code 4) because M40 cannot verify it against the page.
- `list [--status active|paused|stopped]` — show tasks; a fresh/empty database
  prints "No tasks found."
- `pause <id>`, `resume <id>`, `stop <id>` — status changes through the same atomic
  status function the scheduler's send permit uses. A missing ID returns a non-zero
  exit code with no success message. If a notification had already started when a
  pause/stop arrives, the change still applies but the CLI prints an explicit note
  that the email may still be delivered.
- `search <instruction> [--email <email>] [--select N] [--yes]` — find products by name
  across supported retailers, print the comparison, then optionally create a monitor task
  from one selected offer. Requires a budget to monitor (a target price is required) and
  refuses a request that already contains a URL, pointing at `add` instead. Nothing is
  created until an offer is selected *and* confirmed. A `new`/`used`/`refurbished`
  condition is refused (exit code 4) exactly like `add`, and the refusal happens **before**
  any retailer is searched: the condition is neither silently dropped nor carried into a
  task the monitoring module cannot honour (B2). `any`/unset is accepted.
- `release-send <id> [--yes]` — release a send permit left behind by a process that exited
  mid-send. Prints the recorded owner and start time, warns that all monitoring processes
  must already be stopped, and requires confirmation. Nothing clears permits automatically.
- `check <id>` — run one check immediately.
- `run` — start the scheduler loop (once per process; not restartable in-process).

## Delivery Semantics

Delivery is **bounded-retry best-effort**, not at-least-once. A crash between SMTP
acceptance and the DB write of `sent` can cause a **duplicate**; a permanent error, an
exhausted budget, or a task stopped mid-flight can cause a **miss**. Neither
exactly-once nor at-least-once delivery is claimed.
## Stage C additions (2026-09-24)

### `storage` — task sources and per-source history

- `create_task_with_sources(task, sources, db_path=None) -> int` — creates a task and
  attaches the given sources in one transaction; `sources=None` derives one source from
  `task["url"]` (legacy `create_task` behaviour unchanged from the caller's view).
- `add_task_source(task_id, url, retailer=None, db_path=None) -> int` — idempotent:
  attaching the same URL twice returns the existing row id.
- `list_task_sources(task_id, db_path=None, enabled_only=False) -> list[dict]` —
  ordered by id; rows carry `id, task_id, retailer, url, enabled, created_at,
  updated_at, last_check_at, last_error`.
- `get_task_source / set_task_source_enabled(source_id, enabled, db_path=None)`.
- `save_source_check(source_id, quote, error=None, db_path=None)` and
  `list_source_checks(task_id, db_path=None)` — per-source observation log.
- `save_quote(..., source_id=None)` and `list_quotes(task_id=None, source_id=None,
  limit=50, db_path=None)` — quotes may link to a source; legacy rows keep
  `source_id = NULL` and stay readable.

### `scheduler` — aggregated check

- `check_task(task, *, fetcher=scraper.fetch_quote, send_fn=notifier.notify_deal,
  max_retries=None, db_path=None, should_abort=None) -> dict`
  Fetches **every enabled source**, records each observation, then makes ONE deal
  decision from the best buyable comparable quote. Result keys include
  `is_deal, notified, chosen_retailer, chosen_price, sources_checked, sources_failed,
  observations[].` A deal email names the chosen shop and the other sources. A failing
  source never ends an episode; only all-comparable-sources-not-deal resets it.
  **Note:** `send_fn` is bound at import time as a keyword default — inject it
  explicitly in tests/harnesses; `patch.object(notifier, ...)` does not take effect.
- Skip reasons are explicit in the result: `lock_held`, `task_<status>`,
  `no_enabled_sources`.

### `discovery` — match-status gating

- Offer rows carry `match` (`same` / `needs_confirmation` / `different`) and a
  `selectable` flag; `select_offer` refuses `needs_confirmation` rows with a precise
  per-offer error (previously only excluded rows were refused).
- `select_offers(result, indices)` accepts several confirmed offers for ONE task and
  refuses the whole selection when it would mix colours, capacities, currencies, or
  include a non-`same` row.

### Review-fix interface changes (C1-C3, 2026-09-24)

- `storage.set_task_source_enabled_for_task(task_id, source_id, enabled,
  db_path=None) -> (status, row)` — status is `"updated"` / `"missing"` /
  `"foreign"`; the write is one conditional UPDATE keyed on **both** ids, so a
  source from another task can never be modified. `cmd_sources` uses this and
  exits non-zero on `"missing"`/`"foreign"` with zero side effects.
- `storage.update_task_conditions(..., end_episode=False)` — with
  `end_episode=True` the threshold change bumps `tasks.config_version` and resets
  active events **in the same transaction**.
- `storage.try_begin_send(task_id, owner, db_path=None, *, config_version=None,
  source_id=None)` — the permit is denied when `config_version` differs from the
  task's current version (conditions changed mid-check), when the triggering
  `source_id` is no longer enabled, or when no enabled source remains.
- `scheduler.task_identity(task)` / `scheduler.quote_identity_verdict(identity,
  quote)` — each observation is classified `same` / `needs_confirmation` /
  `different` against the identity stored at task creation
  (`tasks.identity_json`); only `same` observations can trigger a deal, end an
  episode, or be chosen as best. `identity_json` is NULL for legacy single-URL
  tasks, which always classify as `same`.
- `check_task` re-reads the task row **after** fetching, so a threshold change
  that committed during the fetches governs the decision.

### `cli` — new commands

- `search --select 1,2 [--yes] <instruction>` — multi-select confirmed offers and
  create ONE multi-source task (sources summary is printed even under `--yes`).
- `sources <task_id> [--enable ID] [--disable ID]` — list or toggle a task's sources;
  disabling the last enabled source prints "no enabled sources".
- `history <task_id> [--limit N]` — per-source price observations, newest first.
- `modify <task_id> [--target-price X] [--comparison lt|lte] [--frequency N]` —
  changing price or comparison ends the current episode and re-judges at the new
  threshold; frequency-only changes never touch events.

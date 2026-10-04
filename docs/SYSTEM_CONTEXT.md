# Bargain Agent — System Context

> This file describes the **current** architecture of Bargain Agent. It replaces the
> earlier "single-retailer PB Tech / M40 foundation" wording, which is out of date.
> Everything below is derived from the code and from the stage documents listed at the
> end of this file; nothing is asserted that those sources do not support.

## Scope

Bargain Agent is a **shopping bargain and price-monitoring agent for the New Zealand
retail market**: it takes a natural-language request, discovers the same product across
supported shops (and optionally the open web), matches product identity, compares
same-currency prices, and — after the user confirms — watches several sources on a
schedule and emails an alert when a deal is found.

## Component Diagram

```
   [ webapp.py — local web UI, 127.0.0.1 ]        [ cli.py / main.py — terminal ]
                 \                                             /
                  v                                           v
       +------------------------------------------------------------------+
       | agent.py — LLM instruction parsing (OpenAI-compatible chat API)   |
       |   parse_user_instruction   -> schema v1 (monitor task)            |
       |   parse_shopping_request   -> schema v2 (search request)          |
       +--------------------------------+---------------------------------+
                                        v
       +------------------------------------------------------------------+
       | schemas.py — validation & normalisation                           |
       |   validate_task / validate_search_request / validate_quote         |
       +--------------------------------+---------------------------------+
                                        v
       +------------------------------------------------------------------+
       | discovery.py — discovery + identity matching + price comparison   |
       |   search_and_compare(...)                                         |
       |   match_candidate  -> same | needs_confirmation | different        |
       |   _detail_identity (re-verifies identity from the detail page)     |
       |   compare_offers / select_offer / select_offers                    |
       +------+-------------------------------------------+---------------+
              |                                           |
              v                                           v
   +--------------------------+              +---------------------------------+
   | retailers/ (adapters)    |              | search_providers/brave.py       |
   |   PbTechRetailer         |              |   Brave Web Search API          |
   |   JbHiFiRetailer         |              |   candidate URLs only           |
   |   MightyApeRetailer(403) |              |   BRAVE_SEARCH_API_KEY          |
   +--------------------------+              +---------------------------------+

   (v1) direct URL add                       (v2) confirmed offers
              \                                           /
               v                                         v
       +------------------------------------------------------------------+
       | storage.py — SQLite persistence                                   |
       |   tasks | quotes | notification_events | task_locks | task_sources|
       +--------------------------------+---------------------------------+
                                        v
       +------------------------------------------------------------------+
       | scheduler.py — multi-source check engine (TaskScheduler)          |
       |   run_loop -> check_task -> _observe_sources (every enabled src)  |
       |   is_deal / task_identity / quote_identity_verdict                |
       +------+-------------------------------------------+---------------+
              v                                           v
   +---------------------------+          +----------------------------------+
   | scraper.py — fetch/parse  |          | notifier.py — SMTP email         |
   |   fetch_quote             |          |   notify_deal                    |
   |   fetch_quote_on_host     |          |   send_email_notification        |
   +---------------------------+          +----------------------------------+

   Cross-cutting:
     config.py / config_setup.py  — environment configuration (.env), setup form
     demo_data.py                 — offline demo parser / shops / fake notifier
     evaluation/                  — offline deterministic evaluation harness
```

## Data Flow

Both entry points (the local web UI `webapp.py`, and the terminal `cli.py` / `main.py`)
feed the same core modules.

### (a) Direct-URL add (v1 — the original foundation)

1. The user submits a natural-language request that contains a product URL, e.g.
   `add "Monitor <URL> when below NZD X" --email me@example.com`.
2. `agent.parse_user_instruction` calls the LLM chat API and returns a candidate
   monitor-task dict; `schemas.validate_task` validates **schema v1**, checks that the
   URL appears literally in the instruction, and requires an explicit check frequency
   (5..525600 whole minutes).
3. The CLI prints a summary and, on confirmation (or `--yes`), writes one row into
   `tasks` via `storage.create_task`.
4. `scheduler.run_loop` (or a one-off `check <id>`) fetches the page through
   `scraper.fetch_quote`, and `scheduler.is_deal` compares the quote to the task using
   `Decimal`.
5. If it is a deal, the scheduler requests an atomic send permit
   (`storage.try_begin_send`). If granted, `notifier.notify_deal` sends the email and
   the outcome is persisted in `notification_events`; if refused the notification is
   cancelled and the reason is reported.

### (b) Search → preview → confirm → multi-source monitoring (v2)

1. The user submits a product name with no URL ("search", not a monitor command).
2. `agent.parse_shopping_request` returns a **schema v2 search request** validated by
   `schemas.validate_search_request`. A search request is deliberately a different
   shape from a monitor task: it requires only `product_name`, never requires a URL,
   refuses a colour/capacity the user never stated, and turns an unstated frequency into
   `DEFAULT_FREQUENCY_MINUTES = 1440` (**once per day**). A search is never permission to
   monitor anything.
3. `discovery.search_and_compare` searches every enabled adapter, and (unless disabled)
   the Brave web-search provider, then matches candidates. Retailer search failures and
   web-provider failures are reported independently, so one broken source never becomes
   "no products found".
4. Price/currency/stock come from each candidate's **detail page** parsed by
   `scraper.fetch_quote` / `fetch_quote_on_host`; search snippets are never used as
   prices. The detail page's own product name is re-checked by `discovery._detail_identity`
   to derive the offer's `match` verdict (`same` / `needs_confirmation` / `different`).
5. The result is **previewed with zero database writes**. Only offers whose `match` is
   `same` are selectable; `select_offer` / `select_offers` refuse unconfirmed or
   mixed-product selections.
6. On confirmation, `storage.create_task_with_sources` creates **one** task with several
   `task_sources` rows (each with `source_kind` = `retailer_adapter` or `web_search`) and
   persists the confirmed identity in `tasks.identity_json`.
7. On each scheduled cycle, `scheduler.check_task` fetches **every enabled source**
   (`_observe_sources`), records a per-source observation, and makes **one** deal decision
   from the best buyable comparable quote. A failing source never ends a deal episode;
   only all comparable sources returning to non-deal does. One deal window produces one
   email naming the chosen shop and the other sources.

**Frequency:** an explicit cadence is held to 5..525600 minutes; where none is stated the
default is **1440 minutes (once per day)**. The loop ticks once per minute
(`schedule.every(1).minutes`), and each task is checked only when it is due.

## Verified vs. Unverified

**Verified (real, evidence recorded in the stage documents):**

- **PB Tech** and **JB Hi-Fi NZ** adapters — both verified against live pages and both
  used in the real dual-retailer comparison acceptance (`tools_live_dual_retailer_check.py`,
  exit 0). Adapter comments record the verification dates (2026-09-23 and 2026-09-24).
- **Single-link end-to-end monitoring** (parse → confirm → schedule → fetch → compare →
  SMTP → user-confirmed receipt) — accepted in Stage A.
- **Multi-source real fetch + fake send** — Stage C verified real fetching from two
  sources with exactly one injected fake notification and zero SMTP dials
  (`tools_live_stage_c_check.py`, exit 0).
- **Offline evaluation assets** — `python -m evaluation` (82 cases) and
  `tests/test_evaluation.py` run green.

**Unverified / not confirmed (do not present as working):**

- **Mighty Ape** — the adapter is implemented and registered, but the whole site is
  blocked by commercial anti-bot protection (**HTTP 403, server: DataDome**). Nothing was
  bypassed. The search path/query parameter are marked **UNVERIFIED** in
  `retailers/mightyape.py`, its fixture is synthetic, and at runtime the source is
  honestly reported as failed / not searched rather than "no results".
- **Brave web search** — the provider is implemented and covered by offline tests, but
  **real broad-discovery accuracy has not been evaluated**, and persisting search results
  depends on usage rights that are not confirmed.
- **Multi-source real receipt** — a real multi-source task reaching a real inbox has not
  been confirmed. (A controlled real vertical slice on 2026-09-25 did reach exactly one
  SMTP *accepted* submission; actual arrival in the mailbox remains the outstanding
  external item, so real end-to-end is reported as "PASS WITH EXTERNAL CONFIRMATION",
  not as fully verified.)
- **Real accuracy figures** — real-site price-extraction accuracy (3+ shops / 20+
  products) and stable real-LLM-parsing accuracy are not measured; long-running scheduler
  soak testing has not been done; cross-shop price gaps have not been demonstrated.

**Read the offline evaluation correctly:** the offline `1.00` results are **internal
consistency / conformance** of deterministic code against hand-authored expectations, not
real-world accuracy. The evaluation harness itself states it makes no real network, LLM,
Brave, merchant or SMTP call, never reads `.env`, and that the numbers describe offline
logic only. It must not be quoted as real LLM or real-site accuracy.

## Demo Mode vs. Live Mode

The local web UI runs in either of two modes:

- **Demo mode (default)** — fully offline and used for automated acceptance. It uses its
  **own per-run temporary SQLite database** and never touches the user's real task
  database. Substitutions:
  - parser → `demo_data.parse_demo_request` (offline bilingual EN/ZH parser);
  - shops → `demo_data.build_demo_retailers()` (demo "Demo Tech" / "Demo Mart"), with the
    web-search provider disabled entirely (`include_web=False`) — not even an
    "unavailable" lookup;
  - check fetcher → `demo_data.demo_check_fetcher` (fixed quotes, injectable failures);
  - notifications → `demo_data.fake_notify_deal` (recorded, never sent);
  - no network beyond `127.0.0.1`, no LLM, no SMTP.
- **Live mode (`--live`)** — the real parser (`agent`), the real retailers plus the web
  provider when configured, the real fetcher (`scraper`), and the real notifier
  (`notifier`, real SMTP). It refuses to fall back to the demo recipient silently, and
  destructive actions (checks, starting continuous monitoring) require an explicit
  server-side confirmation.

The server binds only to `127.0.0.1` (default port `8765`) and validates the JSON body of
every POST.

## Boundaries & Constraints

- Only HTTP/HTTPS public URLs are accepted. Private IPs, localhost, and non-allowed
  domains are rejected. Redirects are followed manually with a hop limit
  (`MAX_REDIRECT_HOPS = 5`); every hop is re-validated before the request is sent.
- The set of shops that may be scraped is fixed by configuration
  (`SCRAPER_ALLOWED_DOMAINS`, default `pbtech.co.nz`, `mightyape.co.nz`, `jbhifi.co.nz`).
  An adapter outside the allow-list is skipped, never bypassed.
- Money is always handled as decimal strings (`Decimal`); float arithmetic is never used
  for price comparison. Supported currencies are `NZD`, `AUD`, `USD`; comparisons are
  `lt` (below) and `lte` (at most).
- Product identity is resolved from JSON-LD with a three-state selection
  (`none` / `unique` / `ambiguous`); equally matching products are refused, and a
  selected JSON-LD product's quote is never completed from unrelated page metadata. The
  OpenGraph-only fallback reports stock as unknown.
- Detail fetches are capped (`DETAIL_CAP = 6` in total, at most 3 from the web provider
  and 3 from built-in shops, with unused quota transferring between the two classes).
- DNS resolution uses a fixed-size pool (`BoundedDnsResolver`), so live resolver threads
  are hard-bounded.
- The scheduler runs once per process and is not restartable in-process. Each task is
  claimed in the shared SQLite database (`task_locks`), the pending queue is bounded by
  `max_backlog` (default `max_workers * 2`, `max_workers = 5`), and extra due work waits
  for the next tick.
- Sending needs an atomic permit (`tasks.send_in_flight`) decided in the same database as
  pause/stop; the permit records its owner, and a permit left by a crashed process is
  cleared only by the explicit `release-send` command. A stop that lands first prevents
  the send; a send that lands first is completed and reported, because SMTP delivery
  cannot be recalled.
- Delivery is **bounded-retry best-effort**, not at-least-once: a crash between SMTP
  acceptance and the DB write can duplicate an email, and a permanent error, an exhausted
  retry budget, or a mid-flight stop can miss one. Neither exactly-once nor at-least-once
  delivery is claimed.
- Anti-bot protection is never bypassed (no forged User-Agent, no cookie/challenge
  handling); a blocked source is reported honestly.

## Interfaces at a Glance

- **`agent.py`** — `parse_user_instruction` (schema v1), `parse_shopping_request`
  (schema v2), `summarise_parsed_task`.
- **`schemas.py`** — `validate_task`, `validate_search_request`, `validate_quote`;
  `SUPPORTED_CURRENCIES`, `DEFAULT_FREQUENCY_MINUTES`.
- **`discovery.py`** — `search_and_compare`, `match_candidate`, `_detail_identity`,
  `compare_offers`, `select_offer` / `select_offers`; match constants
  `MATCH_SAME` / `MATCH_NEEDS_CONFIRMATION` / `MATCH_DIFFERENT`; `FEATURE_PHRASES`.
- **`retailers/`** — `Retailer` (abstract) with `search` / `fetch_offer`, the `Candidate`
  dataclass, and `DEFAULT_RETAILERS` (PB Tech, Mighty Ape, JB Hi-Fi) plus
  `enabled_retailers`.
- **`search_providers/brave.py`** — `search`, `is_configured`, and the exception
  hierarchy (`BraveNotConfigured`, `BraveAuthError`, `BraveRateLimited`, `BraveTimeout`,
  `BraveUnavailable`); endpoint
  `https://api.search.brave.com/res/v1/web/search`, `count` ≤ 10, `country=NZ`,
  `safesearch=moderate`; the API key is read from `BRAVE_SEARCH_API_KEY`, and a missing
  key raises `BraveNotConfigured` (surfaced by discovery as `unavailable`, distinct from a
  runtime `error`).
- **`scraper.py`** — `fetch_quote`, `fetch_quote_on_host`, `fetch_html`.
- **`storage.py`** — `create_task`, `create_task_with_sources`, `add_task_source`,
  `list_task_sources`, `record_source_check`, `list_quotes`, the event functions
  (`create_event` / `get_active_event` / `update_event` / `reset_active_events`), and the
  concurrency functions (`acquire_task_lock`, `try_begin_send`, `end_send`).
- **`scheduler.py`** — `check_task`, `is_deal`, `task_identity`, `quote_identity_verdict`,
  and the `TaskScheduler` class (`run_once`, `run_loop`, `stop`).
- **`notifier.py`** — `notify_deal`, `send_email_notification` (retry budget is owned by
  the scheduler event state machine; states are `pending`, `sent`, `failed`, `reset`).
- **`cli.py`** subcommands — `add`, `search`, `list`, `sources`, `history`, `modify`,
  `pause`, `resume`, `stop`, `check`, `release-send`, `run`.
- **`webapp.py`** routes — `GET /api/tasks`, `/api/tasks/{id}`, `/api/tasks/{id}/history`,
  `/api/monitor/status`, `/api/setup/status`, `/api/csrf`, `/api/demo/sends` (demo only);
  `POST /api/search`, `/api/search/select`, `/api/search/confirm`, `/api/setup`,
  `/api/tasks/add`, `/api/check/due`, `/api/monitor/start`, `/api/monitor/stop`,
  `/api/tasks/{id}/{check|pause|resume|stop|modify}`, `/api/tasks/{id}/sources/{enable|disable}`.
- **`evaluation/`** — `python -m evaluation`; five offline tracks
  (`instruction_schema`, `demo_parser_pipeline`, `identity`, `quote_extraction`,
  `notification_dedup`), 82 cases total, with `--json` output.

## Related Documents

- `DEVELOPMENT_PLAN.md` — product positioning, boundaries, phases, acceptance.
- `docs/INTERFACES.md` — module interface contract.
- `docs/stages/01-foundation.md`, `02-discovery-comparison.md`,
  `03-multi-source-monitoring.md`, `04-web-experience.md` — per-stage scope and evidence.
- `docs/EVALUATION.md` — offline evaluation report and its stated limits.
- `docs/architecture/BROAD_PRODUCT_DISCOVERY.md` — the Brave / broad-discovery design.
- `README.md` — run instructions.

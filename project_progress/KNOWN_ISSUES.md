# Known Issues — M40

_Last updated: 2026-09-25 (full-project offline integration acceptance; guard sealed to socket/DNS)_

## Verification Boundary (read this before quoting any "verified" claim)

Three different levels of verification exist and must never be conflated:

1. **Legacy single-URL full chain WITH receipt — verified.** Stage A
   ([FITBIT_LIVE_ACCEPTANCE.md](FITBIT_LIVE_ACCEPTANCE.md), 2026-09-23): parse → confirm →
   save → schedule → scrape → compare → email, and the user confirmed the message arrived.
   Earlier text here claiming "the full chain has never been exercised as one flow" was
   wrong and referred only to the multi-source extension; it has been corrected.
2. **Multi-source real fetch + fake send — verified.** Stage C
   (`tools_live_stage_c_check.py`, 2026-09-24): real PB Tech + JB Hi-Fi search, task
   creation, aggregated checks, modify/disable flows. The sender is an explicit stub;
   no SMTP is dialled and no receipt exists.
3. **Multi-source actual receipt — NOT verified.** No deal email from a multi-source
   task has ever been delivered to a real mailbox. This requires authorisation.

- **Independent SMTP verification (2026-09-23).** A test email was sent through Gmail,
  from and to `zhangfw0516@gmail.com`, and the user confirmed receipt. This validates
  the notifier in isolation only.
- **Real parsing of search requests.** Run 2026-09-24 (`tools_live_llm_search_eval.py`,
  10 real DeepSeek calls of the 12-call budget): 7 of 8 cases pass; the Chinese-colour
  case is **flaky** (fail-closed; see the Stage C section). No further LLM calls this round.

## Stage B — Verified vs Unverified

| Item | Status |
| --- | --- |
| PB Tech search (candidate discovery) | **Verified live** 2026-09-23 against real pages |
| PB Tech detail price/stock via `scraper.fetch_quote` | **Verified live** (NZD 239.00, in stock) |
| Identity matching (model + colour/capacity) | Verified live on real titles + fixture tests |
| Comparison labelling (budget, stock, unknown shipping/condition) | Fixture tests; live output inspected |
| Search → select → confirm → monitor task | **Verified live** with a throwaway database (exit 0) |
| **JB Hi-Fi NZ search (`/search?q=`, in-page `initData`)** | **Verified live** 2026-09-24 (HTTP 200, real product handles) |
| **JB Hi-Fi NZ product page quote** | **Verified live** 2026-09-24 — `/products/google-fitbit-air-obsidian` → JSON-LD → NZD 239.00, `InStock`, name "Google Fitbit Air (Obsidian)" |
| **Dual-retailer comparison (PB Tech + JB Hi-Fi)** | **Verified live** 2026-09-24 — one product, both shops, both real quotes + stock, comparison display, task created, exit 0 |
| Noel Leeming (considered, not adopted) | Home page 200, but search results are client-rendered only (`product-card` entries are skeleton placeholders, JSON-LD `ItemList` empty) → no product list obtainable from HTML |
| Mighty Ape domain spelling | **Fixed (B1)**: module/imports/`BASE`/`domains`/default allow-list/`.env.example`/fixture/tests/docs all changed from `mightyaoe.co.nz` to `mightyape.co.nz`; a regression now asserts the requested host |
| Mighty Ape host reachability | **Reachable** — DNS/TCP/HTTP all work for `www.mightyape.co.nz` |
| Mighty Ape search path & page structure | **Still unverified (B1)**: every automated request is refused with **HTTP 403 Forbidden**, response header `server: DataDome` (site-side bot protection), for the site root *and* the search path. Not bypassed. The path/param live in `mightyape.SEARCH_PATH`/`SEARCH_QUERY_PARAM`, marked UNVERIFIED, and the fixture is synthetic |
| New/used/refurbished condition end-to-end | **Fixed (B2)**: `add` and `search` both refuse an explicit condition (exit 4) instead of ignoring it; matching treats a conflict as `different` and an unstated/uninterpretable condition as `needs_confirmation`. The monitoring module still cannot verify condition, so it is not supported |
| LLM parsing of a search request (schema v2 prompt) | **Run 2026-09-24** (`tools_live_llm_search_eval.py`, 10 real DeepSeek calls of the 12-call budget): 7 of 8 distinct cases pass; the Chinese-colour case is **flaky** — see the Stage C section. No task is created, nothing scraped, no email sent |
| Full end-to-end incl. price drop → email → receipt | **Not run** (see also the Stage C SMTP note) |

## Stage C — Multi-source monitoring (implemented 2026-09-24)

| Item | Status |
| --- | --- |
| `task_sources` table + `quotes.source_id` migration (idempotent, legacy backfill once) | Implemented; offline-tested (`tests/test_stage_c.py::TestMigration`) |
| `search --select 1,2` multi-select → ONE task with several sources | **Verified live** 2026-09-24 (`tools_live_stage_c_check.py`, exit 0): PB Tech + JB Hi-Fi, one task, 2 sources |
| Aggregated `check_task` over all enabled sources | **Verified live**: 2 sources fetched (both NZD 239.00, in stock), one deal decision, exactly one notification per episode |
| Per-source `source_check` records + price history per source | **Verified live**: history grows per source, newest first |
| Modify target price ends episode and re-judges | **Verified live**: target 250 → 100, episode ended, next check `deal=False`, no resend |
| Frequency-only change does not touch events / resend | Offline-tested |
| Disable one source → only the other is fetched | **Verified live**: JB Hi-Fi disabled, next check fetched PB Tech only |
| Legacy single-URL task remains checkable | Offline-tested (`test_old_single_url_task_still_checks`) |
| Deal email body mentioning other sources | Unit-tested; **also exercised once by accident over real SMTP** (see below) |

### Stage C verification notes and incidents (honest record)

- **One real SMTP submission to a non-routable address (2026-09-24, during tooling).**
  The first `tools_live_stage_c_check.py` run patched `notifier.notify_deal` via
  `patch.object`, but `check_task`'s `send_fn` **keyword default is bound at import
  time**, so the patch never took effect and the scheduler dialled real Gmail SMTP
  toward `acceptance@example.com` (a reserved, non-routable address — no real
  recipient). The harness now passes `send_fn=` explicitly and a second full run
  produced **zero** SMTP dials (log inspected). Product code was not affected: offline
  tests inject `send_fn` explicitly. Side effect: the multi-source email body path was
  in fact exercised over real SMTP once (logged "Email sent").
- **Chinese colour + anti-invention guard is flaky (real LLM finding).**
  `validate_search_request` rejects a colour that does not literally appear in the
  instruction (anti-invention guard). For the Chinese instruction
  “我想买黑色的 Google Fitbit Air…”, the model returned `colour: "黑色"` (accepted) in
  one run and apparently translated it to `"black"` (rejected, `ParserError`) in
  another — non-deterministic. The failure is **fail-closed** (a search is refused, not
  mis-scoped), consistent with the "rather refuse than guess" rule, but a Chinese
  instruction may need a retry or English phrasing. Possible future fix (not
  implemented): accept a colour whose Chinese source character appears in the
  instruction and whose canonical base matches `discovery.CHINESE_COLOURS`.
- **Budget is deliberately not required for a search** (`SEARCH_REQUIRED` =
  product_name + frequency). `search` succeeds without one; only creating the monitor
  task requires it (CLI exit 2 with guidance). An eval case asserting otherwise was
  corrected, not the code.
- **The accidental-send incident above is not a second email channel**: no scheduled
  monitoring was started, no real mailbox received anything, and `.env` was untouched.

### Stage C specific risks

- **One notification per episode is decided per task, not per source.** Two shops both
  matching produce one email naming the best buyable offer and the other sources —
  verified offline and live.
- **Only "all comparable sources say not-a-deal" resets an episode.** A failing or
  disabled source leaves the episode alone (verified). Consequence: if the cheaper shop
  is down, the episode may stay open longer — correct-by-design, not a bug.
- **`sources --disable` can empty a task.** Disabling the last enabled source is
  allowed but reported (`no enabled sources` on stderr); the next check skips with
  `reason=no_enabled_sources` instead of silently passing.

### B1: what actually blocks Mighty Ape (evidence, 2026-09-24)

Read-only, low-frequency probe (`tools_live_mightyape_check.py`), with the allow-list
overridden **for that process only** — the local `.env` was not modified:

| Request | Result |
| --- | --- |
| `https://www.mightyape.co.nz/` | **403 Forbidden**, `server: DataDome` |
| `https://www.mightyape.co.nz/search?q=fitbit+air` | **403 Forbidden**, `server: DataDome` |
| `https://www.mightyape.co.nz/search?query=fitbit+air` | **403 Forbidden**, `server: DataDome` |

The earlier "HTTP 502 on CONNECT" was a consequence of the **misspelled** domain and did
**not** show that the correct domain was blocked. The corrected domain is reachable but
protected by a commercial anti-bot service, which refuses ordinary clients. No bypass was
attempted (no forged User-Agent, no cookie or challenge handling), so the search path and
page structure remain assumptions. At runtime the adapter raises `RetailerError` and
discovery reports the source as failed with the HTTP error rather than implying the shop
returned no results.

### Stage B specific risks

- **Search page markup can change.** The PB Tech card selectors were verified once, on
  2026-09-23. A live run already exposed one real bug (a generic `h3` selector picked up
  marketing bullets instead of the product name); the adapter now uses specific selectors
  and falls back to the URL slug. If the markup changes again, `search` raises
  `RetailerError` instead of returning wrong titles.
- **Only one retailer is actually searched with the current local `.env`**, because
  `SCRAPER_ALLOWED_DOMAINS` there still lists only `pbtech.co.nz` (unchanged deliberately;
  the user's configuration was not edited). The UI reports this honestly as "NOT SEARCHED".
- **Mighty Ape cannot be exercised from this environment at all** (DataDome 403), so its
  adapter is *implemented and fixture-tested but unproven*. A fixture that matches the
  adapter's own assumptions cannot discover a wrong assumption — that is exactly how the
  B1 typo survived, since the fixture and the adapter shared it. **Synthetic fixtures do
  not substitute for a real retailer verification.**
- **JB Hi-Fi's search is fuzzy.** A nonsense query still returns matches (the engine
  token-matches, so "zzqqxxnotathing" returns "Nothing Phone …"). The adapter therefore
  never reports "no results" from that shop; it hands over candidates and lets
  deterministic matching reject them. Conversely, when the page's server-rendered payload
  is missing entirely, the adapter raises `RetailerError` instead of returning `[]`,
  because an empty list would read as "this shop had nothing".
- **No cross-store price difference has been demonstrated yet.** In the acceptance run both
  shops quoted exactly NZD 239.00, so the comparison showed "same price, both in stock",
  not "one is cheaper". Do not describe the system as having found a lower price.
- **Accessory detection is a finite keyword list.** `ACCESSORY_KEYWORDS` can miss unusual
  accessory names and can misclassify a product whose name contains one of its words
  (`charge` is deliberately excluded so "Fitbit Charge 6" survives). The bias is toward
  rejecting: an accessory ranking first would be read as the product's price.
- **A new/used/refurbished requirement is refused, not satisfied.** `add` and `search`
  both return exit code 4 for an explicit condition. The matcher can already tell a
  conflicting condition from an unstated one, but since the scraper cannot confirm
  condition and the monitoring task does not enforce it, accepting the request would
  silently ignore what the user asked for.
- **Shipping cost is almost never available** from search or detail pages, so no displayed
  price includes shipping.
- **Colour vocabulary is finite.** Unrecognised colour names produce
  `needs_confirmation` rather than a guess.
- **A monitor task requires a budget.** "Notify me when it is in stock" without a target
  price is not supported yet.
- **Only same-currency offers are compared.** Cross-currency prices are excluded and
  labelled, never converted.

## Stage C Review Fixes C1-C3 (2026-09-24, after review "PASS WITH REQUIRED FIXES")

The reviewer reproduced three real defects with a throwaway database; all are fixed with
regression tests (suite now **355 tests**):

- **C1 - source ownership.** `sources <task-id> --enable/--disable <source-id>` used to
  write first and check ownership afterwards, so a mistyped task id disabled *another
  task's* source. Now `storage.set_task_source_enabled_for_task` does one conditional
  UPDATE keyed on both ids: foreign/missing ids return non-zero and change nothing.
- **C2 - identity gate during monitoring.** Multi-source tasks now store the confirmed
  identity (`tasks.identity_json`: request product name, colour, capacity, confirmed
  title) at creation. Every fetched observation is classified by
  `scheduler.quote_identity_verdict`; only a `same` verdict can trigger a deal, end an
  episode, or be chosen as best offer. A detail page that turns into a NZD 19 accessory
  therefore produces no alert (regression-tested). Legacy single-URL tasks carry no
  identity and pass unchanged. Offer *selection* is also strict: only an explicit
  `match == "same"` is selectable; a missing verdict is unconfirmed, never implicit
  confirmation.
  - Heuristic note: the accessory scan applies only to listing-style names (<= 80
    chars). Long detail-page marketing names legitimately mention features
    ("...7 Day Battery Life...") and were false-flagged in the first live run - the
    hardened live script caught this immediately, which is exactly what it is for.
- **C3 - modify-vs-check race.** Threshold/comparison changes now bump
  `tasks.config_version` and reset active events **in one transaction**
  (`update_task_conditions(..., end_episode=True)`). After fetching, `check_task`
  re-reads the task row and requests the send permit with the version it decided
  under plus the triggering source id; the single conditional UPDATE denies the permit
  when the conditions changed mid-check, when the triggering source was disabled, or
  when no enabled source remains. Both orderings are covered by event-barrier tests
  (no sleeps), not luck.

Also in this round: `tools_live_stage_c_check.py` now asserts its own expectations
(distinct PB Tech + JB Hi-Fi sources, both checked, exactly one fake send per deal,
expected behaviour after modify/disable) and exits non-zero on any failure; the sender
is passed explicitly as `send_fn=` on every call, and the log is asserted free of
notifier/SMTP lines. Real DeepSeek was NOT called and no test email was sent.

Remaining verification boundary unchanged: multi-source actual receipt is still
**not verified** (needs authorisation); scheduler long-run soak still not run.

## Same-product identification hardening (round 3, 2026-09-24)

After C1-C3 passed review, the identification rules were strengthened on both the
search and monitoring sides (suite now **370 tests**):

- **Generation**: "Fitbit Air 2" is never "Fitbit Air". Search: a user-named
  generation missing from the listing is needs_confirmation; a stated conflicting
  generation is different; a listing-stated generation the request did not name is
  needs_confirmation — never same. Monitoring: a page showing a generation the
  confirmed product does not carry is different. Capacity digits ("64 GB") are
  excluded via `discovery._capacity_digit_tokens`.
- **Model-token completeness**: the quote name must contain every significant model
  token of the confirmed product; "Google Fitbit Charge 6 Black" (shares only
  "fitbit", and even carries the requested colour) is needs_confirmation — no alert,
  no episode reset.
- **Confirmed specs persist**: colour/capacity the user never stated are inferred
  from the selected offer's title, stored with the task AND re-inferred inside
  `quote_identity_verdict` (independent of the creation path). Lavender/256GB later
  is different; a page that stops stating the spec is needs_confirmation.
- **No length exemption for accessory scanning**: the former <=80-char skip is
  replaced by the feature-phrase whitelist `discovery.FEATURE_PHRASES`
  ("battery life"/"battery health", derived from the real PB Tech page name on
  2026-09-24). Accessory words at any length outside those phrases are different.
- `tools_live_stage_c_check.py` now asserts `identity_verdict == "same"` for every
  source; any different/unconfirmed source exits non-zero. The live run passed with
  both shops same, exactly one fake send, zero SMTP dials. No DeepSeek calls, no
  emails, no `.env` or Git changes this round.

## Stage D — local web UI (2026-09-24)

- Demo mode is the acceptance path: offline bilingual parser, two demo retailers,
  demo fetcher, recorded fake notifications. No DeepSeek / SMTP / real-shop access.
- Known boundaries (accepted): loopback-only bind with no auth (single-user local
  tool); in-memory store of the last 20 search results for the selection step; the
  demo parser is deterministic and does not represent real LLM quality; no
  continuous background scheduler in the UI (manual "check now" / "run due checks");
  `--live` mode exists but is NOT acceptance-tested.
- The Stage D task brief attachment did not reach the assistant; the brief was
  reconstructed from DEVELOPMENT_PLAN Stage D + the user's message constraints and
  is marked as such in `docs/stages/04-web-experience.md`.

## Stage D review round 2 (2026-09-24) — fixes applied

All six review items are fixed with regressions (suite now **414 tests**); the
residual boundaries are:

- The browser walkthrough runs headless Chrome via `tools_browser_walkthrough.js`
  (30 assertions, screenshots in `project_progress/browser_evidence/`); the
  agent-browser daemon itself occasionally blanks its tab between CLI calls — an
  environment quirk, worked around by the single-process Playwright script.
- Demo temp databases are not auto-deleted on process exit (Windows file-lock);
  harmless clutter in `%TEMP%`.
- The monitor loop stays single-run-per-process (inherited from the scheduler):
  after `Stop monitoring` the app must be restarted to monitor again — stated in
  the UI.
- Live mode still has no end-to-end acceptance (recipient/config boundaries are
  unit-tested only); multi-source actual receipt remains unverified.

## Stage D delivery round (2026-09-24) — D-R1/D-R2 + package

- D-R1/D-R2 fixed with regressions (suite now **424 tests**; walkthrough 33 asserts).
- Distribution package excludes all secrets/data by construction; verified by a
  forbidden-name scan at build time and a clean-directory run with system Python.
- Cross-machine acceptance is NOT claimed (no second machine available).
- Live mode requires one internet-connected `pip install` on first live start
  (openai has compiled wheels and cannot be vendored version-independently).
- Incident recorded: an Edit that silently failed to persist caused one test
  config line (`DEFAULT_NOTIFY_EMAIL=someone@example.com`) to be written into
  the developer's real `.env`; that line was removed. CORRECTED RECORD: the
  backup `.env.pre-cleanup-backup` was created AFTER the pollution, so it cannot
  prove the original recipient configuration was restored — "test value removed,
  whether the original recipient config was recovered remains UNCONFIRMED". The
  current recipient (zhangfw0516@gmail.com) was explicitly provided by the user,
  not restored. No other .env content was touched. Editing workflow now verifies
  writes immediately.

## Delivery Semantics

**Bounded-retry best-effort, not at-least-once.** Concretely:

- Each deal episode has a finite attempt budget. Once exhausted, the event stays
  `failed` until the price verifiably leaves and re-enters the deal band.
- A crash between SMTP acceptance and the `sent` write can produce a **duplicate**.
- A permanent error, an exhausted budget, or a task stopped mid-flight can produce a
  **miss** (no notification at all).
- A stop prevents a send that has not started yet, but it cannot recall one that has:
  once the permit is granted the message may be delivered even though the CLI reports
  the stop honestly rather than claiming a clean shutdown.

Both duplicates and misses are therefore possible; no exactly-once or at-least-once
guarantee is claimed.

## Known Code Limitations (accepted for M40)

- Three retailers are declared (`pbtech.co.nz`, `mightyape.co.nz`, `jbhifi.co.nz`) and each
  needs an adapter; `SCRAPER_ALLOWED_DOMAINS` remains the single supported-shop boundary.
  The code default lists all three, but the current local `.env` lists only `pbtech.co.nz`,
  so only PB Tech is searched by default here. A domain outside the allow-list raises
  `ScraperError`. Two of the three are verified live; Mighty Ape is not (DataDome 403).
- The scheduler must remain running to check prices. The task claim is a row in the
  shared SQLite database, so it coordinates processes that share that database; a
  distributed queue is out of scope.
- **The scheduler runs once per process and is not restartable.** A second `run_loop()`
  in the same process — on the same object or a new one — is refused with
  `SchedulerError`; two threads starting the same object cannot both succeed. To run
  again, exit the program and start it again. Two *processes* may run concurrently,
  coordinated by the database claim.
- **A send permit abandoned by a process that died mid-send needs one explicit human
  step**: `python main.py release-send <id>` (confirmation required). Nothing clears
  permits automatically, because at startup a permit held by a live sender is
  indistinguishable from an abandoned one, and clearing a live sender's permit can cause
  a duplicate email. The released permit does not change the task's status, so a stopped
  task does not regain sending rights. Whether the interrupted send reached the recipient
  is unknown; after recovery the next matching check notifies again.
- When many tasks are due at once, work beyond `max_backlog` (default
  `2 × max_workers`) is deferred to the next tick rather than growing the queue.
- Product identity must be resolvable from the page. When several JSON-LD products
  match equally the fetch is refused. Several offers are accepted only when every
  identity-relevant field (price, currency, availability, condition, seller, SKU, MPN)
  matches; a price/currency match with a differing or missing field is refused.
- Once a JSON-LD product is selected, its quote is built only from that product. If it
  lacks price or currency the fetch fails; OpenGraph values are never spliced in. The
  OpenGraph fallback applies only to pages with no JSON-LD product at all, and always
  reports stock as unknown.
- Condition filtering (`new`/`used`/`refurbished`) is refused at **both** entry points —
  `add` and `search` (exit code 4) — because the scraped page cannot confirm it and the
  monitoring task does not enforce it. `condition = "any"` or absent is allowed.
- `variant` is `null` unless the page exposes a size/model/additionalProperty value.
- Shipping cost is not included in deal comparison; the email states it is unverified.
- Product search exists (`search`), but it is limited to the shops with adapters and to
  the configured allow-list; it is not a general web search.

## Security Notes and Limits

- Redirects are followed manually and every hop is validated before the request.
- The overall fetch budget uses a monotonic clock and starts before DNS admission. DNS
  goes through a fixed-size pool (`BoundedDnsResolver`, 4 slots): admission waiting counts
  against the deadline, a still-running lookup keeps its slot rather than being replaced,
  and a saturated pool fails inside the deadline instead of queueing. The number of live
  resolver threads is therefore hard-bounded.
- The OS resolver call itself cannot be cancelled, so a lookup stuck inside the OS can
  hold one of those four slots for as long as the OS takes. That is a deliberate trade-off
  against unbounded thread accumulation: sustained resolver hangs degrade DNS capacity
  (fails fast within the deadline) rather than leaking threads. We do not claim the stuck
  call is ever reclaimed.
- Domain/IP checks remain best-effort: a DNS change between validation and connection
  cannot be fully excluded. Only the validated hostname is requested, never a raw IP.
- This is not a general-purpose SSRF sandbox; the guarantee is limited to a fixed
  retailer allow-list with per-hop validation.

## Test Notes

- Offline suite currently reports **547 tests** (`python -m unittest discover -s tests -v`).
- `tests/test_zz_offline_network_guard.py` seals the **whole current outbound surface** at import
  and fails the run if any test reaches it: **socket/DNS** (`socket.getaddrinfo` and
  `socket.create_connection`, loopback allowed for the Stage D local server), `requests.Session`,
  `requests.get/post/request`, `search_providers.brave._default_sender`, `smtplib.SMTP/SMTP_SSL`
  and `openai.OpenAI`. Sealed to socket/DNS on 2026-09-25; the suite stays `OK` even with fake
  Brave/LLM keys present in the environment.
- Stage C adds `tests/test_stage_c.py` (31 tests: migration, aggregated check, modify
  semantics, per-source history) and extends `tests/test_cli.py` (sources / history /
  modify commands) and `tests/test_discovery.py` (`match_status` gating).
- **`cmd_search` early-return nuance**: the "nothing can be monitored" short-circuit
  only fires when no `--select` was given; an explicit `--select` on an unconfirmed
  offer falls through to `select_offers`, which reports the precise per-offer error.
- Stage B adds `tests/test_retailers.py` and `tests/test_discovery.py`; the retail search
  flow is fully injectable, so no test performs a network request.
- B1 adds host-level regressions: the Mighty Ape requested URL's hostname must be
  `www.mightyape.co.nz` (checked both on `search_url()` and on the URL actually handed to
  the fetch layer). B1/B2 also assert the default allow-list spelling and that
  `search --yes` with a condition neither searches nor creates a task.
- JB Hi-Fi regressions assert the verified host and query parameter, parse the real
  `initData` shape from a fixture captured 2026-09-24, require deduplication,
  assert **no price is claimed from the search page**, and require that a missing payload
  raises instead of claiming "no results". Accessory regressions cover four real titles
  rejected, an explicit accessory query still matching, and "Fitbit Charge 6" surviving.
- Two tests spawn a real child Python process against a shared temporary SQLite file: one
  verifies cross-process mutual exclusion, the other verifies that a permit left behind by
  a dying sender is recovered only by the explicit human step. Both use `subprocess` with a
  timeout and do not touch the network.
- Tests use thread-synchronisation events instead of sleeps where possible; no test
  performs a real network, LLM or SMTP call.
- `tests/test_notifier.py::test_send_retryable_failure_retries` intentionally sleeps
  ~3s due to backoff; this is bounded and not a hang.

## Repository — Git State

**Current state (read-only check, 2026-09-24): the branch ref is missing again.** While
creating the Stage B baseline checkpoint, `git commit` produced
`7ad3d196f7fbc36c97fb402fa7b1877134ead910` (the object is intact — `git cat-file -t` returns
`commit`), the reflog records `d5a3680 -> 7ad3d196`, but
`.git/refs/heads/review/m40-monitoring-foundation` was gone immediately afterwards, so
`git log` reports "does not have any commits yet" and `git tag` fails with
"Failed to resolve 'HEAD' as a valid ref". `refs/heads/main` is unaffected.

Per instruction, **Git writes were stopped at that point and no metadata was repaired by
hand** (this round authorises code review only — not rebuilding the ref, disabling
`worktreeConfig`, removing the worktree, or rebuilding `.git`). So no Stage B branch was
created and the Stage B work stays in the working tree.

Verified local backups that do not depend on the ref:

```
...\bargain-agent-backups\m40-baseline-20260923T115344Z\     302 files (pre-Stage-B baseline)
...\bargain-agent-backups\stage-b-pre-fix-20260923T123501Z\  359 files, sha256 096d8e82…1df55e
...\bargain-agent-backups\stage-b-post-b2-20260924T003148Z\  385 files, sha256 58cdf801…3aed6b
```

Both contain real `.git/objects` (not worktree pointer files) and `.env`. The Stage B
backup was verified to contain the checkpoint object itself
(`.git/objects/7a/d3d196…`, 595 bytes) and the reflog that records its SHA, so the
checkpoint is recoverable even while the branch ref is missing.

Observed correlates — **not** a proven cause:

- A linked worktree is registered at `.git/worktrees/bargain-agent` (gitdir under
  `~/.tool/worktrees/...`).
- `.git/config` sets `[extensions] worktreeConfig = true`.
- `.git/refs/review/` holds external checkpoint refs
  (`refs/review/turn-diffs/checkpoints/...`, visible inside the Stage B backup) while
  `refs/heads/review/` was missing.

Different ref namespaces are involved, so the responsible tool/process has **not** been
identified; these are correlations, not a confirmed root cause. `worktreeConfig` has not
been disabled and the worktree has not been removed.

**Do not run `git gc` / `git prune`.** The dangling trees reported by `fsck` may contain
recoverable user data and are not proven safe to delete. Their count fluctuates as
unstaged work is written and is not by itself evidence of loss or corruption.

**Earlier recovery scope (do not over-claim).** A previous session recreated `.git` from
a fresh clone after local ref corruption; only the branch ref and `HEAD` were recreated by
hand, while `main` and remote-tracking refs come from the clone's `packed-refs`. It is not
proven that every local-only ref survived.

## This Round Is Uncommitted

The B1/B2 fixes and the dual-retailer acceptance work (JB Hi-Fi adapter, accessory rule)
are left in the working tree because this round forbids modifying Git metadata, and
committing here has repeatedly removed the branch ref (which then requires metadata writes
to recover). Review with `git status --short` and `git diff --stat`.

## Out of Scope (Not Fixed Here)

- ~~Multi-retailer *continuous* monitoring for one task~~ — **implemented in Stage C
  (2026-09-24)**; remaining Stage C items: real scheduled-loop soak run and an
  authorised end-to-end email receipt.
- ~~Price history and trends~~ — per-source history implemented in Stage C; trend
  charts/summaries are not.
- Web UI or API server (Stage D).
- Distributed/multi-process queueing.
- General network sandboxing / full SSRF hardening.
- Bypassing site access controls (e.g. Mighty Ape's DataDome 403).
- Full new/used filtering and continuous condition monitoring.
- Executing JavaScript to render client-side search grids. Second-shop selection therefore
  favours shops whose search page ships a server-rendered payload (PB Tech cards, JB Hi-Fi
  `initData`); Noel Leeming was rejected on exactly this ground.
- Fixing the Chinese-colour guard flake by weakening the anti-invention rule (a narrow
  allow-list fix is possible; see Stage C section).

## 商品发现扩展（2026-09-24）

- **真实 Brave 搜索未验证**（外部待验收）：需要 `BRAVE_SEARCH_API_KEY` 与对应使用权确认；
  未配置时该来源显示 "NOT SEARCHED (not configured)"，原商家搜索照常。官方对保存搜索结果有
  存储权利限制 —— 客户端不持久化原始响应/摘要；正式启用前仍需核对选中 URL 保存权利。
- **网页候选详情抓取未在真实站点验证**：`scraper.fetch_quote_on_host` 强制 HTTPS、仅候选
  自身主机、跨主机跳转/私网 IP 拒绝 —— 均有离线测试，但真实商店/品牌站行为（403、JSON-LD
  缺失、机器人拦截）待密钥到位后小规模核验。预期大量非电商站点会以结构化失败原因上报。
- **去重保留商家归属**：同一 URL 同时被商家与网页搜索发现时保留商家条目（记 `also_found_by`），
  网页来源不再单独出价 —— 有意为之，避免重复行。
- **摘要永远不是价格**：网页候选的价格/币种/库存只来自详情页 JSON-LD；搜索摘要仅请求内可见。

## 商品发现扩展 · 复审修复带来的行为变化（2026-09-24）

- **详情页未陈述规格即不可确认**（有意从严）：详情页名不含颜色/容量时，即使搜索卡片写了，也不再判为
  `same`，该 offer 不可建监控任务（`needs_confirmation`）。演示数据详情页名已带颜色，演示流程不受影响；
  真实商店若详情页名过于简略，会出现较多“未确认”行 —— 属准确性问题，不用猜测填补。
- **同 path 不同变体不再合并**：`?variant=30ml` 与 `?variant=60ml` 视为两件商品，各出一行；
  无法识别的查询参数一律保留（宁可少去重），因此单纯的参数差异不会再被误并为同一商品。
- **测试装置抖动**：基于本地 socket 的 webapp HTTP 测试在本轮三轮全套运行中出现过 1 次
  `http.client.IncompleteRead`，重跑两轮全绿；未触碰 webapp/网络层，疑为既有装置抖动，待复核。

## 全项目封网集成验收（2026-09-25，离线）

- **测试发现**：`unittest discover` 547 项，覆盖 Stage A–D、Brave 扩展、Stage D live、Stage E 评估；
  无 `skip`/`expectedFailure`，发现数=执行数。
- **guard 覆盖补全**：原守护未封 socket/DNS；本轮扩到 `socket.getaddrinfo` / `socket.create_connection`
  （回环放行）与 `requests.get/post/request`，并新增回归。负向对照：真实抓取路径会命中 DNS 封印。
- **全套与稳定性**：封网下 `Ran 547 tests ... OK`（2 轮），带假密钥再跑 547 OK；
  `test_webapp` 5/5、`TestWriteGuards` 10/10。§20 遗留的“webapp HTTP 抖动 / `WinError 10053`”
  本轮 15 轮组合复跑**未复现**（重试助手有效）。
- **评估**：`python -m evaluation` 82/82、`tests.test_evaluation` 11 OK；`failure_isolation`
  分母由数据集显式标签（4 条）决定。
- **浏览器走查**：demo 32 断言 PASSED、live 假外部 33 断言 PASSED（假发送恰 1 条）。
- **新口径修正**：demo 走查断言数为 32（早期文档写 33，疑把 `function assert(` 定义行计入）；
  live 为 33。

**仍未完成（不得以离线/mock 代替，需真实授权）**：真实 Brave 广域发现、真实商家网页批量抓价、
live 多来源真实收信、长时调度浸泡、真实准确率数据。**离线 1.00 是符合性/回归结果，不是真实准确率。**

## 受控真实垂直切片验收（2026-09-25，真实外部）

一次最小真实 vertical slice 端到端跑通（真实 DeepSeek 解析 + 真实 PB Tech/JB Hi-Fi + 1 次真实 Brave +
真实聚合 check + 恰 1 次真实 SMTP accepted + 假发送复检去重）。装置 `tools_real_live_acceptance.py`；
脱敏证据 `REAL_LIVE_ACCEPTANCE_2026-09-25.md`；交接 `HANDOFF.md` §22。

**实际外部调用（均在授权上限内）**：DeepSeek 2/4、Brave 2/2、商家页面 16/20、SMTP 提交 1/1；
其他主机 0、Mighty Ape 0、购买 0。`.env`/Git/真实任务库未改；离线回归 155 tests OK。

**结论：PASS WITH EXTERNAL CONFIRMATION。** 唯一外部待确认项 = 告警邮件是否**实际到达**收件邮箱。
**SMTP accepted 不等于用户已收到**——在用户确认收信前，不得宣称真实模式端到端验收通过。
（三层验证边界第 3 层由「未验证」更新为「已到 SMTP accepted，收信待确认」。）

**仍未覆盖 / 不得外推**：

- **真实 Brave 广域发现准确率**：本切片仅 1 次真实搜索，且非授权主机候选按边界拒抓，不构成准确率结论；
- **多周期长时调度浸泡**：本切片为一次性 `check_task`，未做真实多周期运行；
- **真实站点批量抓价准确率**（3+ 商家 / 20+ 商品）：未做；
- **跨店价差**：本切片两源同价（NZD 239.00），未出现“一家更便宜”的真实样本；
- 收件人配置在发送前经代码比对与既定授权地址一致（值未回显），但**若日后 `.env` 收件人被改动**，
  同一装置会在发送前中止并仅报告“收件配置需用户核对”，不发送。

**装置缺陷如实记录**：首版 `tools_real_live_acceptance.py` 缺 `storage.init_db`，在预览前以
`no such table: tasks` 失败（属装置自身，非生产代码）；未建任务/未发信，已计入外部调用总数。

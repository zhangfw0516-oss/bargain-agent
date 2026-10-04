# Decisions — M40

## Architecture

- **Single-process scheduler**: Chose `schedule` + threading per task rather than external cron/job queue to keep M40 self-contained and easy to run on a laptop.
- **SQLite persistence**: Selected SQLite for task/quote/event storage because it requires no separate server and provides restart recovery.
- **Decimal strings for money**: All prices and thresholds are stored and compared as `Decimal` to avoid floating-point errors.

## LLM Integration

- **OpenAI-compatible SDK**: Reused the existing `openai` dependency because DeepSeek exposes a compatible API. Configuration switches provider via `.env`.
- **Versioned prompt**: Moved the system prompt to `prompts/parse_instruction.md` so it can be versioned independently of code.
- **JSON mode**: Used `response_format={"type":"json_object"}` as recommended by DeepSeek docs, with strict local schema validation afterward.

## Scraping

- **Single retailer (PB Tech)**: Selected because its product pages expose JSON-LD `Product`/`Offer` with price, currency, and availability. Keeps M40 scope narrow and verifiable.
- **URL allow-list**: Configured via `SCRAPER_ALLOWED_DOMAINS` to prevent accidental access to internal/non-public addresses.

## Notification

- **SMTP with TLS/SSL**: Supports both STARTTLS (port 587) and SSL (port 465) to cover common providers.
- **Limited retries with backoff**: Max 3 attempts by default; failures are recorded in `notification_events`.

## Testing

- **Offline-first tests**: Real LLM/SMTP are not required for `python -m unittest`. Fakes and mocks are injected through public function parameters.
- **Fixture prices are historical**: Test fixtures are annotated with capture time and marked as not live prices.

## Round 2 Review-Fix Decisions

- **Only verified comparable quotes can end a deal event (R1).** A fetch failure or
  incomplete quote proves nothing about the price, so it must not reset the event
  and trigger a duplicate alert. Incomparable observations are recorded as
  `event_reset_skipped` with a reason.
- **One retry budget per deal event (R2).** The scheduler owns the budget
  (`NOTIFICATION_MAX_RETRIES`); `notifier.send_email_notification` defaults to
  `max_retries=0` and `notify_deal` always passes `0`, so the two layers cannot
  multiply. `failed` events persist across restarts and are never silently
  recreated; a new event requires the price to leave and re-enter the deal band.
- **Explicit user confirmation (R3).** `add` prints the parsed summary and requires
  an interactive `y`/`yes`. `--yes` is an explicit opt-in, documented as equivalent
  to confirming the displayed summary. Cancel and EOF create nothing.
- **Manual redirect following (R8).** Automatic redirects are disabled so each hop
  can be validated before any request is sent; the test asserts that a disallowed
  destination is never requested.
- **Atomic product selection (R9).** The scraper picks one JSON-LD `Product` by
  identity score and extracts all fields from its offer in one step, rather than
  merging the first non-null value of each field across products.
- **Conservative parsing over recall (R10).** Ambiguous or missing budget,
  currency or frequency becomes `needs_clarification`. URL extraction matches
  printable ASCII only, so CJK punctuation after a URL is not absorbed.
  Empty/truncated model responses are retried a bounded number of times (2);
  validation failures raise immediately.
- **Configuration fails closed (R11).** Non-positive, NaN or Infinity timeouts and
  out-of-range size/retry values raise `ConfigError` instead of silently using a
  dangerous value. Scoring/permanent SMTP errors are not retried; transient errors
  are. The connection is closed on TLS/send failures.
- **No delivery guarantee beyond bounded retries.** Delivery is best-effort: a crash
  after SMTP acceptance and before the DB write can duplicate a message, and a
  permanent error, exhausted budget or mid-flight stop can drop one. We claim neither
  exactly-once nor at-least-once.

## Round 3 Review-Fix Decisions (F1–F8)

- **A verified exit ends the whole episode (F1).** `reset_active_events` moves
  `pending`, `sent` **and** `failed` to `reset`. Otherwise a single permanent failure
  would block every future deal for that task forever. Incomparable observations still
  cannot end an episode, so an exhausted budget survives a scraper outage.
- **The database decides who checks a task (F2).** The claim is a row in `task_locks`
  keyed by task id, with the resolved database path inside the owner string. This is
  atomic across processes sharing a database and keeps two unrelated databases from
  blocking each other. Inside the claim the task row is re-read, and the status is
  re-checked immediately before sending, so a pause/stop during queueing or mid-fetch
  cancels the send. This narrows but does not eliminate the stop-versus-send race; a
  message already accepted by the server cannot be recalled.
- **Reserve once, then validate on dequeue (F3).** Bounded threads are not a bound on
  the queue, so each due task is reserved exactly once in a `_pending` set limited by
  `max_backlog`. Workers re-read the task and drop it if it is no longer active or due,
  which prevents stale queued copies from running after the task moved on. Errors
  advance `next_check_at` so a failing task cannot become permanently due.
- **The evaluator must not be able to fake success (F4).** Decision logic is a pure
  function (`evaluate_case`) covered offline; any error or mismatch makes the process
  exit non-zero; clarifications are only acceptable when they ask for nothing beyond
  the listed missing fields. The report records model id, UTC time, prompt hash, HEAD
  and call count, and is written to a new timestamped file so history is preserved.
- **Record the symptom, not a theory (F5).** The Git ref-loss description lists raw
  commands and outputs and explicitly marks the cause as unproven, because a linked
  worktree and an extra refs namespace do not by themselves explain a missing
  `refs/heads` entry. `git gc`/`prune` is not recommended, since dangling trees may
  hold recoverable data.
- **Ambiguity is a refusal, not a coin flip (F6).** Product selection returns
  none/unique/ambiguous; a tie or an unidentified candidate among several blocks the
  fetch and is never masked by OpenGraph fallback. Multiple offers are accepted only
  when they agree on price and currency. Conditions that cannot be verified are refused
  at creation instead of being silently ignored.
- **Judge SMTP by status code, not by class (F7).** Class-based classification wrongly
  treated a 454 authentication pause or a 450 deferral as permanent. Codes are now
  checked first (4xx transient, 5xx permanent), `SMTPRecipientsRefused` is inspected per
  recipient, TLS/certificate errors stay permanent, and unknown errors are treated as
  permanent so the budget is not burned without cause.
- **The deadline must include DNS (F8).** The budget uses `time.monotonic()` and starts
  before resolution. DNS runs on a daemon thread with a bounded join: the resolver call
  itself cannot be cancelled, but a stuck lookup can no longer block a pool thread
  indefinitely or escape the deadline.

## Round 4 Review-Fix Decisions (G1–G5)

- **Sending needs a permit, not a status re-read (G1).** Re-reading the status is not a
  boundary: another connection can stop the task between the read and the send.
  `storage.try_begin_send` is a single conditional `UPDATE` that flips
  `send_in_flight` only while the task is still `active`, and pause/stop flip the status
  under the same column in the same database. Exactly one of the two orderings is
  observable, including across processes. When the send wins, the stop still applies but
  the CLI states that an email may already be on its way — a granted permit is never
  revoked because SMTP delivery cannot be recalled. The permit is held only across the
  send itself, never across the fetch, so no long write transaction is held.
- **Reservations belong to the future, not to the function (G2).** `_run_check`'s
  `finally` never runs for a future that is cancelled before starting, so the slot leaked.
  Reservations are now released by a `Future` done callback, which fires for success,
  failure, submission failure (handled inline) and cancellation alike, and release is
  idempotent. The lifecycle is explicit rather than implied: one instance per process
  (a second `run_loop` raises), restartable on the same instance (own job cancelled, stop
  flag cleared, stale reservations dropped), and shutdown is cooperative — cancel queued
  work, let running checks observe `should_abort` before requesting a permit, then wait a
  bounded time. The old comment claiming running checks stop "at their next safe point"
  was wrong and is gone; `should_abort` makes the claim real for the send decision only.
- **Same price is not the same offer (G3).** Comparing only price and currency let array
  order decide stock. Equivalence now covers availability, condition, seller and SKU/MPN,
  and a field present on one offer but absent on another counts as a difference rather
  than an assumed match. Anything not provably identical is refused.
- **A selected product's quote must not be completed from elsewhere (G4).** Pulling a
  price out of OpenGraph after choosing a JSON-LD product cannot prove the value belongs
  to that product. Once a product is selected, missing price or currency fails the fetch.
  The OpenGraph fallback is reserved for pages with no JSON-LD product at all, and it
  reports stock as unknown so an unverifiable page can never become alert-eligible.
- **DNS capacity must be finite, not just the caller's patience (G5).** A bounded join
  limits how long a caller waits, not how many threads exist. A fixed-size resolver pool
  makes concurrency a hard bound: admission waiting is charged to the deadline, a
  running lookup keeps its slot instead of spawning a replacement, and a saturated pool
  fails fast. A resolver stuck inside the OS therefore degrades DNS capacity instead of
  leaking threads — the honest trade-off, documented rather than hidden behind "briefly
  alive" wording.

## Round 5 Review-Fix Decisions (H1–H3)

- **A permit needs an owner, and recovery must be explicit (H1).** If the process dies
  after `send_in_flight = 1`, no `finally` runs and the task can never notify again — a
  permanent loss, not a missed email. Clearing all permits at startup is not acceptable
  either: a permit held by a live sender looks identical, and clearing it invites a
  duplicate. The permit therefore records `send_owner`, release requires that owner, and
  an abandoned permit is cleared only by a deliberate human step
  (`release-send`, confirmation required, with the recorded owner/time shown). Recovery
  does not touch the task's status, so a stopped task never regains sending rights. The
  delivery outcome of the interrupted send is genuinely unknown and is reported as such.
- **One run per process, no restart (H2).** Allowing an in-process restart lets a new run
  reset the stop flag and reservations that still-running workers depend on, so a stale
  worker could see "not stopped" and a stale callback could free a new run's slot.
  Generation tracking would fix that but is more machinery than this milestone needs.
  The simple, honest rule is: the scheduler runs once per process; a second start is
  refused, and the operator exits and restarts the program. The process slot stays held
  when the drain window closes with work still running.
- **Fingerprint the whole offer, not a field whitelist (H3).** A whitelist silently drops
  every difference it does not know about — the earlier version compared stock, condition
  and seller but ignored `size`, `model` and nested `itemOffered`, so two offers for
  different capacities were treated as equivalent. Inverting the rule makes unknown
  differences safe by default: everything except presentation metadata participates in the
  fingerprint, so offers are equivalent only when they are truly identical. This is
  deliberately conservative — it may refuse some genuinely equivalent pairs — and adds no
  spec-selection behaviour.

## Stage B Decisions (商品发现与基础比价)

- **A search request is a different structure from a monitor task.** Schema v2 keeps them
  apart: a search does not require a URL (finding one is the point), and it is never
  permission to monitor. Merging them would have made "I searched for something" look like
  "start monitoring it".
- **Retailer parsing lives only in adapters.** Each shop implements `search` and
  `fetch_offer`. The scheduler, the LLM layer and the CLI never see shop HTML, so adding a
  shop cannot destabilise the monitoring path.
- **Adapters reuse `scraper.fetch_html` instead of calling `requests`.** The allow-list,
  per-hop redirect validation, resolved-IP checks, size cap and monotonic deadline apply to
  search pages too; a second HTTP path would eventually drift weaker than the verified one.
- **Search pages give identity, detail pages give price.** PB Tech's search markup carries
  no reliable price, so candidates are identified cheaply and only the best few are priced
  through the already-verified JSON-LD detail parser. This bounds requests and keeps one
  parsing path for money.
- **Identity is decided by deterministic code, never by a model.** `match_candidate`
  returns same / different / needs_confirmation. A stated mismatch is always "different";
  an unstated constraint is "needs_confirmation", never an assumed match. Conservative
  refusal is preferred over a plausible-looking wrong product.
- **Unknown stays unknown in comparison.** Missing price or mismatched currency is
  *excluded with a stated reason* rather than converted or dropped; unknown shipping and
  unknown condition are printed as unknown. Prices are shown as "excluding unknown
  shipping", never as a total.
- **One source failing must not hide the others.** Each retailer reports its own status, and
  a shop that was not searched at all (disabled by configuration) is reported as
  "not searched" — silence would be indistinguishable from "it had nothing".
- **Marketing colour names are mapped, but the list stays finite.** PB Tech lists the black
  Fitbit Air as "Obsidian"; without a small alias table the correct product would be
  rejected. An unrecognised colour name yields "needs_confirmation" instead of a guess.
- **The default allow-list now names both supported shops.** It remains an explicit support
  boundary; the local `.env` still overrides it, so an operator's narrower list is
  respected rather than bypassed.

## Stage B Review-Fix Decisions (B1–B2)

- **Domain spellings are duplicated facts, so they get a single source plus a host-level
  test.** `mightyaoe.co.nz` appeared in the module name, `BASE`, `domains`, the default
  allow-list, `.env.example`, the fixture, the tests and the docs. Because adapter and
  fixture shared the same typo, the tests could not detect it — a self-consistent wrong
  assumption. The fix is not just the rename: a regression now asserts the *requested
  hostname* (`urlparse(search_url).hostname`, and the URL actually handed to
  `scraper.fetch_html`), plus an assertion that the default allow-list does not contain the
  old spelling. A constant checked only against another constant would not have caught this.
- **A live "does not work" report is only as good as the identity of what was probed.** The
  earlier "proxy refuses Mighty Ape (502)" described the *misspelled* domain, so it proved
  nothing about the real site. After correcting the domain, the true blocker is visible:
  DataDome returns HTTP 403 for the root and the search path alike. Both statements are now
  recorded separately, because the old one must not be read as evidence about the real host.
- **An unverifiable adapter must still fail honestly.** Since the search path and markup
  cannot be confirmed, the path/parameter are single constants marked UNVERIFIED, and
  `search()` wraps fetch failures in `RetailerError`. Discovery then reports the source as
  failed with the HTTP error, so a blocked shop can never be mistaken for "no results".
- **No bypass of a site's access control.** The 403 is respected: no forged User-Agent, no
  cookie or challenge handling, no alternate host discovery. The honest outcome is "this
  retailer cannot be verified from here", not a workaround.
- **A condition the system cannot honour is refused at the entry point, in both entries.**
  `add` already refused `new`/`used`/`refurbished`; `search` silently copied it into a task
  the monitoring module never enforces. The rule now lives once, in behaviour: reject before
  doing any work, with the same exit code (4), and never drop the requirement and continue.
- **In matching, "unknown" and "uninterpretable" are not "no constraint".** A conflicting
  condition is `different`; a condition the listing does not state, or a condition word the
  module cannot resolve, is `needs_confirmation`. Treating an unrecognised word as "no
  constraint" would recreate exactly the silent-ignore failure B2 reported.

## Stage B Dual-Retailer Acceptance Decisions (2026-09-24)

- **A second shop is only "supported" once it is verified, and verification is the gate.**
  Noel Leeming answered 200 on both home and search, but its result grid is purely
  client-side (skeleton placeholders, empty JSON-LD `ItemList`), so no product list can be
  obtained without executing JS. Reachable ≠ usable. JB Hi-Fi was chosen because its search
  page ships a *server-rendered* payload, which is the same shape PB Tech already relies on.
- **Prefer the server-rendered payload over the rendered page.** JB Hi-Fi's visible grid is
  client-side, but the page embeds an `initData` object listing matched products by handle.
  Parsing that is both simpler and more durable than CSS selectors on a JS-rendered grid —
  a theme restyle cannot break it. The rule "search page gives identity, detail page gives
  price" now holds for both verified shops.
- **A fuzzy search must never be reported as "no results".** JB Hi-Fi returns matches even
  for nonsense queries, so an empty candidate list would be a false statement about the
  shop. Conversely, when the payload itself is missing the adapter raises: there the honest
  answer is "we could not read this", not "it had nothing". Both directions avoid the same
  failure mode — inventing a conclusion the data does not support.
- **An accessory is not the product, and the cost of the error is asymmetric.** Letting a
  NZD 69.99 band outrank the NZD 239.00 tracker reads as "Fitbit Air costs 69.99" — a
  materially wrong comparison, not a cosmetic ordering issue. So the rule biases toward
  rejecting words the user did not ask for, accepts that it may occasionally reject
  something legitimate, and keeps a real product name ("Fitbit Charge 6") off the list.
- **Report the comparison, not a verdict.** The two verified shops quoted the same price, so
  the acceptance output says "same price, both in stock" within a stated shop scope and
  query time. It deliberately does not say "cheapest" or "lowest price": the shop set is
  configured and partial, shipping is unknown, and prices are shown excluding it.
## Stage C Decisions (2026-09-24)

- **Sources are task-owned rows, not task fields.** A new `task_sources` table
  (task_id, retailer, url, enabled) plus `quotes.source_id` carries the multi-source
  model. The migration is idempotent and backfills every legacy task with exactly one
  source derived from its URL, so old databases keep working and re-running
  `init_db` never duplicates sources.
- **Selection stays at creation time, identity stays deterministic.** `search --select 1,2`
  accepts only confirmed `same` offers; mixing colours, capacities, currencies or a
  `needs_confirmation` row is refused as a whole. The LLM never decides what gets
  monitored.
- **One notification per episode, decided per task.** `check_task` fetches every enabled
  source, records each observation (price/currency/stock/error) per source, then makes a
  single deal decision from the best buyable comparable quote. Two shops matching in the
  same cycle produce one email naming the chosen shop and the other sources — verified
  live.
- **Failures narrow, never widen, the decision.** A failing or unknown-stock source is
  reported and excluded from judging but does not end the episode; only when *all*
  comparable enabled sources observe a non-deal price does the episode reset. Bias: an
  episode may outlive a broken cheap source rather than being killed by it.
- **Modify semantics split by field.** Changing target price or comparison ends the
  current episode (the old threshold’s judgement no longer applies); changing only the
  frequency never touches events, so no resend is possible.
- **Disabling the last source is allowed but loud.** `sources --disable` refuses nothing,
  but the CLI prints “no enabled sources” and the next check skips with
  `reason=no_enabled_sources` instead of silently passing.
- **`send_fn` is an explicit parameter, not a patch target.** The Stage C verification
  harness initially patched `notifier.notify_deal` via `patch.object`; because
  `check_task`’s keyword default binds at import time the real SMTP path fired once
  toward a non-routable address. Harness fixed to pass `send_fn=` explicitly; product
  tests already injected it. Recorded honestly in KNOWN_ISSUES.md.
- **Anti-invention guard stays strict for colours.** The real-LLM eval surfaced a flake:
  for a Chinese instruction the model sometimes translates “黑色” to "black", which the
  guard rejects because "black" does not literally appear in the instruction. We keep
  the fail-closed behaviour and document the workaround (retry or English phrasing)
  rather than weakening the guard this round.
## Stage D Decisions (2026-09-24)

- **标准库 `http.server`，无新依赖**：课程项目本地工具，避免引入 Flask/FastAPI 及其
  版本负担；单文件单页 UI（原生 JS + fetch），部署 = 运行一个 python 进程。
- **仅绑定 127.0.0.1**：无认证/无 HTTPS 的单用户工具，绝不暴露公网；这是边界而非疏漏。
- **默认演示模式，`--live` 显式开启**：验收路径 100% 离线（离线双语解析器、两家演示
  商家、演示抓取、假通知），从机制上保证"不调用 DeepSeek、不发邮件、不抓真实商家"；
  真实形态仅作为运行选项存在，本阶段不验收。
- **网页层是薄封装**：全部业务规则复用 discovery/scheduler/storage，包括身份门控、
  一次优惠一次通知、来源归属校验与竞态防护；网页层零业务逻辑，杜绝第二套规则漂移。
- **发送函数显式传参**：演示与 live 均显式传 `send_fn=`（C2 教训：默认值导入时绑定，
  patch 无效）。
- **确认规格在 Web 端同样持久化**：select 建任务时把颜色/容量（用户明示或确认标题推断）
  写入 identity，与 CLI 路径一致。
- **搜索结果内存暂存（20 条）**：本地单用户工具不需要持久化会话；过期报错引导重新搜索。
## Stage D Round-2 Decisions (2026-09-24)

- **CSRF 用 double-submit cookie**：无服务端会话的最简可靠方案（`GET /` 与 `/api/csrf`
  发 cookie，POST 必须在 `X-CSRF-Token` 回传同一值）；配合 Origin 白名单与 Host 校验，
  三层全部通过才处理写请求。本机运行不免除这些保护。
- **确认幂等放在服务端**：token 消费登记（URL 集 + 任务 ID）；重复提交同选择返回
  `duplicate: true` 与原任务，不同选择 409。前端防连点只是第一道，不是保证。
- **演示库隔离做成硬约束**：演示模式默认 per-run 临时库，并显式拒绝 `--db-path`
  指向真实任务库（SystemExit），而不是靠用户记得传不同路径。
- **监控循环复用而非重写**：`TaskScheduler` 增加可覆写的 `_perform_check` 钩子
  （产品代码小重构），演示子类只注入抓取/假发送；同进程单次运行、claim、reservation、
  stop 语义全部继承，UI 刷新不重启。
- **浏览器走查入库为可复现工具**：`tools_browser_walkthrough.js`（Playwright 驱动
  agent-browser 所装 Chrome），30 项断言 + 截图；agent-browser 守护进程在多次 CLI
  调用间偶发标签页置空（环境问题），单进程脚本规避，便于复跑。
## Stage D Round-3 Decisions (2026-09-24)

- **预览/确认分离落在服务端**：`/api/search/select` 无副作用，摘要由服务端校验后
  生成（前端只渲染）；创建收敛到 `/api/search/confirm`。未确认任务从不入库，
  "运行中的监控执行未确认任务"因此结构性不可能。
- **幂等用进程内单锁**：本地单服务进程下，`AppState.confirm_selection` 用一把锁
  覆盖检查/创建/登记三步即可获得原子性，不引入新框架；失败不消费 token 以支持重试。
- **_vendor 内置纯 Python 依赖**：演示模式零安装是"发给别人即可运行"的核心体验；
  requests 栈与 bs4 栈全部纯 Python 可内置，charset_normalizer 排除 .pyd 用纯回退；
  openai 因含编译扩展不内置，由 live 启动器首次运行 pip 安装（诚实记录需要联网一次）。
- **配置写入应用目录 .env 而非用户主目录**：随包走、可整包删除；合并写保留已有
  有效配置；status 只报缺失键名不回显任何值；测试通过环境变量重定向实现 hermetic。
- **构建即安检**：打包脚本的禁止名单扫描是交付流水线的一部分，不是手工检查。
## Stage D Round-4 Decisions (2026-09-24)

- **先核心功能、后统一打包（用户决定）**：打包是交付动作，不是开发依赖；先把
  真实收信与持续监控两项验收收口，再一次性做分发包装，避免重复构建。
- **配置分层**：SMTP/DeepSeek 是运维级配置（只在 .env），收件邮箱是用户级配置
  （网页可写）。网页对运维级键一律 400——权限边界做进后端而不是靠 UI 隐藏。
- **事故表述修正**：污染后创建的备份不能作为"已恢复"的证据；交接文档只写可证明
  的事实（测试值已移除、现值为用户明示提供、原始值未确认）。

## Full-Project Integration Acceptance Decisions (2026-09-25)

- **The offline seal is part of the acceptance, not an afterthought.** The suite must prove
  it cannot reach the network, so the guard now seals the *whole current* egress surface.
  The previous seal stopped at `requests.Session` / SMTP / OpenAI / the Brave sender and left
  **DNS unsealed** (acknowledged in §20b); a real fetch would therefore have performed a real
  `getaddrinfo` before being caught. The seal is extended to `socket.getaddrinfo` and
  `socket.create_connection`, plus the module-level `requests.get/post/request` verbs.
  **Why:** "the test suite is offline" must be a structural property, not a claim resting on
  every test happening to inject a fake. **How to apply:** any new outbound boundary added to
  production code must get a seal here in the same change; the guard fails the whole run and
  names the boundary if one is reached.
- **Loopback stays open, everything else is refused.** The Stage D web suite runs a real HTTP
  server on `127.0.0.1`, so a blanket socket seal would break it and tempt a weaker workaround.
  The seal allows loopback / wildcard hosts only and refuses every other host or public IP.
  **Why:** keep the evidence honest (the local server is genuinely offline) without disabling
  the guard for the one suite that needs a real socket. **How to apply:** do not widen the
  loopback allowance to satisfy a test; fix the test to inject a fake instead.
- **DNS refusal is recorded even though the surface error is rewritten.** `BoundedDnsResolver`
  runs the lookup in a worker thread whose `except Exception` turns the raised seal into
  `ScraperError("URL resolves to a non-public IP address.")`; the guard's recorder still
  captures the boundary label, so the end-of-suite assertion fails with `socket.getaddrinfo(...)
  (DNS)`. **Why:** the record is the detector; the per-test error shape stays realistic.
  **How to apply:** rely on the guard's recorded label, not on the exception type, to identify
  a leak.
- **Only the test infrastructure changed this round.** No production module, `.env` / credential,
  Git metadata or package was touched. **Why:** the round is an integration acceptance; changing
  product code would invalidate the "frozen" verification. **How to apply:** treat the 547-test
  figure and the seal as the baseline for the next stage, and add offline datasets/regressions
  on top without perturbing product behaviour.
- **Offline 1.00 is never "accuracy".** The evaluation harness measures conformance to the
  project's own documented rules; the report and hand-off keep the offline result labelled
  "consistency/regression" and list real-site/real-LLM/real-receipt figures as **not done**.
  **Why:** the expected labels and the implementation share the same authors, so a high pass
  rate proves conformance, not generalisation. **How to apply:** never quote the offline 1.00
  as a real-world accuracy in the course report.

## SerpApi / Multi-Agent Refactor (2026-10-04)

Supersedes the scraping, retailer-adapter, regex identity-matching and demo-mode decisions above.

- **ADR-R1 — SerpApi Google Shopping replaces retailer scraping.** Direct scraping kept failing (Mighty Ape DataDome 403) and every shop needed its own adapter. Google Shopping returns structured listings and seller offers for all merchants Google indexes in NZ, so discovery and monitoring use SerpApi; pasted product URLs use Tavily/Firecrawl extraction plus the price-extractor agent. Trade-off: per-call cost and quota, mitigated by `SERPAPI_DAILY_MAX_CALLS`, `FIND_MAX_QUERIES` and `MONITOR_MIN_FREQUENCY_MINUTES`.
- **ADR-R2 — One Find path: a LangGraph pipeline of agents.** understand → rewrite → search (code) → select → describe, with one bounded select→rewrite retry. LangGraph only orchestrates; model calls go through `llm.call_json` so validation and retries stay ours. The standard Search flow and the old iterative loop were deleted.
- **ADR-R3 — Agents choose, code decides.** Agents may only reference server-assigned ids; every output is validated; budget/condition vetoes, deal thresholds, deduplication and send permits are deterministic; every agent has a deterministic fallback.
- **ADR-R4 — Offer verifier is a veto, not a gate.** Google's product id is the identity. A condition pre-filter rejects unrequested second-hand offers in code; the verifier agent may only downgrade offers, its verdicts are cached per offer, and an unavailable verifier leaves offers `unverified` (usable for alerts, blocking episode resets).
- **ADR-R5 — Discover everywhere, alert from trusted merchants.** All merchants are shown; only `TRUSTED_MERCHANTS` trigger emails by default (`ALERT_TRUSTED_ONLY`). A pasted URL counts as trusted because the user chose it.
- **ADR-R6 — Pluggable providers.** `ShoppingSearch`, `OfferSource` and `PageExtractor` protocols with a registry; providers without keys are reported unavailable. Browser-automation agents were rejected (cost, latency, anti-bot concerns); MCP providers are deferred.
- **ADR-R7 — No demo mode.** Demo branches were spread through the web layer; tests now inject fakes through `webapp.Services` and the scheduler's `observer`/`verifier`/`send_fn`.
- **ADR-R8 — Schema versioning.** `PRAGMA user_version` = 1 with an automatic `<db>.pre-v1.bak` backup before the first migration; legacy sources become `url` sources.

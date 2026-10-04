"""In-process scheduler for periodic price checks with SQLite persistence."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import schedule

import config
import discovery
import notifier
import scraper
import storage

logger = logging.getLogger(__name__)

SCHEDULE_TAG = "bargain-agent"

#: The import-time default quote fetcher. The web-search source path swaps in the
#: host-restricted fetcher only when the caller did not inject a custom one
#: (demo/tests inject their own), so the identity check uses this captured object
#: rather than the current module attribute (which tests may patch).
_DEFAULT_QUOTE_FETCHER = scraper.fetch_quote

# At most one TaskScheduler may run in this process, and it runs once: the scheduler
# is not restartable in-process. Coordination *between* processes is done by
# storage.try_begin_send / set_task_status_atomic.
_active_scheduler: "TaskScheduler | None" = None
_scheduler_has_run = False
_active_scheduler_lock = threading.Lock()


class SchedulerError(RuntimeError):
    """Raised when the scheduler cannot run."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _is_due(task: dict[str, Any]) -> bool:
    try:
        return datetime.fromisoformat(task["next_check_at"]) <= _now()
    except Exception:
        return True


def _normalised_db_path(db_path: Path | None) -> str:
    """Resolve the database path so lock identity is comparable across processes."""
    try:
        return str((db_path or config.database_path()).resolve())
    except Exception:
        return str(db_path or "default")


def _lock_owner(db_path: Path | None, task_id: int) -> str:
    """Lock identity: normalised DB path + task id + process/thread + unique token."""
    return (
        f"{_normalised_db_path(db_path)}"
        f"|task={task_id}"
        f"|pid={os.getpid()}"
        f"|thread={threading.get_ident()}"
        f"|{uuid.uuid4().hex[:8]}"
    )


def _send_owner(db_path: Path | None, task_id: int) -> str:
    """Identity of the permit holder, so only it (or a human recovery step) releases it."""
    return (
        f"{_normalised_db_path(db_path)}"
        f"|send|task={task_id}"
        f"|pid={os.getpid()}"
        f"|thread={threading.get_ident()}"
        f"|{uuid.uuid4().hex[:8]}"
    )


def _is_comparable_quote(task: dict[str, Any], quote: dict[str, Any]) -> bool:
    """Return True when the quote is valid enough to compare against the task."""
    if quote.get("error"):
        return False
    if quote.get("price") is None or quote.get("currency") is None:
        return False
    if task["currency"] != quote["currency"]:
        return False
    if quote.get("in_stock") is None:
        return False
    try:
        Decimal(quote["price"])
        Decimal(task["target_price"])
    except Exception:
        return False
    return True


def is_deal(task: dict[str, Any], quote: dict[str, Any]) -> bool:
    """Return True when the quote satisfies the task threshold and is buyable."""
    if not _is_comparable_quote(task, quote):
        return False
    if quote.get("in_stock") is not True:
        return False
    try:
        current = Decimal(quote["price"])
        target = Decimal(task["target_price"])
    except Exception:
        return False
    if task["comparison"] == "lt":
        return current < target
    if task["comparison"] == "lte":
        return current <= target
    return False


def task_identity(task: dict[str, Any]) -> dict[str, Any] | None:
    """The confirmed product identity stored at multi-source task creation, or None.

    Legacy single-URL tasks carry no identity, which means "created before the
    discovery flow existed" — the detail URL *was* the user's confirmation, so no
    extra identity gate applies.
    """
    raw = task.get("identity_json")
    if not raw:
        return None
    try:
        identity = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return identity if isinstance(identity, dict) else None


def quote_identity_verdict(identity: dict[str, Any] | None, quote: dict[str, Any]) -> str:
    """Classify a fetched quote against the task's confirmed identity.

    Returns ``discovery.MATCH_SAME``, ``MATCH_NEEDS_CONFIRMATION`` or
    ``MATCH_DIFFERENT``:

    - The quote name must contain **every** significant model token of the
      confirmed product — one shared word ("Google Fitbit Charge 6 Black" shares
      only "fitbit") is never enough. Missing tokens are needs_confirmation, so a
      wrong-model page can neither trigger a deal nor end one. The quote's own
      structured ``brand``/``variant`` (same selected JSON-LD product) may stand in
      for tokens the page name omits; the search card never does.
    - A quote whose name contradicts its own structured ``brand``/``variant`` — the
      name says "Bose" while the brand says "Sony", or the name says 30 ml while the
      variant says 60 ml — is **different**: the contradiction cannot be hidden by
      concatenation. Complementary fields (name omits the brand, the structured brand
      supplies it) are not a conflict and still confirm.
    - A generation digit directly following a model token ("Fitbit Air 2") that
      the confirmed product does not carry is **different**.
    - An accessory word the user never asked for — on names of **all** lengths —
      is **different**, except for :data:`FEATURE_PHRASES` ("battery life") and
      contextual body nouns ("... for Blemish-Prone Skin"), which are products.
    - Colour and capacity (explicitly requested *or* inferred from the confirmed
      title) must agree; a stated different value is **different**, an unstated
      one is needs_confirmation.
    - ``identity is None`` (legacy single-URL task) is always same.
    """
    if identity is None:
        return discovery.MATCH_SAME

    name = str(quote.get("product_name") or "")
    name_tokens_list = discovery._tokens(name)
    name_tokens = set(name_tokens_list)
    product_name = identity.get("product_name") or ""
    confirmed_title = identity.get("confirmed_title") or ""
    known = product_name + " " + confirmed_title
    known_tokens = set(discovery._tokens(known))
    product_token_set = set(discovery._significant_tokens(product_name))

    # Structured identity the page itself stated for the *same selected* JSON-LD
    # product: brand and size/variant may fill a name that omits the brand or the
    # spec at search time (see `discovery._detail_identity`). Without this, an offer
    # confirmed from those fields could never pass its own monitoring check. Never
    # the search card, and never OpenGraph data.
    brand = str(quote.get("brand") or "")
    variant = str(quote.get("variant") or "")
    evidence_tokens = set(name_tokens)
    evidence_tokens.update(discovery._tokens(brand))
    evidence_tokens.update(discovery._tokens(variant))

    # 0. A page that contradicts itself is `different` before anything else: its name
    # states a different brand/aspect than its own structured brand/variant (the same
    # selected JSON-LD product). Concatenating the two would hide the contradiction and
    # let a Bose-named page with a Sony brand read as a Sony match. Complementary
    # fields — the name omits the brand and the structured brand supplies it — are not
    # a conflict and still confirm (see `discovery._page_self_conflicts`).
    if discovery._page_self_conflicts(name, brand, variant, product_token_set):
        return discovery.MATCH_DIFFERENT

    # 1. Model completeness: one shared word is not a match.
    missing_model = [t for t in product_token_set if t not in evidence_tokens]
    if missing_model:
        return discovery.MATCH_NEEDS_CONFIRMATION

    # 2. Accessory scan on names of ALL lengths; feature phrases ("battery life") and
    # contextual body nouns ("... for Blemish-Prone Skin") are exempt.
    if discovery.flagged_accessory_words(name_tokens_list, known_tokens):
        return discovery.MATCH_DIFFERENT

    stated_capacity = discovery._extract_capacity(name) or discovery._extract_capacity(variant)
    # 3. Generation: a digit directly following a model token must be one the
    # confirmed product carries; a generation the product carries must be stated.
    # Capacity digits of the quote itself ("Fitbit Air 64 GB") are not generations.
    capacity = (identity.get("capacity") or "").strip().lower()
    identity_gen = {t for t in discovery._tokens(product_name) if t.isdigit()}
    if capacity:
        identity_gen = {g for g in identity_gen if g not in capacity}
    quote_capacity_digits = discovery._capacity_digit_tokens(stated_capacity)
    for i, tok in enumerate(name_tokens_list[:-1]):
        if tok in product_token_set and name_tokens_list[i + 1].isdigit():
            nxt = name_tokens_list[i + 1]
            if nxt in quote_capacity_digits:
                continue
            if nxt not in identity_gen:
                return discovery.MATCH_DIFFERENT
    unstated_gen = [g for g in identity_gen if g not in name_tokens]
    if unstated_gen:
        return discovery.MATCH_NEEDS_CONFIRMATION

    # 4. Colour: canonical comparison, so "Obsidian" vs "black" agrees. When the
    # request named no colour, the confirmed title's colour binds — selecting
    # "Obsidian" makes it the confirmed spec even if the user never said "black".
    raw_colour = identity.get("colour") or discovery._extract_colour(confirmed_title)
    requested = discovery._canonical_colour(raw_colour)
    if requested is not None:
        stated = discovery._canonical_colour(name) or discovery._canonical_colour(variant)
        if stated is None:
            return discovery.MATCH_NEEDS_CONFIRMATION
        if stated != requested:
            return discovery.MATCH_DIFFERENT

    # 5. Capacity/spec: same inference rule as for colour.
    requested_capacity = capacity or discovery._extract_capacity(confirmed_title)
    if requested_capacity:
        if stated_capacity is None:
            return discovery.MATCH_NEEDS_CONFIRMATION
        if stated_capacity != requested_capacity:
            return discovery.MATCH_DIFFERENT

    return discovery.MATCH_SAME


def _send_event_notification(
    event: dict[str, Any] | None,
    task: dict[str, Any],
    quote: dict[str, Any],
    send_fn: Callable[[str, dict, dict], dict[str, Any]],
    max_retries: int,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Create or retry an event notification and persist its status.

    The scheduler provides max_retries as the total SMTP attempt budget for one
    deal episode. Failed events are cleared only when the price verifiably leaves
    the deal band (see storage.reset_active_events).
    """
    if event is None:
        event_id = storage.create_event(task["id"], quote.get("quote_id"), db_path=db_path)
        event = {"id": event_id, "status": "pending", "attempts": 0}

    attempts = event["attempts"]
    if event["status"] == "sent":
        return {"event_id": event["id"], "notified": False, "reason": "already_sent"}
    if event["status"] == "failed":
        return {"event_id": event["id"], "notified": False, "reason": "exhausted"}
    if attempts >= max_retries:
        storage.update_event(
            event["id"],
            "failed",
            error="Max retry attempts exceeded",
            attempts=attempts,
            db_path=db_path,
        )
        return {"event_id": event["id"], "notified": False, "reason": "max_retries_exceeded"}

    result = send_fn(task["notify_email"], task, quote)
    attempts += 1
    if result.get("success"):
        storage.update_event(
            event["id"],
            "sent",
            error=None,
            attempts=attempts,
            sent_at=_iso(_now()),
            db_path=db_path,
        )
        return {"event_id": event["id"], "notified": True, "attempts": attempts}

    retryable = result.get("retryable", True)
    if attempts >= max_retries or not retryable:
        new_status = "failed"
    else:
        new_status = "pending"
    storage.update_event(
        event["id"],
        new_status,
        error=result.get("error"),
        attempts=attempts,
        db_path=db_path,
    )
    return {
        "event_id": event["id"],
        "notified": False,
        "attempts": attempts,
        "error": result.get("error"),
        "reason": "permanent_error" if not retryable else "retry_exhausted" if attempts >= max_retries else "pending_retry",
    }


def _failed_quote(source: dict[str, Any], product_name: str, error: str, at: str) -> dict[str, Any]:
    """A source observation that did not yield a usable quote."""
    return {
        "source_id": source["id"],
        "url": source["url"],
        "error": error,
        "fetched_at": at,
        "in_stock": None,
        "price": None,
        "currency": None,
        "product_name": product_name,
        "retailer": source.get("retailer"),
    }


def _fetcher_for_source(
    source: dict[str, Any],
    default_fetcher: Callable[[str], dict[str, Any]],
) -> Callable[[str], dict[str, Any]]:
    """Pick the fetcher for one source based on where its URL came from.

    - ``web_search`` sources are product pages found on the open web: they are
      fetched restricted to the candidate's own HTTPS host (cross-host redirects,
      private addresses and oversized pages all fail). The monitoring loop never
      calls any search provider — Brave runs once during discovery, never again.
    - ``retailer_adapter`` sources (and anything without a kind, i.e. legacy rows)
      use the injected/default fetcher unchanged.
    """
    if source.get("source_kind") == "web_search" and default_fetcher is _DEFAULT_QUOTE_FETCHER:
        return scraper.fetch_quote_on_host_llm
    return default_fetcher


def _observe_sources(
    fresh: dict[str, Any],
    sources: list[dict[str, Any]],
    fetcher: Callable[[str], dict[str, Any]],
    db_path: Path | None,
) -> list[dict[str, Any]]:
    """Fetch every enabled source, persisting each observation independently.

    One source failing never stops the others, and never counts as "the deal ended":
    an unreachable shop is not evidence about its price.
    """
    observations: list[dict[str, Any]] = []
    for source in sources:
        started = _now()
        source_fetcher = _fetcher_for_source(source, fetcher)
        try:
            quote = source_fetcher(source["url"])
        except scraper.ScraperError as exc:
            quote = _failed_quote(
                source, fresh["product_name"], str(exc), _iso(started)
            )
        except Exception as exc:  # noqa: BLE001 - one bad source must not sink the task
            logger.exception("Unexpected error fetching %s", source["url"])
            quote = _failed_quote(
                source, fresh["product_name"], f"{type(exc).__name__}: {exc}", _iso(started)
            )
        quote.setdefault("fetched_at", _iso(started))
        quote.setdefault("url", source["url"])
        # The source row is the authoritative shop label (a display name such as
        # "JB Hi-Fi"); the scraper only knows the host. The observation must report
        # the same shop name the user selected, so override rather than default.
        quote["retailer"] = source.get("retailer") or quote.get("retailer")
        quote["source_id"] = source["id"]
        quote["quote_id"] = storage.save_quote(
            fresh["id"], quote, db_path=db_path, source_id=source["id"]
        )
        storage.record_source_check(source["id"], error=quote.get("error"), db_path=db_path)
        observations.append(quote)
    return observations


def _best_buyable_quote(
    task: dict[str, Any], observations: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Cheapest observation that is genuinely comparable and in stock.

    Only an in-stock, same-currency, priced observation may represent the task, so an
    out-of-stock or failed source can never be reported as the best offer.
    """
    usable = [
        q for q in observations
        if _is_comparable_quote(task, q) and q.get("in_stock") is True
    ]
    if not usable:
        return None

    def sort_key(q: dict[str, Any]) -> tuple:
        return (Decimal(str(q["price"])), int(q.get("source_id") or 0))

    return sorted(usable, key=sort_key)[0]


def _other_source_report(
    best: dict[str, Any], observations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Describe every source that did not produce the chosen offer.

    The alert must say what happened to the other shops rather than staying silent
    about them, so the recipient can judge how complete the comparison is.
    """
    report: list[dict[str, Any]] = []
    for quote in observations:
        if quote is best:
            continue
        if quote.get("error"):
            state = f"could not be checked ({quote['error']})"
        elif quote.get("price") is None or quote.get("currency") is None:
            state = "no usable price"
        elif quote.get("in_stock") is not True:
            state = f"{quote.get('currency')} {quote.get('price')} (not in stock)"
        else:
            state = f"{quote.get('currency')} {quote.get('price')}"
        report.append(
            {
                "retailer": quote.get("retailer"),
                "url": quote.get("url"),
                "state": state,
                "checked_at": quote.get("fetched_at"),
            }
        )
    return report


def check_task(
    task: dict[str, Any],
    *,
    fetcher: Callable[[str], dict[str, Any]] = scraper.fetch_quote,
    send_fn: Callable[[str, dict, dict], dict[str, Any]] = notifier.notify_deal,
    max_retries: int | None = None,
    db_path: Path | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run one check for a task and update persistence.

    Stage C: a task may watch several confirmed sources. Every enabled source is
    fetched and recorded; the deal decision is made once per task from the best
    buyable observation, so two shops both matching never produce two emails.

    Concurrency boundaries:

    - The check itself is claimed by `storage.acquire_task_lock`, so two processes
      sharing a database cannot check the same task simultaneously.
    - Sending is gated by `storage.try_begin_send`, a conditional UPDATE that only
      succeeds while the task is still `active` and no send is in flight. Pause and
      stop flip the same column in the same transaction, so either "stop wins"
      (no send is started) or "send wins" (the stop still applies, but a message may
      already be on its way). A permit already granted is **never** revoked: mail
      handed to SMTP cannot be recalled.
    - `should_abort` is consulted immediately before the permit is requested, so a
      stopping scheduler cancels notifications that have not started yet.
    """
    max_retries = max_retries if max_retries is not None else config.notification_max_retries()
    task_id = task["id"]
    owner = _lock_owner(db_path, task_id)

    if not storage.acquire_task_lock(task_id, owner, db_path=db_path):
        logger.warning("Task %s is already being checked elsewhere; skipping", task_id)
        return {"task_id": task_id, "skipped": True, "reason": "lock_held"}

    try:
        # The snapshot passed in may be stale; prefer the persisted row.
        fresh = storage.get_task(task_id, db_path=db_path) or task
        if fresh.get("status") != "active":
            logger.info("Task %s is %s; skipping check", task_id, fresh.get("status"))
            return {"task_id": task_id, "skipped": True, "reason": f"task_{fresh.get('status')}"}

        start = _now()
        sources = storage.list_task_sources(task_id, db_path=db_path, enabled_only=True)
        if not sources:
            logger.warning("Task %s has no enabled sources; nothing to check", task_id)
            return {
                "task_id": task_id,
                "skipped": True,
                "reason": "no_enabled_sources",
                "sources_checked": 0,
            }

        observations = _observe_sources(fresh, sources, fetcher, db_path)

        # Re-read the task after fetching: a threshold/comparison change that
        # committed while the fetches were in flight must govern this decision,
        # never the stale snapshot taken before it.
        fresh = storage.get_task(task_id, db_path=db_path) or fresh

        # Identity gate: each observation is classified against the confirmed
        # product identity. A detail page that now serves an accessory or a
        # different colour/capacity must not drive the deal decision in either
        # direction. Legacy tasks without identity pass unchanged.
        identity = task_identity(fresh)
        for q in observations:
            q["identity_verdict"] = quote_identity_verdict(identity, q)

        best = _best_buyable_quote(
            fresh,
            [q for q in observations if q.get("identity_verdict") == discovery.MATCH_SAME],
        )
        duration_ms = int((_now() - start).total_seconds() * 1000)

        active_event = storage.get_active_event(task_id, db_path=db_path)
        result: dict[str, Any] = {
            "task_id": task_id,
            "quote_id": best.get("quote_id") if best else None,
            "is_deal": False,
            "notified": False,
            "duration_ms": duration_ms,
            "sources_checked": len(observations),
            "sources_failed": sum(1 for q in observations if q.get("error")),
            "observations": [
                {
                    "source_id": q.get("source_id"),
                    "retailer": q.get("retailer"),
                    "price": q.get("price"),
                    "currency": q.get("currency"),
                    "in_stock": q.get("in_stock"),
                    "error": q.get("error"),
                    "identity_verdict": q.get("identity_verdict"),
                }
                for q in observations
            ],
        }

        if best is not None and is_deal(fresh, best):
            result["is_deal"] = True
            result["chosen_retailer"] = best.get("retailer")
            result["chosen_price"] = best.get("price")
            if should_abort is not None and should_abort():
                result["notification_cancelled"] = True
                result["notification_cancelled_reason"] = "scheduler_stopping"
            else:
                send_owner = _send_owner(db_path, task_id)
                if not storage.try_begin_send(
                    task_id,
                    send_owner,
                    db_path=db_path,
                    # Deny the permit when the conditions changed since the
                    # decision, or when the triggering source was disabled
                    # mid-check — the alert must never contradict the user's
                    # latest instruction.
                    config_version=fresh.get("config_version"),
                    source_id=best.get("source_id"),
                ):
                    # Either the task is no longer active, another send is already
                    # in flight, the conditions changed mid-check, or the chosen
                    # source was disabled. A permit left behind by a process that
                    # died also lands here, and is cleared by the explicit
                    # recovery command.
                    result["notification_cancelled"] = True
                    result["notification_cancelled_reason"] = "send_permit_denied"
                else:
                    try:
                        # The alert reports the chosen shop and, explicitly, the state of
                        # every other source; silence would imply they were fine.
                        notification_quote = dict(best)
                        notification_quote["other_sources"] = _other_source_report(best, observations)
                        notification = _send_event_notification(
                            active_event, fresh, notification_quote, send_fn, max_retries, db_path=db_path
                        )
                        result["notified"] = notification.get("notified", False)
                        result["event_id"] = notification.get("event_id")
                        result["notification_error"] = notification.get("error")
                        result["notification_reason"] = notification.get("reason")
                    finally:
                        storage.end_send(task_id, send_owner, db_path=db_path)
        else:
            # End the episode only when *every* enabled source produced a comparable
            # observation, confirmed as the same product, and none of them is a
            # deal. A single failure, an unknown stock level, or a page that now
            # serves a different product/accessory is not evidence that the deal
            # ended.
            if active_event is not None:
                all_comparable = len(observations) == len(sources) and all(
                    _is_comparable_quote(fresh, q)
                    and q.get("identity_verdict") == discovery.MATCH_SAME
                    for q in observations
                )
                if all_comparable:
                    reset_count = storage.reset_active_events(task_id, db_path=db_path)
                    result["event_reset"] = True
                    result["events_reset"] = reset_count
                else:
                    result["event_reset_skipped"] = True
                    result["event_reset_reason"] = (
                        "identity_mismatch"
                        if all(
                            _is_comparable_quote(fresh, q) for q in observations
                        )
                        else "not_all_sources_comparable"
                    )

        next_check = _iso(_now() + timedelta(minutes=fresh["frequency_minutes"]))
        storage.update_last_check(task_id, best.get("quote_id") if best else None, db_path=db_path)
        storage.update_next_check(task_id, next_check, db_path=db_path)
        logger.info(
            "Task %s checked %s source(s): deal=%s notified=%s duration=%sms",
            task_id, len(observations), result["is_deal"], result["notified"], duration_ms,
        )
        return result
    finally:
        storage.release_task_lock(task_id, owner, db_path=db_path)


class TaskScheduler:
    """Single-process scheduler that loads tasks from SQLite and runs checks.

    Lifecycle contract:

    - **One run per process, and never restartable.** `run_loop()` claims a process
      slot; a second call in the same process — on this object or on a new one — is
      refused with `SchedulerError`. To run again, exit the program and start it
      again. Cross-process coordination is the database's job.
    - Stopping cancels queued work, asks running checks to abort before they acquire
      a send permit, and waits a bounded time for reservations to drain. If work is
      still running when that window closes, the process slot stays held so a new
      scheduler cannot start alongside the stale workers.
    - A notification already handed to SMTP is not recalled.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        max_workers: int = 5,
        max_backlog: int | None = None,
        shutdown_timeout: float = 5.0,
    ) -> None:
        self.db_path = db_path
        self.max_workers = max(1, int(max_workers))
        self.max_backlog = max_backlog if max_backlog is not None else self.max_workers * 2
        self.shutdown_timeout = max(0.0, float(shutdown_timeout))
        self._stop_event = threading.Event()
        self._executor: ThreadPoolExecutor | None = None
        self._job = None
        self._pending: set[int] = set()
        self._pending_lock = threading.Lock()
        self._started = False

    def _due_tasks(self) -> list[dict[str, Any]]:
        now = _iso(_now())
        return storage._fetchall(
            self.db_path,
            "SELECT * FROM tasks WHERE status = 'active' AND next_check_at <= ? ORDER BY id",
            (now,),
        )

    def _reserve(self, task_id: int) -> bool:
        """Reserve a queued slot. False if already reserved or backlog is full."""
        with self._pending_lock:
            if task_id in self._pending:
                return False
            if len(self._pending) >= self.max_backlog:
                logger.warning("Pending backlog is full (%s); deferring task %s", self.max_backlog, task_id)
                return False
            self._pending.add(task_id)
            return True

    def _release(self, task_id: int) -> None:
        """Release a reservation. Idempotent; safe from completion callbacks."""
        with self._pending_lock:
            self._pending.discard(task_id)

    def pending_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def _make_release_callback(self, task_id: int):
        def _callback(future) -> None:
            # Runs for success, failure *and* cancellation, which is what makes a
            # cancelled-but-never-started future give its reservation back.
            self._release(task_id)
            try:
                future.exception()
            except Exception:
                pass

        return _callback

    def _defer_task(self, task_id: int) -> None:
        """Advance next_check after an unexpected error so a task cannot stay
        permanently due and be retried in a tight loop."""
        try:
            fresh = storage.get_task(task_id, self.db_path)
            if fresh is None:
                return
            minutes = max(1, int(fresh.get("frequency_minutes") or 60))
            storage.update_next_check(task_id, _iso(_now() + timedelta(minutes=minutes)), db_path=self.db_path)
        except Exception:
            logger.exception("Could not defer task %s after an error", task_id)

    def _perform_check(self, fresh: dict[str, Any]) -> dict[str, Any]:
        """The actual check invocation. Subclasses may override this to inject a
        different fetcher/sender (e.g. the demo web UI); the claim, reservation,
        stop-flag and scheduling machinery above stay shared and unchanged."""
        return check_task(fresh, db_path=self.db_path, should_abort=self._stop_event.is_set)

    def _run_check(self, task_id: int) -> None:
        # Reservation is released by the future's done callback, not here.
        try:
            if self._stop_event.is_set():
                return
            # Re-validate on dequeue: the task may have been paused/stopped, or
            # already rescheduled, while it sat in the queue.
            fresh = storage.get_task(task_id, self.db_path)
            if fresh is None or fresh.get("status") != "active":
                return
            if not _is_due(fresh):
                return
            self._perform_check(fresh)
        except Exception:
            logger.exception("Unhandled error checking task %s", task_id)
            self._defer_task(task_id)

    def _tick(self) -> None:
        if self._stop_event.is_set() or self._executor is None:
            return
        executor = self._executor
        for task in self._due_tasks():
            if self._stop_event.is_set():
                break
            task_id = task["id"]
            if not self._reserve(task_id):
                continue
            try:
                future = executor.submit(self._run_check, task_id)
            except RuntimeError:
                # Executor already shut down; give the reservation straight back.
                self._release(task_id)
                break
            future.add_done_callback(self._make_release_callback(task_id))

    def run_once(self, task_id: int) -> dict[str, Any]:
        """Run a single check for one task immediately."""
        task = storage.get_task(task_id, self.db_path)
        if not task:
            raise SchedulerError(f"Task {task_id} not found")
        if task["status"] != "active":
            raise SchedulerError(f"Task {task_id} is {task['status']}")
        result = check_task(task, db_path=self.db_path)
        if result.get("skipped"):
            raise SchedulerError(f"Task {task_id} was not checked: {result.get('reason')}")
        return result

    def _claim_process_slot(self) -> None:
        """Claim the single per-process scheduler slot.

        The scheduler runs once per process. A previous run may still have workers
        draining, and a second run would reset the stop flag and the reservations
        those workers rely on, so a repeat start — on this object or a new one — is
        refused. Exit the program and start it again instead.
        """
        global _active_scheduler, _scheduler_has_run
        with _active_scheduler_lock:
            if _active_scheduler is not None:
                raise SchedulerError(
                    "Another scheduler is already running in this process. "
                    "Exit the program before starting a new one."
                )
            if self._started or _scheduler_has_run:
                raise SchedulerError(
                    "A scheduler has already run in this process and cannot be started again. "
                    "Exit the program and start it again."
                )
            _active_scheduler = self
            self._started = True
            _scheduler_has_run = True

    def _release_process_slot(self, *, drained: bool) -> None:
        """Release the slot only when no worker is still running.

        If the drain window closed with checks in flight, the slot stays held so no
        new scheduler can start while stale workers still share the old stop flag.
        """
        global _active_scheduler
        if not drained:
            logger.warning(
                "Keeping the process scheduler slot: %s check(s) were still running when the "
                "shutdown window closed. Exit the process before starting another scheduler.",
                self.pending_count(),
            )
            return
        with _active_scheduler_lock:
            if _active_scheduler is self:
                _active_scheduler = None

    def _cancel_own_job(self) -> None:
        """Cancel only the schedule job this instance registered."""
        if self._job is not None:
            try:
                schedule.cancel_job(self._job)
            except Exception:
                logger.debug("Could not cancel schedule job", exc_info=True)
            self._job = None

    def _drain_pending(self) -> bool:
        """Wait a bounded time for running checks to release their reservations.

        Returns True when every reservation has been released.
        """
        deadline = time.monotonic() + self.shutdown_timeout
        while time.monotonic() < deadline:
            if self.pending_count() == 0:
                return True
            time.sleep(0.05)
        remaining = self.pending_count()
        if remaining:
            logger.warning("%s task(s) still running when the shutdown window closed", remaining)
        return remaining == 0

    def run_loop(self) -> None:
        """Run the scheduler until stop() is called.

        Not restartable: a second call in this process (on this object or a new one)
        is refused with `SchedulerError`. Run it again by exiting the program first.
        """
        self._claim_process_slot()
        drained = False
        try:
            storage.init_db(self.db_path)
            self._job = schedule.every(1).minutes.do(self._tick)
            self._job.tag(SCHEDULE_TAG)
            self._executor = ThreadPoolExecutor(max_workers=self.max_workers)
            logger.info("Scheduler started (workers=%s, backlog=%s)", self.max_workers, self.max_backlog)
            try:
                self._tick()
                while not self._stop_event.is_set():
                    schedule.run_pending()
                    time.sleep(1)
            finally:
                self._stop_event.set()
                self._cancel_own_job()
                executor = self._executor
                self._executor = None
                if executor is not None:
                    # Queued-but-not-started work is cancelled; its done callbacks
                    # release the reservations. Running checks observe should_abort
                    # before requesting a send permit.
                    executor.shutdown(wait=False, cancel_futures=True)
                drained = self._drain_pending()
                logger.info("Scheduler stopped")
        finally:
            self._release_process_slot(drained=drained)

    def stop(self) -> None:
        self._stop_event.set()

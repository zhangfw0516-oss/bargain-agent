"""In-process scheduler for periodic price checks with SQLite persistence."""

from __future__ import annotations

import hashlib
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
import llm
import notifier
import pricing
import storage
from agents import offer_verifier
from agents.offer_verifier import DIFFERENT, NEEDS_CONFIRMATION, SAME, UNVERIFIED

logger = logging.getLogger(__name__)

SCHEDULE_TAG = "bargain-agent"

#: ``(source_row, task_row) -> [quote, ...]``; one source may yield many sellers.
Observer = Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]
#: ``(product, offers) -> AgentResult`` with ``{offer_id: (verdict, reason)}``.
Verifier = Callable[[dict[str, Any], list[dict[str, Any]]], Any]

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
        Decimal(pricing.effective_price(task, quote))
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
        current = Decimal(pricing.effective_price(task, quote))
        target = Decimal(task["target_price"])
    except Exception:
        return False
    if task["comparison"] == "lt":
        return current < target
    if task["comparison"] == "lte":
        return current <= target
    return False


def drop_deal(task: dict[str, Any], quote: dict[str, Any]) -> bool:
    """True when the quote is ``drop_percent`` below the task's reference price."""
    percent, reference = task.get("drop_percent"), task.get("reference_price")
    if not percent or not reference or not _is_comparable_quote(task, quote) or quote.get("in_stock") is not True:
        return False
    try:
        limit = Decimal(reference) * (Decimal(100) - Decimal(percent)) / Decimal(100)
        return Decimal(pricing.effective_price(task, quote)) <= limit
    except Exception:
        return False


def deal_reason(task: dict[str, Any], quote: dict[str, Any] | None) -> str | None:
    """``"threshold"``, ``"drop"`` or None for the best buyable quote."""
    if quote is None:
        return None
    if is_deal(task, quote):
        return "threshold"
    if drop_deal(task, quote):
        return "drop"
    return None


def task_identity(task: dict[str, Any]) -> dict[str, Any] | None:
    """The product identity stored when the task was confirmed (title, constraints)."""
    raw = task.get("identity_json")
    if not raw:
        return None
    try:
        identity = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return identity if isinstance(identity, dict) else None


def _offer_hash(quote: dict[str, Any]) -> str:
    key = "|".join(str(quote.get(f) or "").strip().lower() for f in ("retailer", "product_name", "condition"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def assign_identity_verdicts(
    task: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    verifier: Verifier | None,
    db_path: Path | None = None,
) -> None:
    """Set ``identity_verdict`` on every usable observation, in place.

    1. Deterministic pre-filter: a used/refurbished offer the task did not ask for
       is ``different`` with no AI call.
    2. A page the user pasted (``url`` source) is the user's own confirmation:
       ``same``.
    3. Offers of a Google Shopping product default to ``same`` (Google's product id
       is the identity); the offer-verifier agent may only downgrade them. Verdicts
       are cached per offer, so unchanged offers cost no model calls. Without a
       verdict (agent off or failing) an offer is ``unverified``.
    """
    wanted = task.get("condition")
    to_verify: list[dict[str, Any]] = []
    for quote in observations:
        if quote.get("error"):
            continue
        condition = quote.get("condition")
        if condition and condition != wanted and wanted not in ("any",):
            quote["identity_verdict"] = DIFFERENT
        elif quote.get("source_kind") == storage.SOURCE_SERPAPI:
            quote["identity_verdict"] = SAME
            to_verify.append(quote)
        else:
            quote["identity_verdict"] = SAME
    if not to_verify or verifier is None:
        return

    hashes = {id(q): _offer_hash(q) for q in to_verify}
    cached = storage.get_offer_verdicts(task["id"], list(set(hashes.values())), db_path=db_path)
    pending: dict[str, dict[str, Any]] = {}
    for quote in to_verify:
        verdict = cached.get(hashes[id(quote)])
        if verdict is not None:
            if verdict[0] != SAME:
                quote["identity_verdict"] = verdict[0]
        else:
            pending.setdefault(hashes[id(quote)], quote)
    if not pending:
        return

    identity = task_identity(task) or {}
    product = {
        "title": identity.get("confirmed_title") or task.get("product_name"),
        "constraints": {k: identity.get(k) for k in ("colour", "capacity", "brand") if identity.get(k)},
        "condition": wanted or "new",
    }
    ids = {f"o{i + 1}": h for i, h in enumerate(pending)}
    offers = [
        {
            "id": oid,
            "merchant": pending[h].get("retailer"),
            "title": pending[h].get("product_name"),
            "condition": pending[h].get("condition") or "new/unknown",
            "price": pending[h].get("price"),
            "currency": pending[h].get("currency"),
        }
        for oid, h in ids.items()
    ]
    result = verifier(product, offers)
    fresh_verdicts = {}
    for oid, h in ids.items():
        verdict, reason = result.value.get(oid, (UNVERIFIED, None))
        if verdict != UNVERIFIED:
            fresh_verdicts[h] = (verdict, reason)
    if fresh_verdicts:
        storage.save_offer_verdicts(task["id"], fresh_verdicts, db_path=db_path)
    for quote in to_verify:
        h = hashes[id(quote)]
        if h in pending:
            verdict = fresh_verdicts.get(h, (UNVERIFIED, None))[0]
            if verdict != SAME:
                quote["identity_verdict"] = verdict


def default_verifier() -> Verifier | None:
    """The offer-verifier agent when enabled and a model is configured."""
    if not config.llm_offer_verify() or not llm.is_configured(offer_verifier.NAME):
        return None
    return lambda product, offers: offer_verifier.run(product, offers)


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


def _fetcher_observer(fetcher: Callable[[str], dict[str, Any]]) -> Observer:
    """Adapt a single-URL fetcher (one quote per source) to the observer contract."""

    def observe(source: dict[str, Any], _task: dict[str, Any]) -> list[dict[str, Any]]:
        quote = fetcher(source["url"])
        # The source row is the authoritative shop label for a single-page source.
        quote["retailer"] = source.get("retailer") or quote.get("retailer")
        quote.setdefault("trusted", True)
        return [quote]

    return observe


def _observe_sources(
    fresh: dict[str, Any],
    sources: list[dict[str, Any]],
    observer: Observer,
    db_path: Path | None,
) -> list[dict[str, Any]]:
    """Observe every enabled source, persisting each quote independently.

    One source failing never stops the others, and never counts as "the deal ended":
    an unreachable shop is not evidence about its price. A product source can return
    one quote per seller; all of them share the check's ``check_run_id``.
    """
    run_id = uuid.uuid4().hex[:12]
    observations: list[dict[str, Any]] = []
    for source in sources:
        started = _now()
        try:
            quotes = list(observer(source, fresh))
            if not quotes:
                raise pricing.ObservationError("The source returned no observation.")
        except pricing.ObservationError as exc:
            quotes = [_failed_quote(source, fresh["product_name"], str(exc), _iso(started))]
        except Exception as exc:  # noqa: BLE001 - one bad source must not sink the task
            logger.exception("Unexpected error observing source %s", source["id"])
            quotes = [_failed_quote(source, fresh["product_name"], f"{type(exc).__name__}: {exc}", _iso(started))]
        for quote in quotes:
            quote.setdefault("fetched_at", _iso(started))
            quote.setdefault("url", source["url"])
            if not quote.get("retailer"):
                quote["retailer"] = source.get("retailer")
            quote["source_id"] = source["id"]
            quote["source_kind"] = source.get("source_kind")
            quote["check_run_id"] = run_id
            observations.append(quote)
        errors = [q.get("error") for q in quotes if q.get("error")]
        storage.record_source_check(source["id"], error=errors[0] if errors else None, db_path=db_path)
    return observations


def _save_observations(task_id: int, observations: list[dict[str, Any]], db_path: Path | None) -> None:
    for quote in observations:
        quote["quote_id"] = storage.save_quote(task_id, quote, db_path=db_path, source_id=quote.get("source_id"))


def _alert_candidates(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Observations allowed to trigger an alert: same item, and trusted when required."""
    trusted_only = config.alert_trusted_only()
    return [
        q for q in observations
        if q.get("identity_verdict") in (SAME, UNVERIFIED)
        and (not trusted_only or q.get("trusted") is not False)
    ]


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
        return (Decimal(pricing.effective_price(task, q)), int(q.get("source_id") or 0))

    return sorted(usable, key=sort_key)[0]


def _reset_blocker(
    task: dict[str, Any], sources: list[dict[str, Any]], observations: list[dict[str, Any]]
) -> str | None:
    """Why the current deal episode must not be reset, or None when it may be."""
    if any(q.get("error") for q in observations):
        return "not_all_sources_comparable"
    relevant = _alert_candidates(observations) + [
        q for q in observations if q.get("identity_verdict") == NEEDS_CONFIRMATION
        and (not config.alert_trusted_only() or q.get("trusted") is not False)
    ]
    if any(q.get("identity_verdict") != SAME for q in relevant):
        return "identity_unconfirmed"
    for source in sources:
        if not any(q.get("source_id") == source["id"] and _is_comparable_quote(task, q) for q in relevant):
            if any(q.get("source_id") == source["id"] and q.get("identity_verdict") == DIFFERENT for q in observations):
                return "identity_mismatch"
            return "not_all_sources_comparable"
    return None


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


_DEFAULT = object()


def check_task(
    task: dict[str, Any],
    *,
    observer: Observer | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    verifier: Verifier | None | object = _DEFAULT,
    send_fn: Callable[[str, dict, dict], dict[str, Any]] = notifier.notify_deal,
    max_retries: int | None = None,
    db_path: Path | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run one check for a task and update persistence.

    Every enabled source is observed and recorded (a Google Shopping product source
    yields one quote per seller); the deal decision is made once per task from the
    best buyable, same-item, trusted observation, so several matching sellers never
    produce several emails.

    ``observer`` defaults to :func:`pricing.default_observer`; ``fetcher`` adapts a
    single-URL fetcher instead. ``verifier`` defaults to the offer-verifier agent
    when enabled; pass ``None`` to skip it.

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

        if observer is None:
            observer = _fetcher_observer(fetcher) if fetcher is not None else pricing.default_observer(db_path)
        observations = _observe_sources(fresh, sources, observer, db_path)

        # Re-read the task after fetching: a threshold/comparison change that
        # committed while the fetches were in flight must govern this decision,
        # never the stale snapshot taken before it.
        fresh = storage.get_task(task_id, db_path=db_path) or fresh

        # Identity gate: second-hand offers the task did not ask for are rejected in
        # code; the offer-verifier agent may veto the rest. Neither a rejected nor
        # an uncertain offer can end a deal episode.
        assign_identity_verdicts(
            fresh,
            observations,
            verifier=default_verifier() if verifier is _DEFAULT else verifier,
            db_path=db_path,
        )
        _save_observations(task_id, observations, db_path)

        best = _best_buyable_quote(fresh, _alert_candidates(observations))
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
                    "trusted": q.get("trusted"),
                }
                for q in observations
            ],
        }

        if best is not None and fresh.get("drop_percent") and not fresh.get("reference_price"):
            # First comparable check of a drop-percent task: remember where it started.
            storage.set_reference_price(task_id, pricing.effective_price(fresh, best), db_path=db_path)
            fresh["reference_price"] = pricing.effective_price(fresh, best)
        reason = deal_reason(fresh, best)
        if reason is not None:
            result["is_deal"] = True
            result["deal_reason"] = reason
            result["chosen_retailer"] = best.get("retailer")
            result["chosen_price"] = best.get("price")
            result["chosen_effective_price"] = pricing.effective_price(fresh, best)
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
                        notification_quote["deal_reason"] = reason
                        notification_quote["reference_price"] = fresh.get("reference_price")
                        try:
                            notification_quote["insight"] = pricing.price_insight(
                                fresh, pricing.effective_price(fresh, best), db_path=db_path)
                        except Exception:  # noqa: BLE001 - context only, never blocks an alert
                            notification_quote["insight"] = None
                        notification = _send_event_notification(
                            active_event, fresh, notification_quote, send_fn, max_retries, db_path=db_path
                        )
                        result["notified"] = notification.get("notified", False)
                        if result["notified"] and fresh.get("drop_percent"):
                            # The next drop is measured from the price just reported.
                            storage.set_reference_price(task_id, pricing.effective_price(fresh, best), db_path=db_path)
                        result["event_id"] = notification.get("event_id")
                        result["notification_error"] = notification.get("error")
                        result["notification_reason"] = notification.get("reason")
                    finally:
                        storage.end_send(task_id, send_owner, db_path=db_path)
        else:
            # End the episode only on positive evidence: every enabled source was
            # observed without error and produced at least one comparable, same-item
            # quote, no alert-relevant quote is uncertain, and none is a deal. A
            # failure, an unknown stock level or an unverified offer is not evidence
            # that the deal ended.
            if active_event is not None:
                reason = _reset_blocker(fresh, sources, observations)
                if reason is None:
                    reset_count = storage.reset_active_events(task_id, db_path=db_path)
                    result["event_reset"] = True
                    result["events_reset"] = reset_count
                else:
                    result["event_reset_skipped"] = True
                    result["event_reset_reason"] = reason

        minutes = int(fresh["frequency_minutes"])
        if any(s.get("source_kind") == storage.SOURCE_SERPAPI for s in sources):
            # Each product check spends a SerpApi call: enforce the quota-protecting floor.
            minutes = max(minutes, config.monitor_min_frequency_minutes())
        next_check = _iso(_now() + timedelta(minutes=minutes))
        storage.update_last_check(task_id, best.get("quote_id") if best else None, db_path=db_path)
        storage.update_next_check(task_id, next_check, db_path=db_path)
        logger.info(
            "Task %s checked %s source(s): deal=%s notified=%s duration=%sms",
            task_id, len(observations), result["is_deal"], result["notified"], duration_ms,
        )
        return result
    finally:
        storage.release_task_lock(task_id, owner, db_path=db_path)


def due_tasks(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Active tasks whose next check time has passed, oldest id first."""
    return storage._fetchall(
        db_path,
        "SELECT * FROM tasks WHERE status = 'active' AND next_check_at <= ? ORDER BY id",
        (_iso(_now()),),
    )


def check_due_tasks(db_path: Path | None = None, *, check: Callable[..., dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Check every due task once and return the results (one-shot, no loop).

    Meant for an OS scheduler (Windows Task Scheduler, cron) so monitoring does not
    depend on a window staying open. Task locks keep it safe alongside a running
    in-app scheduler; one failing task never stops the others.
    """
    run = check or check_task
    results = []
    for task in due_tasks(db_path):
        try:
            results.append(run(task, db_path=db_path))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Due check failed for task %s", task["id"])
            results.append({"task_id": task["id"], "error": f"{type(exc).__name__}: {exc}"})
    return results


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
        return due_tasks(self.db_path)

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
        different observer/sender (e.g. tests); the claim, reservation, stop-flag
        and scheduling machinery above stay shared and unchanged."""
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

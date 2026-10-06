"""Local web interface for the bargain agent.

A small stdlib HTTP server plus the single-page UI in ``static/index.html``. It is a
thin layer over the same modules the CLI uses: the Find pipeline, pricing,
scheduler and storage.

Flow: ``/api/find`` (AI pipeline, no writes) -> ``/api/find/preview`` (all sellers
of the chosen product, no writes) -> ``/api/find/confirm`` (the only call that
creates a task). A product page URL can also be monitored directly through
``/api/tasks/add``.

Every external collaborator lives in :class:`Services`, so the whole server is
tested offline with fakes. Actions that may send a real email (checks, starting
monitoring) require an explicit ``{"confirm": true}`` from the caller.

Security posture (loopback-only does NOT mean unprotected):

- Binds only to 127.0.0.1; JSON bodies capped at 64 KiB.
- CSRF double-submit cookie: ``GET /`` and ``GET /api/csrf`` set ``csrftoken`` and
  every POST must repeat it in ``X-CSRF-Token``.
- POSTs must carry an ``Origin`` of this server and a matching ``Host``.
- Untrusted text is rendered by the UI with ``textContent``; only public http(s)
  URLs are accepted for monitoring.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import config
import config_setup
import matching
import notifier
import pipeline
import pricing
import scheduler
import storage
from agents import task_parser
from sources.base import ProductRef, ProviderError

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
#: How many recent find results are kept for preview/confirmation.
MAX_STORED_RESULTS = 20
MAX_DESCRIPTION_CHARS = 400


class WebError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Services:
    """External collaborators. ``None`` fields are built from configuration."""

    find: Callable[[str], dict[str, Any]] | None = None
    #: Product detail (SerpApi immersive: stores, shipping, store links) for previews.
    product_offers: Callable[[ProductRef], tuple[list, ProductRef]] | None = None
    #: One extra search used when a find has too few same-product candidates.
    extra_search: Callable[[str], list] | None = None
    observer: scheduler.Observer | None = None
    verifier: Any = scheduler._DEFAULT
    send_fn: Callable[[str, dict, dict], dict[str, Any]] | None = None
    parse_task: Callable[[str], dict[str, Any]] | None = None
    provider_status: Callable[[], dict[str, Any]] | None = None


class AppState:
    """Shared server state: one DB path, services, recent find results."""

    def __init__(self, db_path: Path, services: Services | None = None):
        self.db_path = db_path
        self.services = services or Services()
        self.csrf_token = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._finds: dict[str, dict[str, Any]] = {}
        self._finds_order: list[str] = []
        self._jobs: dict[str, dict[str, Any]] = {}
        self._jobs_order: list[str] = []
        self.monitor: dict[str, Any] = {"status": "not_started", "scheduler": None, "thread": None, "started_at": None}

    # -- collaborators ---------------------------------------------------------
    def _providers(self):
        from sources.registry import build_providers

        return build_providers(pricing.quota_guard(self.db_path))

    def run_find(self, description: str, on_stage: Callable[[str], None] | None = None,
                 previous: dict[str, Any] | None = None, followup: str | None = None) -> dict[str, Any]:
        if self.services.find is not None:
            return self.services.find(description)
        providers = self._providers()
        deps = pipeline.FindDeps(
            shopping=providers.shopping, supplement=providers.supplement, limits=pipeline.FindLimits.from_config(),
            on_stage=on_stage or (lambda _stage: None),
        )
        return pipeline.run_find(description, deps=deps, previous=previous, followup=followup)

    # -- background finds ------------------------------------------------------
    def start_find_job(self, description: str, *, previous: dict[str, Any] | None = None,
                       followup: str | None = None, extra: dict[str, Any] | None = None) -> str:
        job_id = secrets.token_urlsafe(12)
        job: dict[str, Any] = {"status": "running", "stages": [], "response": None, "error": None}
        with self._lock:
            self._jobs[job_id] = job
            self._jobs_order.append(job_id)
            while len(self._jobs_order) > MAX_STORED_RESULTS:
                self._jobs.pop(self._jobs_order.pop(0), None)

        def work() -> None:
            try:
                result = self.run_find(description, on_stage=job["stages"].append, previous=previous, followup=followup)
                result.update(extra or {})
                job["response"] = _find_response(self, result)
                job["status"] = "done"
            except config.ConfigError as exc:
                job.update(status="error", error=f"Configuration problem: {exc}")
            except Exception as exc:  # noqa: BLE001
                job.update(status="error", error=f"Find failed: {type(exc).__name__}")

        threading.Thread(target=work, daemon=True, name="bargain-agent-find").start()
        return job_id

    def find_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(job_id)

    def product_detail(self, ref: ProductRef) -> tuple[list, ProductRef] | None:
        """Store list for a product page, or None when no detail source is configured."""
        if self.services.product_offers is not None:
            return self.services.product_offers(ref)
        detail = self._providers().detail
        return detail.offers(ref) if detail is not None else None

    def extra_search(self, query: str) -> list:
        if self.services.extra_search is not None:
            return self.services.extra_search(query)
        shopping = self._providers().shopping
        return shopping.search(query, limit=40) if shopping is not None else []

    def match_verifier(self):
        verifier = self.services.verifier
        return scheduler.default_verifier() if verifier is scheduler._DEFAULT else verifier

    def check_task(self, task: dict[str, Any]) -> dict[str, Any]:
        return scheduler.check_task(
            task,
            db_path=self.db_path,
            observer=self.services.observer,
            verifier=self.services.verifier,
            send_fn=self.services.send_fn or notifier.notify_deal,
        )

    def parse_task(self, instruction: str) -> dict[str, Any]:
        return (self.services.parse_task or task_parser.run)(instruction)

    def provider_status(self) -> dict[str, Any]:
        if self.services.provider_status is not None:
            return self.services.provider_status()
        providers = self._providers()
        return {**providers.status(), "unavailable": providers.unavailable}

    def make_scheduler(self) -> scheduler.TaskScheduler:
        state = self

        class WebTaskScheduler(scheduler.TaskScheduler):
            def _perform_check(self, fresh: dict[str, Any]) -> dict[str, Any]:
                return scheduler.check_task(
                    fresh,
                    db_path=self.db_path,
                    observer=state.services.observer,
                    verifier=state.services.verifier,
                    send_fn=state.services.send_fn or notifier.notify_deal,
                    should_abort=self._stop_event.is_set,
                )

        return WebTaskScheduler(db_path=self.db_path)

    # -- email -------------------------------------------------------------------
    @property
    def notify_email(self) -> str | None:
        email = config._get("DEFAULT_NOTIFY_EMAIL")
        return email if email and "@" in email else None

    def email_warning(self) -> str | None:
        if not self.notify_email:
            return "No notification email is set; add one in Settings before creating a task."
        return f"A real notification email may be sent to {self.notify_email} when a deal is found."

    # -- find store --------------------------------------------------------------
    def store_find(self, result: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._finds[token] = {"result": result, "previews": {}, "matches": {}, "confirmed": {}}
            self._finds_order.append(token)
            while len(self._finds_order) > MAX_STORED_RESULTS:
                self._finds.pop(self._finds_order.pop(0), None)
        return token

    def get_find(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            return self._finds.get(token)

    def confirm_once(self, token: str, rec_id: str, build: Callable[[], int]) -> tuple[str, int]:
        """Create at most one task per (find token, recommendation)."""
        with self._lock:
            entry = self._finds.get(token)
            if entry is None:
                raise WebError(404, "This find result has expired; please run Find again.")
            if rec_id in entry["confirmed"]:
                return "duplicate", entry["confirmed"][rec_id]
            task_id = build()  # nothing is recorded if creation fails
            entry["confirmed"][rec_id] = task_id
            return "created", task_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- helpers


def _money(value: Any, label: str = "target price") -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise WebError(400, f"Invalid {label}: {value!r}") from None
    if not amount.is_finite() or amount <= 0:
        raise WebError(400, f"Invalid {label}: {value!r}")
    return str(amount.quantize(Decimal("0.01")))


def _percent(value: Any) -> str | None:
    """Optional "alert on an X% drop": None/"" means off; otherwise 1–90."""
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise WebError(400, f"Invalid drop percentage: {value!r}") from None
    if not amount.is_finite() or not Decimal(1) <= amount <= Decimal(90):
        raise WebError(400, "drop_percent must be between 1 and 90.")
    return format(amount.normalize(), "f")


def _frequency(value: Any, *, product_source: bool) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise WebError(400, "frequency_minutes must be a whole number of minutes.") from None
    if not 5 <= minutes <= 525_600:
        raise WebError(400, "frequency_minutes must be between 5 and 525600.")
    if product_source:
        minutes = max(minutes, config.monitor_min_frequency_minutes())
    return minutes


def _choice(value: Any, allowed: tuple[str, ...], name: str, default: str | None = None) -> str:
    value = value or default
    if value not in allowed:
        raise WebError(400, f"{name} must be one of {', '.join(allowed)}.")
    return value


def _require_email(state: AppState) -> str:
    email = state.notify_email
    if not email:
        raise WebError(400, "No notification email is configured; set it in Setup first.")
    return email


def _require_confirm(body: dict[str, Any] | None, action: str) -> None:
    """The UI asks first, but the API itself is the boundary: no real email without consent."""
    if not (body or {}).get("confirm"):
        raise WebError(400, f"{action} may send a real notification email — confirm to proceed.")


def _has_product_source(task_id: int, db_path: Path) -> bool:
    return any(s.get("source_kind") == storage.SOURCE_SERPAPI for s in storage.list_task_sources(task_id, db_path=db_path))


def _quote_view(q: dict[str, Any]) -> dict[str, Any]:
    return {
        "fetched_at": q.get("fetched_at"),
        "source_id": q.get("source_id"),
        "retailer": q.get("retailer"),
        "price": q.get("price"),
        "currency": q.get("currency"),
        "in_stock": q.get("in_stock"),
        "condition": q.get("condition"),
        "trusted": None if q.get("trusted") is None else bool(q.get("trusted")),
        "identity_verdict": q.get("identity_verdict"),
        "shipping": q.get("shipping"),
        "url": q.get("url"),
        "error": q.get("error"),
    }


def _best_and_insight(task: dict[str, Any], sources: list[dict[str, Any]], db_path: Path) -> dict[str, Any]:
    """The cheapest usable latest quote and how it compares with the last 30 days."""
    best = None
    for source in sources:
        if not source["enabled"]:
            continue
        for q in source["latest"]:
            if not q.get("price") or q.get("error") or q.get("in_stock") is False or q.get("trusted") is False:
                continue
            if q.get("identity_verdict") in ("different", "needs_confirmation") or q.get("currency") != task["currency"]:
                continue
            if best is None or Decimal(pricing.effective_price(task, q)) < Decimal(pricing.effective_price(task, best)):
                best = q
    current = pricing.effective_price(task, best) if best else None
    return {"best": best, "best_effective_price": current,
            "insight": pricing.price_insight(task, current, db_path=db_path)}


def _task_summary(task: dict[str, Any], db_path: Path) -> dict[str, Any]:
    sources = storage.list_task_sources(task["id"], db_path=db_path)
    out_sources = []
    for s in sources:
        rows = storage.list_quotes(source_id=s["id"], limit=1, db_path=db_path)
        latest_run = rows[0].get("check_run_id") if rows else None
        latest = [
            _quote_view(q)
            for q in storage.list_quotes(source_id=s["id"], limit=config.monitor_max_offers(), db_path=db_path)
            if latest_run and q.get("check_run_id") == latest_run
        ] if latest_run else [_quote_view(r) for r in rows]
        out_sources.append(
            {
                "id": s["id"],
                "retailer": s["retailer"],
                "url": s["url"],
                "enabled": bool(s["enabled"]),
                "source_kind": s.get("source_kind") or storage.SOURCE_URL,
                "last_checked_at": s["last_checked_at"],
                "last_error": s["last_error"],
                "consecutive_empty": s.get("consecutive_empty") or 0,
                "shipping_cost": s.get("shipping_cost"),
                "latest": latest,
            }
        )
    return {
        "id": task["id"],
        "product_name": task["product_name"],
        "target_price": task["target_price"],
        "currency": task["currency"],
        "comparison": task["comparison"],
        "frequency_minutes": task["frequency_minutes"],
        "drop_percent": task.get("drop_percent"),
        "reference_price": task.get("reference_price"),
        "include_shipping": bool(task.get("include_shipping")),
        "status": task["status"],
        "next_check_at": task["next_check_at"],
        "last_check_at": task["last_check_at"],
        "notify_email": task["notify_email"],
        "sources": out_sources,
        **_best_and_insight(task, out_sources, db_path),
    }


# --------------------------------------------------------------------------- read API


def monitor_status(state: AppState) -> dict[str, Any]:
    mon = state.monitor
    thread = mon.get("thread")
    if mon["status"] == "running" and thread is not None and not thread.is_alive():
        mon["status"] = "stopped"
    return {"status": mon["status"], "started_at": mon.get("started_at"), "restartable": False}


def api_tasks(state: AppState) -> dict[str, Any]:
    return {
        "tasks": [_task_summary(t, state.db_path) for t in storage.list_tasks(db_path=state.db_path)],
        "monitor": monitor_status(state),
        "email_warning": state.email_warning(),
        "api_usage": storage.api_usage_today(db_path=state.db_path),
        "serpapi_daily_limit": config.serpapi_daily_max_calls(),
        "daily_limits": {"serper": config.serper_daily_max_calls(), "serpapi": config.serpapi_daily_max_calls()},
    }


def api_task_detail(state: AppState, task_id: int) -> dict[str, Any]:
    task = storage.get_task(task_id, db_path=state.db_path)
    if task is None:
        raise WebError(404, f"Task {task_id} not found.")
    summary = _task_summary(task, state.db_path)
    summary["history"] = [_quote_view(q) for q in storage.list_quotes(task_id=task_id, limit=50, db_path=state.db_path)]
    summary["events"] = [
        {"id": e["id"], "status": e["status"], "created_at": e["created_at"], "attempts": e["attempts"], "last_error": e["last_error"]}
        for e in storage.list_events(task_id=task_id, db_path=state.db_path)
    ]
    summary["email_warning"] = state.email_warning()
    return summary


def api_history(state: AppState, task_id: int) -> dict[str, Any]:
    if storage.get_task(task_id, db_path=state.db_path) is None:
        raise WebError(404, f"Task {task_id} not found.")
    return {"task_id": task_id, "history": [_quote_view(q) for q in storage.list_quotes(task_id=task_id, limit=100, db_path=state.db_path)]}


def api_setup_status(state: AppState) -> dict[str, Any]:
    """Which keys are MISSING — never their values."""
    status = config_setup.status()
    try:
        providers = state.provider_status()
    except Exception:  # noqa: BLE001 - status must never fail the page
        providers = {}
    return {
        **status,
        "providers": providers,
        "note": "Model and provider keys are loaded from the app's .env; the recipient and the sending mailbox can be set here.",
    }


# --------------------------------------------------------------------------- find flow


def api_find(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """Run the multi-agent Find pipeline. Nothing is written to the database."""
    description = " ".join(str(body.get("description") or "").split())
    if not description:
        raise WebError(400, "Please describe what you are looking for.")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise WebError(400, f"That description is too long; keep it under {MAX_DESCRIPTION_CHARS} characters.")
    if body.get("async"):
        return {"job": state.start_find_job(description)}
    try:
        result = state.run_find(description)
    except config.ConfigError as exc:
        raise WebError(400, f"Configuration problem: {exc}") from None
    return _find_response(state, result)


def api_find_refine(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """Refine an earlier find with a follow-up ("only black", "cheaper"). No writes."""
    token = str(body.get("token") or "")
    message = " ".join(str(body.get("message") or "").split())
    if not message:
        raise WebError(400, "Please type how to refine the results.")
    if len(message) > 200:
        raise WebError(400, "Keep the refinement under 200 characters.")
    entry = state.get_find(token)
    if entry is None:
        raise WebError(404, "This find result has expired; please run Find again.")
    previous = entry["result"]
    original = previous.get("original_description") or previous.get("description") or ""
    followups = list(previous.get("followups") or []) + [message]
    if len(followups) > 5:
        raise WebError(400, "That is a lot of refinements; start a new Find instead.")
    description = pipeline.refine_description(original, followups)
    extra = {"original_description": original, "followups": followups}
    if body.get("async"):
        return {"job": state.start_find_job(description, previous=previous, followup=message, extra=extra)}
    try:
        result = state.run_find(description, previous=previous, followup=message)
    except config.ConfigError as exc:
        raise WebError(400, f"Configuration problem: {exc}") from None
    result.update(extra)
    return _find_response(state, result)


def _find_response(state: AppState, result: dict[str, Any]) -> dict[str, Any]:
    token = state.store_find(result)
    keys = ("status", "description", "need", "queries", "recommendations", "stages", "notes", "usage",
            "original_description", "followups")
    return {"token": token, **{k: result.get(k) for k in keys}}


def api_find_status(state: AppState, job_id: str) -> dict[str, Any]:
    job = state.find_job(job_id)
    if job is None:
        raise WebError(404, "This find has expired; please run Find again.")
    return {"status": job["status"], "stages": list(job["stages"]), "response": job["response"], "error": job["error"]}


def _recommendation(state: AppState, body: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    token, rec_id = str(body.get("token") or ""), str(body.get("rec_id") or "")
    if not token or not rec_id:
        raise WebError(400, "A find token and a chosen recommendation are required.")
    entry = state.get_find(token)
    if entry is None:
        raise WebError(404, "This find result has expired; please run Find again.")
    rec = next((r for r in entry["result"].get("recommendations") or [] if r.get("id") == rec_id), None)
    if rec is None:
        raise WebError(400, "That product was not among the recommendations.")
    return token, entry, rec


def _listing_offer(rec: dict[str, Any]) -> dict[str, Any]:
    return {"merchant": rec.get("merchant"), "price": rec.get("price"), "currency": rec.get("currency"),
            "link": rec.get("link"), "provider": rec.get("provider"), "title": rec.get("title"),
            "condition": rec.get("condition"), "in_stock": True, "shipping": None,
            "trusted": bool(rec.get("trusted"))}


def _matches_for(state: AppState, entry: dict[str, Any], rec: dict[str, Any], need: dict[str, Any]) -> list[dict[str, Any]]:
    """Same product at other merchants (cached per find result and recommendation)."""
    if rec["id"] in entry["matches"]:
        return entry["matches"][rec["id"]]
    pool = [dict(e) for e in entry["result"].get("evidence") or []]
    if len(matching.rule_candidates(rec, pool, need)) < 2:
        query = matching.monitor_query(rec.get("title") or "")
        try:
            extra = state.extra_search(query) if query else []
        except ProviderError:
            extra = []
        known = {e.get("product_id") for e in pool}
        for i, result in enumerate(extra, start=1):
            if result.product_id and result.product_id not in known:
                pool.append(pipeline.evidence_from(result, eid=f"x{i}", query=query))
    found = matching.find_same_product(rec, pool, need, verifier=state.match_verifier(),
                                       max_sources=config.monitor_max_sources())
    entry["matches"][rec["id"]] = found
    return found


def api_find_preview(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """The chosen product's sellers and the same product at other merchants. Nothing is written."""
    _token, entry, rec = _recommendation(state, body)
    need = entry["result"].get("need") or {}
    matches: list[dict[str, Any]] = []
    if rec.get("monitorable"):
        offers = entry["previews"].get(rec["id"])
        if offers is None:
            detail = None
            if rec.get("page_token"):
                try:
                    detail = state.product_detail(ProductRef(rec.get("product_id"), rec.get("page_token"), None, rec.get("title")))
                except ProviderError:
                    detail = None
            if detail and detail[0]:
                rec["page_token"] = detail[1].page_token or rec.get("page_token")
                offers = [{**o.to_dict(), "trusted": pricing.merchant_trust(o.merchant, o.link)}
                          for o in detail[0][: config.monitor_max_offers()]]
            else:
                offers = [_listing_offer(rec)]
            entry["previews"][rec["id"]] = offers
        matches = _matches_for(state, entry, rec, need)
        kind = storage.SOURCE_SERPAPI
    else:
        offers = [{**_listing_offer(rec), "in_stock": None, "trusted": True}]
        kind = storage.SOURCE_URL

    wanted = need.get("condition")
    candidates = offers + [m for m in matches if m.get("preselect")]
    buyable = [
        o for o in candidates
        if o.get("price") and o.get("in_stock", True) is not False and o.get("trusted")
        and (not o.get("condition") or o.get("condition") == wanted)
    ]
    best = min(buyable, key=lambda o: Decimal(o["price"])) if buyable else None
    currency = need.get("currency") or (best or {}).get("currency") or rec.get("currency") or "NZD"
    return {
        "preview": {
            "rec_id": rec["id"],
            "product_name": rec.get("title"),
            "source_kind": kind,
            "offers": offers,
            "matches": matches,
            "max_sources": config.monitor_max_sources(),
            "best": best,
            "suggested_target": need.get("budget") or (best or {}).get("price"),
            "currency": currency,
            "frequency_minutes": max(1440, config.monitor_min_frequency_minutes()) if kind == storage.SOURCE_SERPAPI else 1440,
            "min_frequency_minutes": config.monitor_min_frequency_minutes() if kind == storage.SOURCE_SERPAPI else 5,
            "notify_email": state.notify_email,
        },
        "email_warning": state.email_warning(),
    }


def api_find_confirm(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """Create the monitoring task for a chosen recommendation (the only write)."""
    token, entry, rec = _recommendation(state, body)
    need = entry["result"].get("need") or {}
    product_source = bool(rec.get("monitorable"))
    target = _money(body.get("target_price"))
    comparison = _choice(body.get("comparison"), ("lt", "lte"), "comparison", "lte")
    currency = _choice(str(body.get("currency") or need.get("currency") or rec.get("currency") or "").upper(),
                       ("NZD", "AUD", "USD"), "currency")
    frequency = _frequency(body.get("frequency_minutes") or 1440, product_source=product_source)
    drop_percent = _percent(body.get("drop_percent"))
    include_shipping = bool(body.get("include_shipping"))
    email = _require_email(state)
    preview_shipping = {
        pricing.normalise_merchant(o.get("merchant") or ""): o.get("shipping")
        for o in entry["previews"].get(rec["id"]) or [] if o.get("shipping") is not None
    }
    extra_ids = body.get("extra_ids") or []
    if not isinstance(extra_ids, list) or not all(isinstance(i, str) for i in extra_ids):
        raise WebError(400, "extra_ids must be a list of match ids.")
    available = {m["id"]: m for m in entry["matches"].get(rec["id"], [])}
    if any(i not in available for i in extra_ids) or (extra_ids and not product_source):
        raise WebError(400, "Only same-product matches shown in the preview can be added.")
    extras = [available[i] for i in dict.fromkeys(extra_ids)][: config.monitor_max_sources() - 1]

    def build() -> int:
        if product_source:
            query = matching.monitor_query(rec.get("title") or "") or rec.get("search_query")
            sources = [
                {"url": item["link"], "retailer": item.get("merchant") or "Google Shopping",
                 "source_kind": storage.SOURCE_SERPAPI, "product_id": item.get("product_id"),
                 "page_token": item.get("page_token"), "search_query": query,
                 "shipping_cost": preview_shipping.get(pricing.normalise_merchant(item.get("merchant") or ""))}
                for item in [rec] + extras
            ]
        else:
            if not pricing.is_safe_public_url(rec.get("link")):
                raise WebError(400, "This listing's link cannot be monitored.")
            sources = [{"url": rec["link"], "retailer": rec.get("merchant"), "source_kind": storage.SOURCE_URL}]
        task = {
            "product_name": rec.get("title") or entry["result"].get("description"),
            "url": rec["link"],
            "target_price": target,
            "currency": currency,
            "comparison": comparison,
            "frequency_minutes": frequency,
            "drop_percent": drop_percent,
            "include_shipping": include_shipping,
            "condition": need.get("condition"),
            "notify_email": email,
            "status": "active",
            "identity": {
                "confirmed_title": rec.get("title"),
                **{k: need.get(k) for k in ("brand", "colour", "model") if need.get(k)},
            },
        }
        return storage.create_task_with_sources(task, sources, db_path=state.db_path)

    status, task_id = state.confirm_once(token, rec["id"], build)
    return {"created": task_id, "duplicate": status == "duplicate", "task": api_task_detail(state, task_id),
            "email_warning": state.email_warning()}


# --------------------------------------------------------------------------- direct URL


def api_add_task(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """Monitor one product page. Either ``instruction`` (parsed by the task-parser
    agent) or explicit fields. No network call happens until the first check."""
    instruction = str(body.get("instruction") or "").strip()
    if instruction:
        try:
            parsed = state.parse_task(instruction)
        except task_parser.TaskParseError as exc:
            raise WebError(400, str(exc)) from None
        if parsed["status"] != "ready":
            return {"created": None, "status": parsed["status"], "missing_fields": parsed.get("missing_fields"),
                    "message": parsed.get("clarification_question")}
        fields = parsed
    else:
        fields = body
    url = str(fields.get("url") or "").strip()
    if not pricing.is_safe_public_url(url):
        raise WebError(400, "Only public http(s) product URLs are accepted.")
    product_name = str(fields.get("product_name") or "").strip()
    if not product_name:
        raise WebError(400, "A product name is required.")
    task = {
        "product_name": product_name,
        "url": url,
        "target_price": _money(fields.get("target_price")),
        "currency": _choice(str(fields.get("currency") or "").upper(), ("NZD", "AUD", "USD"), "currency"),
        "comparison": _choice(fields.get("comparison"), ("lt", "lte"), "comparison", "lte"),
        "frequency_minutes": _frequency(fields.get("frequency_minutes") or 1440, product_source=False),
        "drop_percent": _percent(fields.get("drop_percent")),
        "condition": fields.get("condition") if fields.get("condition") in ("new", "used", "refurbished", "any") else None,
        "notify_email": _require_email(state),
        "status": "active",
    }
    task_id = storage.create_task_with_sources(task, None, db_path=state.db_path)
    return {"created": task_id, "task": api_task_detail(state, task_id), "email_warning": state.email_warning()}


# --------------------------------------------------------------------------- task actions


def api_check(state: AppState, task_id: int, body: dict[str, Any] | None = None) -> dict[str, Any]:
    task = storage.get_task(task_id, db_path=state.db_path)
    if task is None:
        raise WebError(404, f"Task {task_id} not found.")
    _require_confirm(body, "Checking a task")
    result = state.check_task(task)
    if result.get("skipped"):
        return {"skipped": True, "reason": result.get("reason"), "email_warning": state.email_warning()}
    keys = ("is_deal", "notified", "chosen_retailer", "chosen_price", "sources_checked", "sources_failed",
            "notification_cancelled", "notification_cancelled_reason", "event_reset", "event_reset_reason", "observations")
    return {"skipped": False, **{k: result.get(k) for k in keys}, "email_warning": state.email_warning()}


def api_check_due(state: AppState, body: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_confirm(body, "Running due checks")
    due = scheduler.due_tasks(state.db_path)
    results = [{"task_id": t["id"], **api_check(state, t["id"], {"confirm": True})} for t in due]
    return {"checked": len(results), "results": results, "email_warning": state.email_warning()}


def api_delete_task(state: AppState, task_id: int) -> dict[str, Any]:
    """Delete a stopped monitor and its history (stop it first)."""
    outcome = storage.delete_stopped_task(task_id, db_path=state.db_path)
    if outcome == "missing":
        raise WebError(404, f"Task {task_id} not found.")
    if outcome == "not_stopped":
        raise WebError(400, "Stop the monitor before deleting it.")
    return {"deleted": task_id}


def api_status_change(state: AppState, task_id: int, status: str) -> dict[str, Any]:
    if storage.get_task(task_id, db_path=state.db_path) is None:
        raise WebError(404, f"Task {task_id} not found.")
    outcome = storage.set_task_status_atomic(task_id, status, db_path=state.db_path, block_on_send=True)
    return {"outcome": outcome, "task": _task_summary(storage.get_task(task_id, db_path=state.db_path), state.db_path),
            "email_warning": state.email_warning()}


def api_modify(state: AppState, task_id: int, body: dict[str, Any]) -> dict[str, Any]:
    if storage.get_task(task_id, db_path=state.db_path) is None:
        raise WebError(404, f"Task {task_id} not found.")
    target, comparison, frequency = body.get("target_price"), body.get("comparison"), body.get("frequency_minutes")
    drop = body.get("drop_percent")
    include_shipping = body.get("include_shipping")
    if include_shipping is not None:
        include_shipping = bool(include_shipping)
    if target is None and comparison is None and frequency is None and drop is None and include_shipping is None:
        raise WebError(400, "Nothing to change.")
    if drop is not None:
        drop = _percent(drop) or ""
    if target is not None:
        target = _money(target)
    if comparison is not None:
        comparison = _choice(comparison, ("lt", "lte"), "comparison")
    if frequency is not None:
        frequency = _frequency(frequency, product_source=_has_product_source(task_id, state.db_path))
    current = storage.get_task(task_id, db_path=state.db_path)
    if include_shipping is not None and include_shipping == bool(current.get("include_shipping")):
        include_shipping = None  # unchanged: do not restart the episode
    threshold_changed = target is not None or comparison is not None or drop is not None or include_shipping is not None
    updated = storage.update_task_conditions(
        task_id, target_price=target, comparison=comparison, frequency_minutes=frequency,
        db_path=state.db_path, end_episode=threshold_changed, drop_percent=drop, include_shipping=include_shipping,
    )
    return {"updated": True, "episode_ended": threshold_changed, "task": _task_summary(updated, state.db_path),
            "email_warning": state.email_warning()}


def api_source_shipping(state: AppState, task_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Set (or clear with "") the shipping cost used for one merchant."""
    if storage.get_task(task_id, db_path=state.db_path) is None:
        raise WebError(404, f"Task {task_id} not found.")
    raw = body.get("shipping")
    if raw in (None, ""):
        value = None
    else:
        try:
            amount = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            raise WebError(400, f"Invalid shipping cost: {raw!r}") from None
        if not amount.is_finite() or amount < 0 or amount > 10000:
            raise WebError(400, "Shipping must be between 0 and 10000.")
        value = str(amount.quantize(Decimal("0.01")))
    outcome = storage.set_source_shipping(task_id, int(body.get("source_id")), value, db_path=state.db_path)
    if outcome == "missing":
        raise WebError(404, "Source not found.")
    if outcome == "foreign":
        raise WebError(400, "That source belongs to a different task.")
    return {"updated": True, "task": _task_summary(storage.get_task(task_id, db_path=state.db_path), state.db_path)}


def api_source_toggle(state: AppState, task_id: int, source_id: int, enabled: bool) -> dict[str, Any]:
    if storage.get_task(task_id, db_path=state.db_path) is None:
        raise WebError(404, f"Task {task_id} not found.")
    outcome, _row = storage.set_task_source_enabled_for_task(task_id, source_id, enabled, db_path=state.db_path)
    if outcome == "missing":
        raise WebError(404, f"Source {source_id} not found.")
    if outcome == "foreign":
        raise WebError(400, f"Source {source_id} belongs to a different task.")
    remaining = storage.list_task_sources(task_id, enabled_only=True, db_path=state.db_path)
    return {"updated": True, "enabled_sources": len(remaining),
            "warning": None if remaining else "This task now has no enabled sources.",
            "task": _task_summary(storage.get_task(task_id, db_path=state.db_path), state.db_path)}


SETUP_PAGE_KEYS = ("DEFAULT_NOTIFY_EMAIL", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM")


def _looks_like_email(value: str) -> bool:
    name, at, domain = value.partition("@")
    return bool(at and name and "." in domain and " " not in value)


def api_setup_save(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """The recipient and the sending mailbox (SMTP) can be set from the page.

    Model and provider keys stay in .env. Empty fields are left unchanged, and values
    are never echoed back.
    """
    foreign = sorted(k for k in body if k not in SETUP_PAGE_KEYS)
    if foreign:
        raise WebError(400, f"{', '.join(foreign)} must be configured in .env.")
    updates: dict[str, str] = {}
    for key in SETUP_PAGE_KEYS:
        value = str(body.get(key) or "").strip()
        if not value:
            continue
        if len(value) > 254 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise WebError(400, f"{key} contains characters that are not allowed.")
        if key in ("DEFAULT_NOTIFY_EMAIL", "SMTP_FROM") and not _looks_like_email(value):
            raise WebError(400, "That does not look like an email address.")
        if key == "SMTP_PORT" and not (value.isdigit() and 0 < int(value) < 65536):
            raise WebError(400, "SMTP_PORT must be a number such as 465 or 587.")
        if key == "SMTP_HOST" and (" " in value or "/" in value):
            raise WebError(400, "SMTP_HOST should be a server name such as smtp.gmail.com.")
        if key == "SMTP_PASS" and "gmail" in (body.get("SMTP_HOST") or config_setup_value("SMTP_HOST")).lower():
            value = value.replace(" ", "")  # Gmail shows app passwords in groups of four
        updates[key] = value
    if not updates:
        raise WebError(400, "Nothing to save.")
    config_setup.write_updates(updates)
    return {"saved": sorted(updates), **config_setup.status()}


def config_setup_value(key: str) -> str:
    return os.getenv(key, "") or ""


def api_monitor_start(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    status = monitor_status(state)["status"]
    if status != "not_started":
        raise WebError(409, f"Continuous monitoring is {status} and cannot be started again in this process.")
    _require_confirm(body, "Starting continuous monitoring")
    sched = state.make_scheduler()
    thread = threading.Thread(target=sched.run_loop, daemon=True, name="bargain-agent-monitor-loop")
    thread.start()
    state.monitor.update({"status": "running", "scheduler": sched, "thread": thread, "started_at": _now_iso()})
    return {"status": "running", "email_warning": state.email_warning()}


def api_monitor_stop(state: AppState) -> dict[str, Any]:
    status = monitor_status(state)["status"]
    if status != "running":
        raise WebError(409, f"Continuous monitoring is {status}; nothing to stop.")
    state.monitor["scheduler"].stop()
    state.monitor["status"] = "stopped"
    return {"status": "stopped", "restartable": False, "email_warning": state.email_warning()}


# --------------------------------------------------------------------------- server

_POST_ROUTES: dict[str, Callable[[AppState, dict[str, Any]], dict[str, Any]]] = {
    "/api/find": api_find,
    "/api/find/preview": api_find_preview,
    "/api/find/refine": api_find_refine,
    "/api/find/confirm": api_find_confirm,
    "/api/tasks/add": api_add_task,
    "/api/setup": api_setup_save,
    "/api/check/due": api_check_due,
    "/api/monitor/start": api_monitor_start,
    "/api/monitor/stop": lambda state, _body: api_monitor_stop(state),
}

_TASK_ACTIONS: dict[str, Callable[[AppState, int, dict[str, Any]], dict[str, Any]]] = {
    "check": api_check,
    "pause": lambda s, tid, _b: api_status_change(s, tid, "paused"),
    "delete": lambda s, tid, _b: api_delete_task(s, tid),
    "resume": lambda s, tid, _b: api_status_change(s, tid, "active"),
    "stop": lambda s, tid, _b: api_status_change(s, tid, "stopped"),
    "modify": api_modify,
}


def make_handler(state: AppState, port: int):
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "BargainAgentWeb/2.0"

        def log_message(self, fmt, *args):  # quiet by default
            pass

        def _csrf_cookie(self) -> str | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                key, _, value = part.partition("=")
                if key.strip() == "csrftoken":
                    return value.strip()
            return None

        def _send(self, status: int, body: bytes, content_type: str, set_csrf: bool) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if set_csrf:
                self.send_header("Set-Cookie", f"csrftoken={state.csrf_token}; Path=/; SameSite=Strict")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict[str, Any], set_csrf: bool = False) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", set_csrf)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 64 * 1024:
                if length <= 1024 * 1024:
                    self.rfile.read(length)  # drain so the 400 is delivered, not a reset
                raise WebError(400, "Request body too large.")
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise WebError(400, "Request body must be valid JSON.") from None
            if not isinstance(data, dict):
                raise WebError(400, "Request body must be a JSON object.")
            return data

        def _check_write_guards(self) -> None:
            if (self.headers.get("Host") or "").lower() not in allowed_hosts:
                raise WebError(403, "Host header does not match this server.")
            if self.headers.get("Origin") not in allowed_origins:
                raise WebError(403, "Missing or unrecognised Origin header.")
            token = self.headers.get("X-CSRF-Token")
            if not token or token != state.csrf_token:
                raise WebError(403, "Missing or invalid CSRF token.")
            if self._csrf_cookie() != state.csrf_token:
                raise WebError(403, "CSRF cookie mismatch.")

        def _dispatch(self, handler: Callable[[], None]) -> None:
            try:
                handler()
            except WebError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except (ValueError, KeyError, TypeError):
                self._send_json(400, {"error": "Malformed request."})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": f"Internal error: {type(exc).__name__}"})

        def do_GET(self):  # noqa: N802
            def handle() -> None:
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8", True)
                elif path == "/api/csrf":
                    self._send_json(200, {"csrf_token": state.csrf_token}, set_csrf=True)
                elif path == "/api/tasks":
                    self._send_json(200, api_tasks(state))
                elif path == "/api/find/status":
                    self._send_json(200, api_find_status(state, (parse_qs(urlparse(self.path).query).get("job") or [""])[0]))
                elif path == "/api/monitor/status":
                    self._send_json(200, monitor_status(state))
                elif path == "/api/setup/status":
                    self._send_json(200, api_setup_status(state))
                elif path.startswith("/api/tasks/"):
                    parts = path.strip("/").split("/")
                    if len(parts) == 4 and parts[3] == "history":
                        self._send_json(200, api_history(state, int(parts[2])))
                    elif len(parts) == 3:
                        self._send_json(200, api_task_detail(state, int(parts[2])))
                    else:
                        raise WebError(404, "Not found.")
                else:
                    raise WebError(404, "Not found.")

            self._dispatch(handle)

        def _discard_body(self) -> None:
            # Drain a small unread body so a rejection does not reset the connection.
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= 64 * 1024:
                self.rfile.read(length)

        def do_POST(self):  # noqa: N802
            def handle() -> None:
                try:
                    self._check_write_guards()  # before the body is parsed
                except WebError:
                    self._discard_body()
                    raise
                path = urlparse(self.path).path
                parts = path.strip("/").split("/")
                body = self._read_json()
                if path in _POST_ROUTES:
                    self._send_json(200, _POST_ROUTES[path](state, body))
                elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] in _TASK_ACTIONS:
                    self._send_json(200, _TASK_ACTIONS[parts[3]](state, int(parts[2]), body))
                elif len(parts) == 5 and parts[:2] == ["api", "tasks"] and parts[3] == "sources" and parts[4] == "shipping":
                    self._send_json(200, api_source_shipping(state, int(parts[2]), body))
                elif len(parts) == 5 and parts[:2] == ["api", "tasks"] and parts[3] == "sources" and parts[4] in ("enable", "disable"):
                    self._send_json(200, api_source_toggle(state, int(parts[2]), int(body.get("source_id")), parts[4] == "enable"))
                else:
                    raise WebError(404, "Not found.")

            self._dispatch(handle)

    return Handler


INDEX_HTML = ""


def serve(db_path: Path | None = None, port: int = DEFAULT_PORT, services: Services | None = None) -> ThreadingHTTPServer:
    global INDEX_HTML
    INDEX_HTML = Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")
    db_path = db_path or config.database_path()
    storage.init_db(db_path)
    state = AppState(db_path=db_path, services=services)
    httpd = ThreadingHTTPServer((HOST, port), make_handler(state, port))
    httpd.app_state = state  # type: ignore[attr-defined]
    httpd.db_path = db_path  # type: ignore[attr-defined]
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description="Local web interface for the bargain agent.")
    parser.add_argument("--db-path", default=None, help="Database file (default: DATABASE_URL).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    import socket

    with socket.socket() as probe:
        probe.settimeout(1.0)
        if probe.connect_ex((HOST, args.port)) == 0:
            print(f"Port {args.port} is already in use — another Bargain Agent window is probably running.")
            return 1

    httpd = serve(Path(args.db_path) if args.db_path else None, port=args.port)
    print(f"Bargain Agent web UI: http://{HOST}:{args.port}/")
    print(f"Database: {httpd.db_path}")  # type: ignore[attr-defined]
    print("Notifications are real emails. Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

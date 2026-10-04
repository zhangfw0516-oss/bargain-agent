"""Stage D: local web interface for the bargain agent.

A deliberately small, dependency-free HTTP server (stdlib `http.server`) plus a
single-page English UI in `static/index.html`. It is a thin layer over the exact
same business modules the CLI uses — discovery, scheduler, storage — so every
Stage B/C guarantee (identity gate, one-notification-per-episode, source
ownership checks, race guards) applies unchanged.

Operating modes:

- **Demo mode (default, and the mode used for acceptance)**: search requests are
  parsed by the offline bilingual demo parser (`demo_data.parse_demo_request`),
  shops are the two demo retailers, checks use `demo_data.demo_check_fetcher`,
  and every notification goes through `demo_data.fake_notify_deal` — explicitly
  passed as `send_fn=` (check_task binds its keyword default at import time).
  The demo mode always runs on its own per-run temporary database: it can never
  read or write the user's real task database. No network beyond 127.0.0.1, no
  LLM, no SMTP.
- **Live mode (`--live`)**: real parser, real retailers, real fetcher, real
  notifier. Every state-changing response carries an explicit email warning.
  Live checks (single or due-batch) and starting continuous monitoring require
  an explicit ``{"confirm": true}`` from the caller — the UI asks the user and
  passes it, and a direct API call without it is refused, so nothing can trigger
  a real email unconfirmed. This mode is for actual use; the automated
  acceptance substitutes every external collaborator (fake DeepSeek, fake shops,
  fake web search, fake fetch, fake sender) and runs fully offline.

Security posture (loopback-only does NOT mean unprotected):

- Binds only to 127.0.0.1; JSON body validation on every POST.
- CSRF: double-submit cookie — `GET /` and `GET /api/csrf` set a random
  `csrftoken` cookie, and every POST must repeat it in the `X-CSRF-Token` header.
- Origin/Host checks: POST requests must carry an `Origin` of
  `http://127.0.0.1:<port>` or `http://localhost:<port>` and a `Host` header
  matching the server; anything else is refused with 403.
- Untrusted text (product names, errors, notes) is rendered by the UI with safe
  DOM APIs (`textContent`), never interpolated into HTML; URLs are shown as text
  and only http/https protocols are accepted for task creation.
"""

from __future__ import annotations

import argparse
import json
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

# Distributable package: vendored pure-Python runtime deps (schedule, dotenv)
# live in ./_vendor next to this file. Kept first so the app runs on a plain
# system Python without any installation step.
_VENDOR_DIR = Path(__file__).with_name("_vendor")
if _VENDOR_DIR.is_dir():
    import sys

    if str(_VENDOR_DIR) not in sys.path:
        sys.path.insert(0, str(_VENDOR_DIR))

import discovery
import iterative_discovery
import find_pipeline
import scheduler
import schemas
import storage

import config
import config_setup
import demo_data

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
#: How many recent search results are kept for the selection step.
MAX_STORED_RESULTS = 20
DEMO_NOTIFY_EMAIL = "demo-user@localhost"


class WebError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class DemoTaskScheduler(scheduler.TaskScheduler):
    """The real scheduling machinery with demo collaborators injected.

    Only `_perform_check` is overridden (fetcher + fake sender); the claim,
    reservation, stop-flag and non-restartable process-slot rules are inherited
    unchanged from TaskScheduler.
    """

    def _perform_check(self, fresh: dict[str, Any]) -> dict[str, Any]:
        return scheduler.check_task(
            fresh, db_path=self.db_path,
            fetcher=demo_data.demo_check_fetcher,
            send_fn=demo_data.fake_notify_deal,
            should_abort=self._stop_event.is_set,
        )


class LiveTaskScheduler(scheduler.TaskScheduler):
    """Live monitoring loop with collaborators resolved when a check runs.

    Identical machinery to TaskScheduler — only `_perform_check` is overridden,
    and it resolves `scraper.fetch_quote` / `notifier.notify_deal` at call time
    rather than binding them at import. In normal live use these are the same
    functions anyway; resolving them late is what lets the offline live-path
    tests substitute every external collaborator for the whole process, so the
    test suite can prove no real fetch or SMTP call happens.
    """

    def _perform_check(self, fresh: dict[str, Any]) -> dict[str, Any]:
        import notifier
        import scraper

        return scheduler.check_task(
            fresh, db_path=self.db_path,
            fetcher=scraper.fetch_quote,
            send_fn=notifier.notify_deal,
            should_abort=self._stop_event.is_set,
        )


class AppState:
    """Shared server state: one DB path, one mode, recent search results."""

    def __init__(self, db_path: Path, demo: bool):
        self.db_path = db_path
        self.demo = demo
        self.csrf_token = secrets.token_urlsafe(24)
        self._results: dict[str, dict[str, Any]] = {}
        self._results_order: list[str] = []
        self._consumed: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        #: Results of the descriptive "intelligent find" discovery step, keyed by
        #: token. Each entry is ``{"result", "description", "request", "consumed"}``.
        self._finds: dict[str, dict[str, Any]] = {}
        self._finds_order: list[str] = []
        #: Continuous monitoring loop (reuses the real TaskScheduler machinery).
        self.monitor: dict[str, Any] = {
            "status": "not_started",   # not_started | running | stopped
            "scheduler": None,
            "thread": None,
            "started_at": None,
        }

    # -- notify email ---------------------------------------------------------
    @property
    def notify_email(self) -> str | None:
        if self.demo:
            return DEMO_NOTIFY_EMAIL
        return config_default_email()

    def email_warning(self) -> str | None:
        if self.demo:
            return None
        return ("LIVE MODE: a real notification email may be sent to "
                f"{self.notify_email} when a deal is found.")

    # -- search result store -------------------------------------------------
    def store_result(self, result: dict[str, Any], request: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._results[token] = {"result": result, "request": request}
            self._results_order.append(token)
            while len(self._results_order) > MAX_STORED_RESULTS:
                old = self._results_order.pop(0)
                self._results.pop(old, None)
        return token

    def get_result(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            return self._results.get(token)

    def consume_result(self, token: str, urls: list[str], task_id: int) -> dict[str, Any] | None:
        """Mark a selection as used. Returns the previous consumption, if any.

        The caller turns this into idempotency: submitting the exact same
        confirmation twice must not create a second task.
        """
        with self._lock:
            previous = self._consumed.get(token)
            if previous is None:
                self._consumed[token] = {"urls": list(urls), "task_id": task_id}
            return previous

    def confirm_selection(
        self, token: str, urls: list[str], build: Callable[[], int]
    ) -> tuple[str, int | None]:
        """Atomically confirm a selection (D-R2).

        The consumed-check, task creation and consumption registration all run
        under one lock, so for the same token:

        - the first confirmation creates exactly one task;
        - a concurrent/repeated confirmation with the same URLs returns
          ("duplicate", existing_task_id) without creating anything;
        - a confirmation with different URLs is rejected ("conflict");
        - if ``build`` raises (creation failed), the token is NOT consumed and
          the user can simply retry.
        """
        with self._lock:
            previous = self._consumed.get(token)
            if previous is not None:
                if sorted(previous["urls"]) == sorted(urls):
                    return "duplicate", previous["task_id"]
                return "conflict", None
            task_id = build()  # may raise: nothing is consumed on failure
            self._consumed[token] = {"urls": list(urls), "task_id": task_id}
            return "created", task_id

    # -- intelligent-find store ----------------------------------------------
    def store_find(self, result: dict[str, Any], description: str) -> str:
        """Keep a discovery result so the chosen product can be confirmed later."""
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._finds[token] = {"result": result, "description": description}
            self._finds_order.append(token)
            while len(self._finds_order) > MAX_STORED_RESULTS:
                old = self._finds_order.pop(0)
                self._finds.pop(old, None)
        return token

    def get_find(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            return self._finds.get(token)

    # -- mode-dependent collaborators -----------------------------------------
    def parse_request(self, instruction: str) -> dict[str, Any]:
        if self.demo:
            return demo_data.parse_demo_request(instruction)
        import agent

        return agent.parse_shopping_request(instruction)

    def search(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.demo:
            # Demo mode is fully offline: no web-search provider is consulted at
            # all (not even an "unavailable" lookup), only the demo retailers run.
            return discovery.search_and_compare(
                request, retailers=demo_data.build_demo_retailers(), include_web=False
            )
        # Live: built-in shops plus the web-search provider when it is configured.
        # A missing key degrades to "web search unavailable" — the shops still run.
        import agent

        expand_fn = None
        rank_fn = None
        if config.llm_query_expansion():
            expand_fn = agent.expand_search_queries
        if config.llm_candidate_ranking():
            rank_fn = agent.rank_candidates
        return discovery.search_and_compare(request, expand_fn=expand_fn, rank_fn=rank_fn)

    def find_enabled(self) -> bool:
        """Whether the intelligent-find flow is available in this mode.

        Demo mode always is (its collaborators are offline fixtures). Live mode
        requires the operator to opt in AND a model to be configured; otherwise the
        old explicit search flow stays the only path.
        """
        if self.demo:
            return True
        return config.llm_find_enabled() and config_setup.status()["llm_ready"]

    def find(self, description: str) -> dict[str, Any]:
        """Run the descriptive 5-stage AI pipeline and return its recommendations."""
        if self.demo:
            return find_pipeline.run_find_pipeline(
                description,
                understand_fn=demo_data.demo_find_understand,
                rewrite_fn=demo_data.demo_find_rewrite,
                select_fn=demo_data.demo_find_select,
                describe_fn=demo_data.demo_find_describe,
                search_fn=demo_data.demo_find_search,
                fetch_fn=demo_data.demo_find_fetch,
            )
        # The pipeline gets the description's own constraints (budget/currency/
        # colour/...) so its recommendation boundary enforces them — an
        # over-budget priced offer is never presented as a recommendation.
        # A parse failure degrades to an unconstrained run, never a 500.
        try:
            constraints = self.parse_request(description)
        except Exception:  # noqa: BLE001 - the description stays the source of truth
            constraints = None
        return find_pipeline.run_find_pipeline(description, request=constraints)

    def find_compare(self, find_result: dict[str, Any], chosen: dict[str, Any],
                     base_request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Turn a chosen recommendation into a specific request and compare it.

        The descriptive request's constraints are retained; only the product name
        comes from the real evidence. The comparison then reuses the discovery
        evidence (its candidates, and its already-fetched real quotes) so no price
        is invented and no page is needlessly re-fetched.
        """
        request = iterative_discovery.build_specific_request(base_request, chosen)
        # Validate against the user's own description, not the derived product
        # name: the anti-invention guard checks that a stated constraint (e.g. a
        # Chinese colour word) really came from the user. The English product name
        # built from the evidence would otherwise reject a constraint the user did
        # state.
        instruction = str(find_result.get("description") or request["product_name"])
        request = schemas.validate_search_request(request, instruction)
        candidates = iterative_discovery.web_candidate_dicts(find_result)
        quotes = iterative_discovery.cached_quotes(find_result)

        chosen_key = discovery.normalise_product_url(chosen.get("url") or "")

        def cached_offer(url: str) -> dict[str, Any]:
            key = discovery.normalise_product_url(url)
            quote = quotes.get(key)
            if quote is not None:
                return dict(quote)
            if key and key == chosen_key:
                # The chosen product's own page may not have been read during
                # discovery (the model can finish before pricing every result).
                # Re-read that one observed URL with the real SSRF-protected fetch
                # rather than inventing a price; unfetched *other* candidates stay
                # an honest "not confirmed".
                return iterative_discovery._default_fetch(url)
            raise RuntimeError("This product's price was not confirmed during discovery.")

        if self.demo:
            result = discovery.search_and_compare(
                request, retailers=demo_data.build_demo_retailers(), include_web=False
            )
        else:
            result = discovery.search_and_compare(
                request,
                web_candidates_fn=lambda _query: candidates,
                web_offer_fn=cached_offer,
                include_web=True,
            )
        # Reuse any real evidence the discovery already fetched, keyed by URL.
        result["discovery_evidence"] = quotes
        result["discovery_candidates"] = candidates
        return request, result

    def check_task(self, task: dict[str, Any]) -> dict[str, Any]:
        if self.demo:
            return scheduler.check_task(
                task, db_path=self.db_path,
                fetcher=demo_data.demo_check_fetcher,
                send_fn=demo_data.fake_notify_deal,
            )
        # Live: resolve the collaborators at call time (same functions in normal
        # use) so the offline live-path tests can substitute them process-wide.
        import notifier
        import scraper

        return scheduler.check_task(
            task, db_path=self.db_path,
            fetcher=scraper.fetch_quote,
            send_fn=notifier.notify_deal,
        )

    def make_scheduler(self) -> scheduler.TaskScheduler:
        if self.demo:
            return DemoTaskScheduler(db_path=self.db_path)
        return LiveTaskScheduler(db_path=self.db_path)


def config_default_email() -> str | None:
    import config

    email = config._get("DEFAULT_NOTIFY_EMAIL")
    if not email or "@" not in email:
        return None
    if email == DEMO_NOTIFY_EMAIL:
        # Live mode must never silently fall back to the demo recipient.
        return None
    return email


def _monotonic() -> float:
    import time

    return time.monotonic()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- API


def _task_summary(task: dict[str, Any], db_path: Path) -> dict[str, Any]:
    sources = storage.list_task_sources(task["id"], db_path=db_path)
    latest = {}
    for source in sources:
        rows = storage.list_quotes(source_id=source["id"], limit=1, db_path=db_path)
        if rows:
            latest[source["id"]] = rows[0]
    return {
        "id": task["id"],
        "product_name": task["product_name"],
        "target_price": task["target_price"],
        "currency": task["currency"],
        "comparison": task["comparison"],
        "frequency_minutes": task["frequency_minutes"],
        "status": task["status"],
        "next_check_at": task["next_check_at"],
        "last_check_at": task["last_check_at"],
        "notify_email": task["notify_email"],
        "sources": [
            {
                "id": s["id"],
                "retailer": s["retailer"],
                "url": s["url"],
                "enabled": bool(s["enabled"]),
                "source_kind": s.get("source_kind") or "retailer_adapter",
                "last_checked_at": s["last_checked_at"],
                "last_error": s["last_error"],
                "latest": (
                    {
                        "price": q.get("price"),
                        "currency": q.get("currency"),
                        "in_stock": q.get("in_stock"),
                        "fetched_at": q.get("fetched_at"),
                        "error": q.get("error"),
                    }
                    if (q := latest.get(s["id"])) else None
                ),
            }
            for s in sources
        ],
    }


def api_tasks(state: AppState) -> dict[str, Any]:
    tasks = storage.list_tasks(db_path=state.db_path)
    return {
        "tasks": [_task_summary(t, state.db_path) for t in tasks],
        "demo": state.demo,
        "monitor": monitor_status(state),
        "email_warning": state.email_warning(),
    }


def monitor_status(state: AppState) -> dict[str, Any]:
    mon = state.monitor
    thread = mon.get("thread")
    status = mon["status"]
    # A loop that ended by itself (crash/drain) must not keep showing "running".
    if status == "running" and thread is not None and not thread.is_alive():
        status = mon["status"] = "stopped"
    return {
        "status": status,
        "started_at": mon.get("started_at"),
        "restartable": False,  # per scheduler constraint: one run per process
    }


def api_task_detail(state: AppState, task_id: int) -> dict[str, Any]:
    task = storage.get_task(task_id, db_path=state.db_path)
    if task is None:
        raise WebError(404, f"Task {task_id} not found.")
    summary = _task_summary(task, state.db_path)
    summary["history"] = [
        {
            "fetched_at": q["fetched_at"],
            "source_id": q.get("source_id"),
            "retailer": q.get("retailer"),
            "price": q.get("price"),
            "currency": q.get("currency"),
            "in_stock": q.get("in_stock"),
            "error": q.get("error"),
        }
        for q in storage.list_quotes(task_id=task_id, limit=50, db_path=state.db_path)
    ]
    summary["events"] = [
        {"id": e["id"], "status": e["status"], "created_at": e["created_at"],
         "attempts": e["attempts"], "last_error": e["last_error"]}
        for e in storage.list_events(task_id=task_id, db_path=state.db_path)
    ]
    summary["email_warning"] = state.email_warning()
    return summary


def _apply_supplements(request: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Fill missing fields from the UI supplement form before validation.

    A field supplied through the form is removed from the parser's
    missing_fields list; when nothing required remains missing the request
    becomes ready again — otherwise it stays needs_clarification.
    """
    filled = []
    if body.get("budget") and not request.get("budget"):
        request["budget"] = str(body["budget"])
        filled.append("budget")
    if body.get("currency") and not request.get("currency"):
        request["currency"] = str(body["currency"]).upper()
        filled.append("currency")
    if body.get("frequency_minutes") and not request.get("frequency_minutes"):
        request["frequency_minutes"] = int(body["frequency_minutes"])
        filled.append("frequency_minutes")
    if filled:
        remaining = [f for f in request.get("missing_fields") or [] if f not in filled]
        request["missing_fields"] = remaining
        if not remaining and request.get("product_name"):
            request["status"] = "ready"
            request["clarification_question"] = None
    return request


def _translate_live_error(exc: Exception) -> WebError | None:
    """Turn a parser/configuration failure into an actionable non-500 response.

    ``agent.ParserError`` and ``config.ConfigError`` are the two failure types the
    live search path can raise for the user; their messages are written to be
    user-facing guidance (what to install, what to set, what to check) and never
    contain credentials. Anything else returns None so it keeps the generic 500
    that exposes only the exception type name.
    """
    import agent
    import config

    if isinstance(exc, agent.ParserError):
        return WebError(400, f"The request could not be parsed: {exc}")
    if isinstance(exc, config.ConfigError):
        return WebError(400, f"Configuration problem: {exc}")
    return None


def api_search(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    instruction = str(body.get("instruction") or "").strip()
    if not instruction:
        raise WebError(400, "Please provide a shopping instruction.")
    try:
        request = state.parse_request(instruction)
    except WebError:
        raise
    except Exception as exc:  # parser/config failures must be actionable, not a 500
        translated = _translate_live_error(exc)
        if translated is not None:
            raise translated from exc
        raise
    request = _apply_supplements(request, body)
    try:
        request = schemas.validate_search_request(request, instruction)
    except ValueError as exc:
        raise WebError(400, f"The request could not be validated: {exc}") from exc

    if request["status"] == "unsupported":
        return {"status": "unsupported", "message": request.get("clarification_question")}
    if request["status"] == "needs_clarification":
        return {
            "status": "needs_clarification",
            "missing_fields": request["missing_fields"],
            "message": request.get("clarification_question"),
        }

    try:
        result = state.search(request)
    except WebError:
        raise
    except Exception as exc:  # e.g. a config problem raised inside the search path
        translated = _translate_live_error(exc)
        if translated is not None:
            raise translated from exc
        raise
    # Selection must only ever offer explicitly confirmed matches.
    result["selectable"] = [
        i for i, offer in enumerate(result["offers"], start=1)
        if not offer.get("excluded_reason") and offer.get("match") == discovery.MATCH_SAME
    ]
    token = state.store_result(result, request)
    return {"status": "ready", "token": token, "request": request, "result": result}


def api_find(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """Run the descriptive AI-discovery step (no task is created here).

    The user gives a free-text description; the model decides which searches and
    detail fetches to run inside a bounded loop, and this returns the concrete,
    evidence-backed products it recommends. Nothing is written to the database.
    """
    if not state.find_enabled():
        raise WebError(400, "The intelligent find flow is not enabled. Use the "
                            "standard search, or set LLM_FIND_ENABLED=1 in live mode.")
    description = str(body.get("description") or "").strip()
    if not description:
        raise WebError(400, "Please describe what you are looking for.")
    if len(description) > 400:
        raise WebError(400, "That description is too long; keep it under 400 characters.")
    try:
        result = state.find(description)
    except WebError:
        raise
    except Exception as exc:  # e.g. a config problem raised inside the model path
        translated = _translate_live_error(exc)
        if translated is not None:
            raise translated from exc
        raise
    token = state.store_find(result, description)
    return {
        "status": result.get("status"),
        "token": token,
        "description": description,
        "recommendations": result.get("recommendations") or [],
        "summaries": result.get("summaries") or {},
        "evidence": result.get("evidence") or [],
        "progress": result.get("progress") or {},
        "notes": result.get("notes") or [],
        "stages": result.get("stages") or {},
        "limits": (result.get("progress") or {}).get("limits") or {},
    }


def api_find_confirm(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """Turn a chosen discovery recommendation into a specific-product comparison.

    This establishes the specific product request from the evidence (retaining the
    description's constraints) and runs the existing deterministic comparison. It
    still creates NO task — the user then selects and confirms exactly as before.
    """
    token = str(body.get("token") or "")
    provenance_id = str(body.get("provenance_id") or "")
    if not token or not provenance_id:
        raise WebError(400, "A discovery token and a chosen product are required.")
    entry = state.get_find(token)
    if entry is None:
        raise WebError(404, "This discovery result has expired; please find again.")
    find_result: dict[str, Any] = entry["result"]
    chosen = iterative_discovery.recommendation_by_id(find_result, provenance_id)
    if chosen is None:
        raise WebError(400, "That product was not among the discovered recommendations.")
    base_request = demo_data.parse_demo_request(entry["description"]) if state.demo \
        else state.parse_request(entry["description"])
    try:
        request, result = state.find_compare(find_result, chosen, base_request)
    except WebError:
        raise
    except Exception as exc:
        translated = _translate_live_error(exc)
        if translated is not None:
            raise translated from exc
        raise
    result["selectable"] = [
        i for i, offer in enumerate(result["offers"], start=1)
        if not offer.get("excluded_reason") and offer.get("match") == discovery.MATCH_SAME
    ]
    token2 = state.store_result(result, request)
    return {"status": "ready", "token": token2, "request": request, "result": result,
            "chosen": chosen}


def _validate_task_url(url: str, state: AppState) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebError(400, "Only http/https product URLs are accepted.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise WebError(400, "The URL has no host.")
    if state.demo:
        allowed = {d for r in demo_data.build_demo_retailers() for d in r.domains}
    else:
        import config

        allowed = set(config.allowed_scraper_domains())
    if host.removeprefix("www.") not in allowed:
        raise WebError(400, f"Host {host} is not in the supported shops list.")
    return url


def api_add_task(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """Direct-URL entry: the user supplies every field explicitly.

    Creating the task performs no network call (the URL is only fetched at check
    time), so the failure modes here are input validation and configuration —
    both must come back as actionable 400s, never a bare 500.
    """
    try:
        return _api_add_task_inner(state, body)
    except WebError:
        raise
    except Exception as exc:
        translated = _translate_live_error(exc)
        if translated is not None:
            raise translated from exc
        raise


def _api_add_task_inner(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    url = _validate_task_url(str(body.get("url") or ""), state)
    product_name = str(body.get("product_name") or "").strip()
    target = body.get("target_price")
    currency = str(body.get("currency") or "").upper()
    comparison = body.get("comparison") or "lte"
    frequency = body.get("frequency_minutes")
    if not product_name:
        raise WebError(400, "A product name is required.")
    if not target:
        raise WebError(400, "A target price is required.")
    try:
        from decimal import Decimal

        target = f"{Decimal(str(target)).quantize(Decimal('0.01'))}"
        if Decimal(target) <= 0:
            raise ValueError
    except WebError:
        raise
    except Exception:
        raise WebError(400, f"Invalid target price: {target!r}") from None
    if currency not in ("NZD", "USD", "AUD"):
        raise WebError(400, "currency must be one of NZD/USD/AUD.")
    if comparison not in ("lt", "lte"):
        raise WebError(400, "comparison must be 'lt' or 'lte'.")
    try:
        frequency = int(frequency)
        assert 5 <= frequency <= 525600
    except Exception:
        raise WebError(400, "frequency_minutes must be a whole number of minutes.") from None

    email = state.notify_email
    if not email:
        raise WebError(400, "No notification email is configured for live mode "
                            "(set DEFAULT_NOTIFY_EMAIL).")

    task = {
        "product_name": product_name,
        "url": url,
        "target_price": target,
        "currency": currency,
        "comparison": comparison,
        "frequency_minutes": frequency,
        "condition": None,
        "notify_email": email,
        "status": "active",
        # No discovery identity: the URL itself was the user's confirmation.
    }
    task_id = storage.create_task_with_sources(task, None, db_path=state.db_path)
    return {
        "created": task_id,
        "task": api_task_detail(state, task_id),
        "email_warning": state.email_warning(),
    }


def _validated_selection(
    state: AppState, token: str, urls: Any
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], str]:
    """Shared validation for preview and confirm. Returns (entry, request, chosen, email)."""
    if not token or not isinstance(urls, list) or not urls:
        raise WebError(400, "A search token and at least one offer URL are required.")
    entry = state.get_result(token)
    if entry is None:
        raise WebError(404, "This search result has expired; please search again.")
    result: dict[str, Any] = entry["result"]
    request: dict[str, Any] = entry["request"]

    index_by_url = {i: offer for i, offer in enumerate(result["offers"], start=1)}
    indices = []
    for url in urls:
        match = next((i for i, off in index_by_url.items() if off.get("url") == url), None)
        if match is None:
            raise WebError(400, f"Unknown offer URL: {url}")
        indices.append(match)
    try:
        chosen = discovery.select_offers(result, indices)
    except ValueError as exc:
        raise WebError(400, str(exc)) from exc

    if not request.get("budget"):
        raise WebError(400, "A budget is required before a monitor task can be created.")
    if not chosen[0].get("currency"):
        raise WebError(400, "The selected offer's currency could not be confirmed.")
    email = state.notify_email
    if not email:
        raise WebError(400, "No notification email is configured for live mode "
                            "(set DEFAULT_NOTIFY_EMAIL).")
    return entry, request, chosen, email


def _build_task(request: dict[str, Any], chosen: list[dict[str, Any]], email: str) -> dict[str, Any]:
    """Build the task dict for a confirmed selection (identity included)."""
    confirmed_title = chosen[0].get("detail_name") or chosen[0].get("title") or ""
    return {
        "product_name": chosen[0]["title"] or request["product_name"],
        "url": chosen[0]["url"],
        "target_price": request["budget"],
        "currency": chosen[0]["currency"],
        "comparison": request.get("comparison") or "lte",
        "frequency_minutes": request["frequency_minutes"],
        "condition": request.get("condition"),
        "notify_email": email,
        "status": "active",
        "identity": {
            "product_name": request["product_name"],
            "colour": request.get("colour") or discovery._extract_colour(confirmed_title),
            "capacity": request.get("capacity") or discovery._extract_capacity(confirmed_title),
            "confirmed_title": confirmed_title,
        },
    }


def api_search_select(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """PREVIEW a selection (D-R1): server-validated summary, zero side effects.

    Nothing is written to the database here — the task is only created when the
    user confirms via `/api/search/confirm`. Because no unconfirmed task ever
    exists in the database, a running monitor can never execute one.
    """
    token = str(body.get("token") or "")
    urls = body.get("urls")
    _, request, chosen, email = _validated_selection(state, token, urls)
    draft = _build_task(request, chosen, email)
    return {
        "preview": {
            "product_name": draft["product_name"],
            "sources": [
                {
                    "retailer": o.get("retailer"),
                    "url": o["url"],
                    "source_kind": o.get("source_kind") or "retailer_adapter",
                }
                for o in chosen
            ],
            "target_price": draft["target_price"],
            "currency": draft["currency"],
            "comparison": draft["comparison"],
            "frequency_minutes": draft["frequency_minutes"],
            "notify_email": draft["notify_email"],
        },
        "email_warning": state.email_warning(),
    }


def api_search_confirm(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """CONFIRM a selection: the only path that creates the task (D-R1 + D-R2)."""
    token = str(body.get("token") or "")
    urls = body.get("urls")
    _, request, chosen, email = _validated_selection(state, token, urls)

    def build() -> int:
        task = _build_task(request, chosen, email)
        sources = [
            {
                "url": offer["url"],
                "retailer": offer.get("retailer"),
                "source_kind": offer.get("source_kind") or "retailer_adapter",
            }
            for offer in chosen
        ]
        return storage.create_task_with_sources(task, sources, db_path=state.db_path)

    status, task_id = state.confirm_selection(token, urls, build)
    if status == "conflict":
        raise WebError(409, "This search result was already used for a different selection.")
    if status == "duplicate":
        return {
            "created": task_id,
            "duplicate": True,
            "task": api_task_detail(state, task_id),
            "email_warning": state.email_warning(),
        }
    return {
        "created": task_id,
        "task": api_task_detail(state, task_id),
        "email_warning": state.email_warning(),
    }


def api_setup_status(state: AppState) -> dict[str, Any]:
    """Configuration status. Reports which keys are MISSING — never values.

    SMTP sender details and the DeepSeek API key come from the app's .env;
    the web UI only collects the recipient email.
    """
    status = config_setup.status()
    return {
        "demo": state.demo,
        "complete": status["complete"],
        "smtp_ready": status["smtp_ready"],
        "llm_ready": status["llm_ready"],
        "missing": status["missing"],
        "note": ("SMTP and DeepSeek settings are loaded from the app's .env; the UI "
                 "only sets the recipient email."),
    }


def api_setup_save(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    """Save the notification recipient into the app-local .env (never prints values).

    The web UI deliberately accepts ONLY the recipient email: SMTP sender
    details and the DeepSeek key are operator-level settings maintained in
    .env directly, so they cannot be modified through the page.
    """
    foreign = sorted(k for k in body
                     if k not in ("DEFAULT_NOTIFY_EMAIL",) and not str(k).startswith("token"))
    if foreign:
        raise WebError(400, "Only DEFAULT_NOTIFY_EMAIL can be set here; "
                            f"{', '.join(foreign)} must be configured in .env.")
    raw = body.get("DEFAULT_NOTIFY_EMAIL")
    if raw is None or str(raw).strip() == "":
        raise WebError(400, "A recipient email address is required.")
    value = str(raw).strip()
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise WebError(400, "That does not look like an email address.")
    config_setup.write_updates({"DEFAULT_NOTIFY_EMAIL": value})
    status = config_setup.status()
    return {
        "saved": ["DEFAULT_NOTIFY_EMAIL"],
        "complete": status["complete"],
        "smtp_ready": status["smtp_ready"],
        "llm_ready": status["llm_ready"],
        "missing": status["missing"],
    }


def _require_live_confirm(state: AppState, body: dict[str, Any] | None, action: str) -> None:
    """Server-side gate for live actions that may send a real email.

    The UI asks the user before calling these endpoints, but the frontend is not
    the security boundary: calling the API directly without ``{"confirm": true}``
    must never trigger a real notification. Demo mode is exempt (its sends are
    recorded fakes).
    """
    if not state.demo and not (body or {}).get("confirm"):
        raise WebError(400, f"{action} in live mode may send a real notification "
                            "email — confirm to proceed.")


def api_check(state: AppState, task_id: int, body: dict[str, Any] | None = None) -> dict[str, Any]:
    task = storage.get_task(task_id, db_path=state.db_path)
    if task is None:
        raise WebError(404, f"Task {task_id} not found.")
    _require_live_confirm(state, body, "Checking a task")
    result = state.check_task(task)
    if result.get("skipped"):
        return {"skipped": True, "reason": result.get("reason"),
                "email_warning": state.email_warning()}
    return {
        "skipped": False,
        "is_deal": result.get("is_deal"),
        "notified": result.get("notified"),
        "chosen_retailer": result.get("chosen_retailer"),
        "chosen_price": result.get("chosen_price"),
        "sources_checked": result.get("sources_checked"),
        "sources_failed": result.get("sources_failed"),
        "notification_cancelled": result.get("notification_cancelled"),
        "notification_cancelled_reason": result.get("notification_cancelled_reason"),
        "event_reset": result.get("event_reset"),
        "event_reset_reason": result.get("event_reset_reason"),
        "observations": result.get("observations"),
        "email_warning": state.email_warning(),
    }


def api_check_due(state: AppState, body: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_live_confirm(state, body, "Running due checks")
    now = _now_iso()
    due = storage._fetchall(
        state.db_path,
        "SELECT * FROM tasks WHERE status = 'active' AND next_check_at <= ? ORDER BY id",
        (now,),
    )
    results = []
    for task in due:
        # Already confirmed by the caller's single explicit confirm above.
        results.append({"task_id": task["id"], **api_check(state, task["id"], {"confirm": True})})
    return {"checked": len(results), "results": results,
            "email_warning": state.email_warning()}


def api_status_change(state: AppState, task_id: int, status: str) -> dict[str, Any]:
    task = storage.get_task(task_id, db_path=state.db_path)
    if task is None:
        raise WebError(404, f"Task {task_id} not found.")
    outcome = storage.set_task_status_atomic(
        task_id, status, db_path=state.db_path, block_on_send=True
    )
    return {
        "outcome": outcome,
        "task": _task_summary(storage.get_task(task_id, db_path=state.db_path), state.db_path),
        "email_warning": state.email_warning(),
    }


def api_modify(state: AppState, task_id: int, body: dict[str, Any]) -> dict[str, Any]:
    task = storage.get_task(task_id, db_path=state.db_path)
    if task is None:
        raise WebError(404, f"Task {task_id} not found.")
    target = body.get("target_price")
    comparison = body.get("comparison")
    frequency = body.get("frequency_minutes")
    if target is None and comparison is None and frequency is None:
        raise WebError(400, "Nothing to change.")
    if target is not None:
        try:
            from decimal import Decimal

            if Decimal(str(target)) <= 0:
                raise ValueError
            target = f"{Decimal(str(target)).quantize(Decimal('0.01'))}"
        except WebError:
            raise
        except Exception:
            raise WebError(400, f"Invalid target price: {target!r}") from None
    if comparison is not None and comparison not in ("lt", "lte"):
        raise WebError(400, "comparison must be 'lt' or 'lte'.")
    if frequency is not None:
        try:
            frequency = int(frequency)
            assert frequency > 0
        except Exception:
            raise WebError(400, f"Invalid frequency: {frequency!r}") from None
    threshold_changed = target is not None or comparison is not None
    updated = storage.update_task_conditions(
        task_id, target_price=target, comparison=comparison,
        frequency_minutes=frequency, db_path=state.db_path,
        end_episode=threshold_changed,
    )
    return {
        "updated": True,
        "episode_ended": threshold_changed,
        "task": _task_summary(updated, state.db_path),
        "email_warning": state.email_warning(),
    }


def api_source_toggle(state: AppState, task_id: int, source_id: int, enabled: bool) -> dict[str, Any]:
    task = storage.get_task(task_id, db_path=state.db_path)
    if task is None:
        raise WebError(404, f"Task {task_id} not found.")
    outcome, _row = storage.set_task_source_enabled_for_task(
        task_id, source_id, enabled, db_path=state.db_path
    )
    if outcome == "missing":
        raise WebError(404, f"Source {source_id} not found.")
    if outcome == "foreign":
        raise WebError(400, f"Source {source_id} belongs to a different task.")
    remaining = storage.list_task_sources(task_id, enabled_only=True, db_path=state.db_path)
    return {
        "updated": True,
        "enabled_sources": len(remaining),
        "warning": None if remaining else "This task now has no enabled sources.",
        "task": _task_summary(storage.get_task(task_id, db_path=state.db_path), state.db_path),
    }


def api_history(state: AppState, task_id: int) -> dict[str, Any]:
    task = storage.get_task(task_id, db_path=state.db_path)
    if task is None:
        raise WebError(404, f"Task {task_id} not found.")
    return {
        "task_id": task_id,
        "history": [
            {
                "fetched_at": q["fetched_at"],
                "source_id": q.get("source_id"),
                "retailer": q.get("retailer"),
                "price": q.get("price"),
                "currency": q.get("currency"),
                "in_stock": q.get("in_stock"),
                "error": q.get("error"),
            }
            for q in storage.list_quotes(task_id=task_id, limit=100, db_path=state.db_path)
        ],
    }


def api_monitor_start(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    status = monitor_status(state)["status"]
    if status != "not_started":
        raise WebError(409, f"Continuous monitoring is {status} and cannot be started "
                            "again in this process (the scheduler runs once per process).")
    _require_live_confirm(state, body, "Starting continuous monitoring")
    sched = state.make_scheduler()
    thread = threading.Thread(target=sched.run_loop, daemon=True,
                              name="bargain-agent-monitor-loop")
    thread.start()
    state.monitor.update({
        "status": "running", "scheduler": sched, "thread": thread,
        "started_at": _now_iso(),
    })
    return {"status": "running", "email_warning": state.email_warning()}


def api_monitor_stop(state: AppState) -> dict[str, Any]:
    status = monitor_status(state)["status"]
    if status != "running":
        raise WebError(409, f"Continuous monitoring is {status}; nothing to stop.")
    sched: scheduler.TaskScheduler = state.monitor["scheduler"]
    sched.stop()
    state.monitor["status"] = "stopped"
    return {"status": "stopped", "restartable": False, "email_warning": state.email_warning()}


# --------------------------------------------------------------------------- server


def make_handler(state: AppState, port: int):
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "BargainAgentWeb/1.0"

        def log_message(self, fmt, *args):  # quiet by default
            pass

        # -- plumbing --------------------------------------------------------
        def _csrf_cookie(self) -> str | None:
            cookies = {}
            for part in (self.headers.get("Cookie") or "").split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
            return cookies.get("csrftoken")

        def _send_json(self, status: int, payload: dict[str, Any], set_csrf: bool = False) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if set_csrf:
                self.send_header("Set-Cookie",
                                 f"csrftoken={state.csrf_token}; Path=/; SameSite=Strict")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie",
                             f"csrftoken={state.csrf_token}; Path=/; SameSite=Strict")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 64 * 1024:
                raise WebError(400, "Request body too large.")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise WebError(400, "Request body must be valid JSON.") from None
            if not isinstance(data, dict):
                raise WebError(400, "Request body must be a JSON object.")
            return data

        def _check_write_guards(self) -> None:
            """CSRF + Origin + Host protection for every state-changing request.

            Loopback-only binding is not a substitute for these: any web page open
            in the same browser is otherwise able to reach the local API.
            """
            host = (self.headers.get("Host") or "").lower()
            if host not in allowed_hosts:
                raise WebError(403, "Host header does not match this server.")
            origin = self.headers.get("Origin")
            if origin is None or origin not in allowed_origins:
                raise WebError(403, "Missing or unrecognised Origin header.")
            token = self.headers.get("X-CSRF-Token")
            if not token or token != state.csrf_token:
                raise WebError(403, "Missing or invalid CSRF token.")
            if self._csrf_cookie() != state.csrf_token:
                raise WebError(403, "CSRF cookie mismatch.")

        # -- routing ---------------------------------------------------------
        def do_GET(self):  # noqa: N802
            try:
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    self._send_html(INDEX_HTML)
                    return
                if path == "/api/csrf":
                    self._send_json(200, {"csrf_token": state.csrf_token}, set_csrf=True)
                    return
                if path == "/api/tasks":
                    self._send_json(200, api_tasks(state))
                    return
                if path == "/api/demo/sends":
                    if not state.demo:
                        raise WebError(404, "Not available outside demo mode.")
                    self._send_json(200, {"sends": list(demo_data.RECORDED_SENDS)})
                    return
                if path == "/api/monitor/status":
                    self._send_json(200, monitor_status(state))
                    return
                if path == "/api/setup/status":
                    self._send_json(200, api_setup_status(state))
                    return
                if path.startswith("/api/tasks/"):
                    parts = path.strip("/").split("/")
                    # /api/tasks/{id} | /api/tasks/{id}/history
                    if len(parts) == 4 and parts[3] == "history":
                        self._send_json(200, api_history(state, int(parts[2])))
                        return
                    if len(parts) == 3:
                        self._send_json(200, api_task_detail(state, int(parts[2])))
                        return
                    raise WebError(404, "Not found.")
                raise WebError(404, "Not found.")
            except WebError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except (ValueError, KeyError):
                self._send_json(400, {"error": "Malformed request."})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": f"Internal error: {type(exc).__name__}"})

        def do_POST(self):  # noqa: N802
            try:
                # Write guards run before the body is even parsed.
                self._check_write_guards()
                path = urlparse(self.path).path
                parts = path.strip("/").split("/")
                body = self._read_json()
                if path == "/api/search":
                    self._send_json(200, api_search(state, body))
                elif path == "/api/find":
                    self._send_json(200, api_find(state, body))
                elif path == "/api/find/confirm":
                    self._send_json(200, api_find_confirm(state, body))
                elif path == "/api/search/select":
                    self._send_json(200, api_search_select(state, body))
                elif path == "/api/search/confirm":
                    self._send_json(200, api_search_confirm(state, body))
                elif path == "/api/setup":
                    self._send_json(200, api_setup_save(state, body))
                elif path == "/api/tasks/add":
                    self._send_json(200, api_add_task(state, body))
                elif path == "/api/check/due":
                    self._send_json(200, api_check_due(state, body))
                elif path == "/api/monitor/start":
                    self._send_json(200, api_monitor_start(state, body))
                elif path == "/api/monitor/stop":
                    self._send_json(200, api_monitor_stop(state))
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "tasks":
                    task_id = int(parts[2])
                    action = parts[3]
                    if action == "check":
                        self._send_json(200, api_check(state, task_id, body))
                    elif action == "pause":
                        self._send_json(200, api_status_change(state, task_id, "paused"))
                    elif action == "resume":
                        self._send_json(200, api_status_change(state, task_id, "active"))
                    elif action == "stop":
                        self._send_json(200, api_status_change(state, task_id, "stopped"))
                    elif action == "modify":
                        self._send_json(200, api_modify(state, task_id, body))
                    else:
                        raise WebError(404, "Not found.")
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "tasks" \
                        and parts[3] == "sources" and parts[4] in ("enable", "disable"):
                    self._send_json(200, api_source_toggle(
                        state, int(parts[2]), int(body.get("source_id")), parts[4] == "enable"
                    ))
                else:
                    raise WebError(404, "Not found.")
            except WebError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except (ValueError, KeyError, TypeError):
                # TypeError covers a missing/non-numeric field reaching a bare
                # int() coercion (e.g. a source toggle without a source_id) — a
                # malformed request, not a server fault.
                self._send_json(400, {"error": "Malformed request."})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": f"Internal error: {type(exc).__name__}"})

    return Handler


def build_index_html() -> str:
    index = Path(__file__).with_name("static").joinpath("index.html")
    return index.read_text(encoding="utf-8")


INDEX_HTML = ""


def serve(db_path: Path | None = None, port: int = DEFAULT_PORT, demo: bool = True) -> ThreadingHTTPServer:
    global INDEX_HTML
    INDEX_HTML = build_index_html()
    if db_path is None:
        if demo:
            # Demo mode is isolated by construction: a fresh per-run temporary
            # database that shares nothing with the user's real task data.
            handle = tempfile.NamedTemporaryFile(prefix="webui-demo-", suffix=".db", delete=False)
            handle.close()
            db_path = Path(handle.name)
        else:
            import config

            db_path = config.database_path()
    else:
        if demo:
            import config

            # Refuse to point demo mode at the user's real database.
            if db_path.resolve() == config.database_path().resolve():
                raise SystemExit("Demo mode must not use the real task database; "
                                 "omit --db-path or choose a different file.")
    storage.init_db(db_path)
    state = AppState(db_path=db_path, demo=demo)
    httpd = ThreadingHTTPServer((HOST, port), make_handler(state, port))
    httpd.app_state = state  # type: ignore[attr-defined]
    httpd.db_path = db_path  # type: ignore[attr-defined]
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description="Local web interface for the bargain agent.")
    parser.add_argument("--db-path", default=None,
                        help="Database file. Demo mode uses an isolated per-run temp "
                             "database when omitted; the real task database is refused "
                             "in demo mode.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--live", action="store_true",
                        help="Use the real LLM parser, real shops and real notifier "
                             "(demo mode is the default and is used for acceptance).")
    args = parser.parse_args()

    # Deterministic port-conflict check (SO_REUSEADDR on Windows can otherwise
    # let a second server silently bind the same port and steal connections).
    import socket

    with socket.socket() as probe:
        probe.settimeout(1.0)
        if probe.connect_ex((HOST, args.port)) == 0:
            print(f"Port {args.port} is already in use — another Bargain Agent "
                  "window is probably running (demo and live mode share the port). "
                  "Close it first, then start again.")
            return 1

    httpd = serve(Path(args.db_path) if args.db_path else None,
                  port=args.port, demo=not args.live)
    mode = "DEMO (offline parser, demo shops, fake notifications, isolated temp DB)" \
        if not args.live else "LIVE (real parser, real shops — notifications may be sent)"
    print(f"Bargain Agent web UI: http://{HOST}:{args.port}/  [{mode}]")
    print(f"Database: {httpd.db_path}")  # type: ignore[attr-defined]
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

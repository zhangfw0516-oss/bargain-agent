"""Command-line interface for managing bargain monitoring tasks."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import config
import matching
import pipeline
import pricing
import scheduler
import storage
from agents import task_parser
from sources.base import ProductRef, ProviderError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


class CLIError(RuntimeError):
    """Expected CLI error that should yield a clean non-zero exit."""


def _fmt_task(task: dict) -> str:
    lines = [
        f"[{task['id']}] {task['product_name']}",
        f"    Status: {task['status']}",
        f"    URL: {task['url']}",
        f"    Target: {task['comparison']} {task['target_price']} {task['currency']}",
        f"    Frequency: {task['frequency_minutes']} min",
        f"    Notify: {task['notify_email']}",
        f"    Next check: {task['next_check_at']}",
    ]
    if task.get("condition"):
        lines.append(f"    Condition: {task['condition']}")
    return "\n".join(lines)


def _fmt_summary(parsed: dict, email: str) -> str:
    lines = [
        "Please confirm the following monitoring task:",
        f"  Product: {parsed['product_name']}",
        f"  URL: {parsed['url']}",
        f"  Target: {parsed['comparison']} {parsed['target_price']} {parsed['currency']}",
        f"  Frequency: {parsed['frequency_minutes']} minutes",
        f"  Notify email: {email}",
    ]
    if parsed.get("condition"):
        lines.append(f"  Condition: {parsed['condition']}")
    return "\n".join(lines)


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N]: ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def cmd_add(args: argparse.Namespace) -> int:
    instruction = " ".join(args.instruction)
    try:
        parsed = task_parser.run(instruction)
    except task_parser.TaskParseError as exc:
        print(f"Parse error: {exc}", file=sys.stderr)
        return 1

    # Initialise the database before any read/write so a fresh path is usable
    # even if the user cancels or the request needs clarification.
    _init_or_error(args.db_path)

    if parsed["status"] != "ready":
        print("Need more information before creating a task:")
        print(parsed.get("clarification_question") or "Missing: " + ", ".join(parsed.get("missing_fields", [])))
        return 2

    # A single product page cannot be checked for its condition, so a specific
    # new/used/refurbished requirement must not be silently ignored. (Use `find`:
    # Google Shopping offers carry their condition.)
    condition = parsed.get("condition")
    if condition not in (None, "any"):
        print(
            f"Condition filtering ({condition}) is not supported for a single product page, so no task "
            "was created. Use `find` instead, or re-run without a condition requirement.",
            file=sys.stderr,
        )
        return 4
    if not pricing.is_safe_public_url(parsed["url"]):
        print("Only public http(s) product URLs can be monitored.", file=sys.stderr)
        return 1

    email = args.email or config._get("DEFAULT_NOTIFY_EMAIL")
    if not email or "@" not in email:
        print(
            "A valid notification email is required. Use --email or set DEFAULT_NOTIFY_EMAIL in .env.",
            file=sys.stderr,
        )
        return 1

    if not args.yes:
        print(_fmt_summary(parsed, email))
        if not _confirm("Create this task?"):
            print("Task creation cancelled.")
            return 3

    task = {
        "product_name": parsed["product_name"],
        "url": parsed["url"],
        "target_price": parsed["target_price"],
        "currency": parsed["currency"],
        "comparison": parsed["comparison"],
        "frequency_minutes": parsed["frequency_minutes"],
        "condition": parsed.get("condition"),
        "notify_email": email,
        "status": "active",
    }

    db_path = args.db_path
    storage.init_db(db_path)
    # Falls back to the task URL, so the direct-URL entry produces the same shape as
    # a search-created task: one task, one source.
    task_id = storage.create_task_with_sources(task, None, db_path=db_path)
    print(f"Created task {task_id}")
    print(_fmt_summary(parsed, email))
    return 0


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _fmt_recommendations(result: dict) -> str:
    lines = [f"Searched: {', '.join(result.get('queries') or []) or '(nothing)'}"]
    stages = result.get("stages") or {}
    fallbacks = [name for name, status in stages.items() if status not in ("ok", "skipped")]
    if fallbacks:
        lines.append(f"Stages without AI or results: {', '.join(fallbacks)}")
    recs = result.get("recommendations") or []
    if not recs:
        lines.append("No suitable product with evidence was found.")
    for i, rec in enumerate(recs, start=1):
        price = f"{rec['currency'] or '?'} {rec['price']}" if rec.get("price") else "price unknown"
        trust = "" if rec.get("trusted") else "  [unverified merchant]"
        watch = "" if rec.get("monitorable") else "  [single listing]"
        lines.append(f"  {i}. {rec['title']} — {rec.get('merchant') or 'unknown'}: {price}{trust}{watch}")
        if rec.get("summary"):
            lines.append(f"     {rec['summary']}")
    for note in result.get("notes") or []:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def _fmt_offers(offers: list) -> str:
    lines = [f"Sellers ({len(offers)}):"]
    for offer in offers:
        price = f"{offer.currency or '?'} {offer.price}" if offer.price else "price unknown"
        flags = []
        if not pricing.merchant_trust(offer.merchant, offer.link):
            flags.append("unverified merchant")
        if offer.condition:
            flags.append(offer.condition)
        if offer.in_stock is False:
            flags.append("out of stock")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f"  - {offer.merchant}: {price}{suffix}")
    return "\n".join(lines)


def cmd_find(args: argparse.Namespace) -> int:
    """Describe a product; the AI pipeline finds candidates; pick one to monitor.

    Searching is not permission to monitor: no task is created, and no email can be
    sent, before the user picks a product and confirms the summary.
    """
    description = " ".join(args.description)
    _init_or_error(args.db_path)
    from sources.registry import build_providers

    providers = build_providers(pricing.quota_guard(args.db_path))
    deps = pipeline.FindDeps(shopping=providers.shopping, supplement=providers.supplement,
                             limits=pipeline.FindLimits.from_config())
    try:
        result = pipeline.run_find(description, deps=deps)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(_fmt_recommendations(result))
    recs = result["recommendations"]
    if not recs:
        return 2

    raw = str(args.select) if args.select is not None else _ask(
        f"\nChoose a product to monitor [1-{len(recs)}], or Enter to cancel: ")
    if not raw:
        print("No product selected; nothing was created.")
        return 3
    try:
        rec = recs[int(raw) - 1]
        if int(raw) < 1:
            raise IndexError
    except (ValueError, IndexError):
        print(f"Error: {raw!r} is not one of the listed products.", file=sys.stderr)
        return 1

    need = result.get("need") or {}
    product_source = bool(rec.get("monitorable"))
    detail = getattr(providers, "detail", None)
    if product_source and detail is not None and rec.get("page_token"):
        try:
            offers, fresh = detail.offers(ProductRef(rec.get("product_id"), rec.get("page_token"), None, rec.get("title")))
            rec["page_token"] = fresh.page_token or rec.get("page_token")
            if offers:
                print(_fmt_offers(offers))
        except ProviderError as exc:
            print(f"Could not list sellers right now: {exc}", file=sys.stderr)

    extras: list[dict] = []
    if product_source and not args.no_matches:
        matches = matching.find_same_product(rec, [dict(e) for e in result.get("evidence") or []], need,
                                             verifier=scheduler.default_verifier(), max_sources=config.monitor_max_sources())
        if matches:
            print("Same product at other merchants:")
            for m in matches:
                mark = "+" if m["preselect"] else " "
                trust = "" if m["trusted"] else "  [unverified merchant]"
                print(f"  [{mark}] {m['merchant']}: {m['currency']} {m['price']}{trust}")
            extras = [m for m in matches if m["preselect"]]

    target = args.target_price or need.get("budget")
    if not target:
        target = _ask("Target price (alert at or below): ")
    try:
        target = str(Decimal(str(target)).quantize(Decimal("0.01")))
        if Decimal(target) <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        print("A positive target price is required to monitor.", file=sys.stderr)
        return 2
    currency = (need.get("currency") or rec.get("currency") or "NZD").upper()
    frequency = args.frequency_minutes or 1440
    if product_source:
        frequency = max(frequency, config.monitor_min_frequency_minutes())

    email = args.email or config._get("DEFAULT_NOTIFY_EMAIL")
    if not email or "@" not in email:
        print("A valid notification email is required. Use --email or set DEFAULT_NOTIFY_EMAIL in .env.", file=sys.stderr)
        return 1

    task = {
        "product_name": rec["title"],
        "url": rec["link"],
        "target_price": target,
        "currency": currency,
        "comparison": args.comparison or "lte",
        "frequency_minutes": frequency,
        "condition": need.get("condition"),
        "notify_email": email,
        "status": "active",
        "identity": {"confirmed_title": rec["title"], **{k: need[k] for k in ("brand", "colour", "model") if need.get(k)}},
    }
    if product_source:
        query = matching.monitor_query(rec["title"]) or rec.get("search_query")
        sources = [{"url": item["link"], "retailer": item.get("merchant") or "Google Shopping",
                    "source_kind": storage.SOURCE_SERPAPI, "product_id": item.get("product_id"),
                    "page_token": item.get("page_token"), "search_query": query} for item in [rec] + extras]
    else:
        sources = [{"url": rec["link"], "retailer": rec.get("merchant"), "source_kind": storage.SOURCE_URL}]

    print("")
    print(_fmt_summary(task, email))
    print("  Watching: " + ", ".join(s["retailer"] for s in sources))
    if not args.yes and not _confirm("Create this monitoring task?"):
        print("Task creation cancelled.")
        return 3
    task_id = storage.create_task_with_sources(task, sources, db_path=args.db_path)
    print(f"Created task {task_id}")
    return 0


def _init_or_error(db_path: Path | None) -> None:
    """Ensure the database is initialised, mapping expected errors to CLIError."""
    try:
        storage.init_db(db_path)
    except (sqlite3.Error, storage.StorageError) as exc:
        raise CLIError(f"Database initialisation failed: {exc}") from exc


_PAST_TENSE = {"active": "resumed", "paused": "paused", "stopped": "stopped"}

_IN_FLIGHT_NOTE = (
    "Note: a notification for task {task_id} had already started, so an email may "
    "still be delivered. A message handed to the mail server cannot be recalled."
)


def _apply_status(
    args: argparse.Namespace,
    status: str,
    *,
    next_check_at: str | None = None,
    block_on_send: bool = True,
) -> int:
    """Change a task's status through the same atomic path the scheduler uses.

    Status changes and the scheduler's send permit are decided by one conditional
    UPDATE in SQLite, so they cannot interleave: either the stop is recorded first
    (and no new send is permitted) or the send is already in flight (and the CLI
    says so instead of claiming a clean stop). Resuming does not need to block on
    an in-flight send.
    """
    _init_or_error(args.db_path)
    try:
        outcome = storage.set_task_status_atomic(
            args.id,
            status,
            next_check_at=next_check_at,
            db_path=args.db_path,
            block_on_send=block_on_send,
        )
    except (sqlite3.Error, storage.StorageError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if outcome == "missing":
        print(f"Error: Task {args.id} not found.", file=sys.stderr)
        return 1
    print(f"Task {args.id} {_PAST_TENSE[status]}.")
    if outcome == "in_flight":
        print(_IN_FLIGHT_NOTE.format(task_id=args.id), file=sys.stderr)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _init_or_error(args.db_path)
    try:
        tasks = storage.list_tasks(status=args.status, db_path=args.db_path)
    except (sqlite3.Error, storage.StorageError) as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 1
    if not tasks:
        print("No tasks found.")
        return 0
    for task in tasks:
        print(_fmt_task(task))
        print()
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    return _apply_status(args, "paused")


def cmd_resume(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    return _apply_status(
        args, "active", next_check_at=datetime.now(timezone.utc).isoformat(), block_on_send=False
    )


def cmd_stop(args: argparse.Namespace) -> int:
    return _apply_status(args, "stopped")


def cmd_release_send(args: argparse.Namespace) -> int:
    """Human recovery for a send permit left behind by a process that died mid-send.

    There is deliberately no automatic clearing: a permit held by a *live* sender is
    indistinguishable from an abandoned one at startup, so releasing it implicitly
    could duplicate an email. The operator must confirm the monitoring processes are
    all stopped.
    """
    _init_or_error(args.db_path)
    try:
        permit = storage.get_send_permit(args.id, db_path=args.db_path)
    except (sqlite3.Error, storage.StorageError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if permit is None:
        print(f"Error: Task {args.id} not found.", file=sys.stderr)
        return 1
    if not permit["send_in_flight"]:
        print(f"Task {args.id} holds no send permit; nothing to release.")
        return 0

    print(
        f"Task {args.id} holds a send permit recorded at {permit['last_send_started_at']} "
        f"by owner {permit['send_owner']}."
    )
    print(
        "Release it only after every monitoring process has exited. Clearing the permit of a "
        "sender that is still running can cause a duplicate email."
    )
    if not args.yes and not _confirm("Release this send permit?"):
        print("Release cancelled.")
        return 3

    outcome = storage.clear_send_permit(args.id, db_path=args.db_path)
    if outcome == "cleared":
        print(
            f"Send permit for task {args.id} released. Whether that send reached the recipient "
            "is unknown; the next matching check will notify again."
        )
        return 0
    if outcome == "not_in_flight":
        print(f"Task {args.id} holds no send permit; nothing to do.")
        return 0
    print(f"Error: Task {args.id} not found.", file=sys.stderr)
    return 1


def _require_task_or_error(task_id: int, db_path: Path | None) -> dict | None:
    """Return the task, printing and returning None when it does not exist."""
    try:
        task = storage.get_task(task_id, db_path=db_path)
    except (sqlite3.Error, storage.StorageError) as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return None
    if task is None:
        print(f"Error: Task {task_id} not found.", file=sys.stderr)
        return None
    return task


def _fmt_sources(task: dict, sources: list[dict], quotes: dict[int, list[dict]]) -> str:
    lines = [
        f"Task {task['id']}: {task['product_name']}",
        f"  Target: {task['comparison']} {task['target_price']} {task['currency']}  "
        f"Status: {task['status']}  Next check: {task['next_check_at']}",
        f"  Sources ({len(sources)}):",
    ]
    if not sources:
        lines.append("    (none)")
        return "\n".join(lines)
    for source in sources:
        state = "enabled" if source["enabled"] else "DISABLED"
        error = f"  last error: {source['last_error']}" if source.get("last_error") else ""
        lines.append(f"    [{source['id']}] {source.get('retailer') or 'unknown'} — {state}")
        lines.append(f"        {source['url']}")
        lines.append(f"        last checked: {source.get('last_checked_at') or 'never'}{error}")
        recent = quotes.get(source["id"]) or []
        if recent:
            latest = recent[0]
            price = f"{latest['currency']} {latest['price']}" if latest.get("price") else "price unknown"
            stock = {1: "in stock", 0: "out of stock", None: "stock unknown"}.get(latest["in_stock"])
            lines.append(f"        latest: {price}, {stock}")
    return "\n".join(lines)


def cmd_sources(args: argparse.Namespace) -> int:
    """Show sources for a task, and optionally enable/disable one."""
    _init_or_error(args.db_path)
    task = _require_task_or_error(args.id, args.db_path)
    if task is None:
        return 1

    if args.enable is not None and args.disable is not None:
        print("Error: use either --enable or --disable, not both.", file=sys.stderr)
        return 1

    source_id = args.enable if args.enable is not None else args.disable
    if source_id is not None:
        enabled = args.enable is not None
        try:
            outcome, source = storage.set_task_source_enabled_for_task(
                task["id"], source_id, enabled, db_path=args.db_path
            )
        except (sqlite3.Error, storage.StorageError) as exc:
            print(f"Database error: {exc}", file=sys.stderr)
            return 1
        # Ownership is verified inside the same conditional UPDATE, so a wrong task
        # id can never flip another task's source — not even transiently.
        if outcome == "missing":
            print(f"Error: Source {source_id} not found.", file=sys.stderr)
            return 1
        if outcome == "foreign":
            print(
                f"Error: Source {source_id} belongs to task {source['task_id']}, not {task['id']}.",
                file=sys.stderr,
            )
            return 1
        verb = "enabled" if enabled else "disabled"
        print(f"Source {source_id} {verb}.")
        remaining = storage.list_task_sources(task["id"], db_path=args.db_path, enabled_only=True)
        if not remaining:
            print(
                "Warning: this task now has no enabled sources, so it will not be checked "
                "until a source is enabled again. Use `pause` if you meant to stop checking.",
                file=sys.stderr,
            )

    try:
        sources = storage.list_task_sources(task["id"], db_path=args.db_path)
        quotes: dict[int, list[dict]] = {}
        for source in sources:
            quotes[source["id"]] = storage.list_quotes(source_id=source["id"], limit=1, db_path=args.db_path)
    except (sqlite3.Error, storage.StorageError) as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 1
    print(_fmt_sources(task, sources, quotes))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Show recorded price observations for a task, newest first."""
    _init_or_error(args.db_path)
    task = _require_task_or_error(args.id, args.db_path)
    if task is None:
        return 1
    if args.limit is not None and args.limit <= 0:
        print("Error: --limit must be a positive number.", file=sys.stderr)
        return 1

    try:
        quotes = storage.list_quotes(
            task_id=task["id"], source_id=args.source, limit=args.limit or 20, db_path=args.db_path
        )
    except (sqlite3.Error, storage.StorageError) as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 1

    scope = f"source {args.source}" if args.source else "all sources"
    print(f"Price history for task {task['id']} ({task['product_name']}), {scope}:")
    if not quotes:
        print("  (no observations recorded yet)")
        return 0
    for quote in quotes:
        price = f"{quote['currency']} {quote['price']}" if quote.get("price") else "price unknown"
        stock = {1: "in stock", 0: "out of stock", None: "stock unknown"}.get(quote["in_stock"])
        retailer = quote.get("retailer") or f"source {quote.get('source_id')}"
        if quote.get("error"):
            detail = f"FAILED: {quote['error']}"
        else:
            detail = f"{price}, {stock}"
        print(f"  {quote['fetched_at']}  [{quote['id']}] {retailer}: {detail}")
    return 0


def cmd_modify(args: argparse.Namespace) -> int:
    """Change a task's threshold, comparison or frequency.

    Changing the threshold or comparison means the old deal no longer describes what
    the user wants, so the current episode is ended; the next check decides afresh.
    Changing only the frequency must not re-notify about a deal already reported.
    """
    _init_or_error(args.db_path)
    task = _require_task_or_error(args.id, args.db_path)
    if task is None:
        return 1

    new_target = args.target_price
    if new_target is not None:
        try:
            from decimal import Decimal

            value = Decimal(new_target)
            if value <= 0:
                raise ValueError
        except Exception:
            print(f"Error: --target-price must be a positive number, got {new_target!r}.", file=sys.stderr)
            return 1

    if args.comparison is not None and args.comparison not in ("lt", "lte"):
        print("Error: --comparison must be 'lt' (below) or 'lte' (at or below).", file=sys.stderr)
        return 1

    if args.frequency_minutes is not None and args.frequency_minutes <= 0:
        print("Error: --frequency-minutes must be a positive number.", file=sys.stderr)
        return 1
    if args.frequency_minutes is not None and any(
        s.get("source_kind") == storage.SOURCE_SERPAPI for s in storage.list_task_sources(task["id"], db_path=args.db_path)
    ):
        args.frequency_minutes = max(args.frequency_minutes, config.monitor_min_frequency_minutes())

    if new_target is None and args.comparison is None and args.frequency_minutes is None:
        print(
            "Error: nothing to change. Use --target-price, --comparison or --frequency-minutes.",
            file=sys.stderr,
        )
        return 1

    try:
        updated = storage.update_task_conditions(
            task["id"],
            target_price=new_target,
            comparison=args.comparison,
            frequency_minutes=args.frequency_minutes,
            db_path=args.db_path,
            # Threshold/comparison changes bump config_version AND reset active
            # events in one transaction, so an in-flight check can neither send
            # under the old conditions nor see the new threshold with the old
            # episode.
            end_episode=new_target is not None or args.comparison is not None,
        )
    except (sqlite3.Error, storage.StorageError) as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 1
    if updated is None:
        print(f"Error: Task {task['id']} not found.", file=sys.stderr)
        return 1

    threshold_changed = new_target is not None or args.comparison is not None
    if threshold_changed:
        print(
            f"Ended the active notification episode for task {task['id']}; "
            "the new threshold applies from the next check."
        )

    print(
        f"Task {task['id']} updated: {updated['comparison']} {updated['target_price']} "
        f"{updated['currency']}, every {updated['frequency_minutes']} minutes."
    )
    if not threshold_changed:
        print("Threshold unchanged, so any deal already reported will not be re-sent.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    _init_or_error(args.db_path)
    sched = scheduler.TaskScheduler(db_path=args.db_path)
    try:
        result = sched.run_once(args.id)
    except scheduler.SchedulerError as exc:
        print(f"Check failed: {exc}", file=sys.stderr)
        return 1
    print(f"Check complete: {result}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _init_or_error(args.db_path)
    sched = scheduler.TaskScheduler(db_path=args.db_path)
    print("Scheduler running. Press Ctrl+C to stop.")
    try:
        sched.run_loop()
    except KeyboardInterrupt:
        sched.stop()
    return 0


def _db_path(path: str | None) -> Path | None:
    return Path(path) if path else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bargain-agent", description="Monitor product prices and send alerts.")
    parser.add_argument("--db-path", type=str, default=None, help="Path to SQLite database.")
    sub = parser.add_subparsers(dest="command", required=True)

    add_parser = sub.add_parser("add", help="Add a monitoring task from natural language.")
    add_parser.add_argument("instruction", nargs="+", help="Natural language instruction including a product URL.")
    add_parser.add_argument("--email", type=str, default=None, help="Notification email address.")
    add_parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip interactive confirmation and create the task immediately. Equivalent to confirming the parsed summary.",
    )
    add_parser.set_defaults(func=cmd_add)

    find_parser = sub.add_parser(
        "find",
        help="Describe a product; AI agents search Google Shopping and recommend; then monitor one.",
    )
    find_parser.add_argument("description", nargs="+", help='e.g. "跑步用的无线耳机，300纽币以内"')
    find_parser.add_argument("--email", type=str, default=None, help="Notification email address.")
    find_parser.add_argument("--select", type=int, default=None, help="Recommendation number to monitor.")
    find_parser.add_argument("--target-price", type=str, default=None, help="Alert threshold (default: the stated budget).")
    find_parser.add_argument("--comparison", type=str, default=None, choices=["lt", "lte"])
    find_parser.add_argument("--frequency-minutes", type=int, default=None, help="Check interval (default 1440).")
    find_parser.add_argument("--no-matches", action="store_true", default=False,
                             help="Watch only the chosen listing, not the same product at other merchants.")
    find_parser.add_argument("--yes", action="store_true", default=False, help="Skip the confirmation prompt.")
    find_parser.set_defaults(func=cmd_find)

    list_parser = sub.add_parser("list", help="List monitoring tasks.")
    list_parser.add_argument("--status", type=str, default=None, choices=["active", "paused", "stopped"])
    list_parser.set_defaults(func=cmd_list)

    sources_parser = sub.add_parser(
        "sources", help="Show a task's monitored sources, or enable/disable one."
    )
    sources_parser.add_argument("id", type=int, help="Task ID.")
    sources_parser.add_argument("--enable", type=int, default=None, metavar="SOURCE_ID")
    sources_parser.add_argument("--disable", type=int, default=None, metavar="SOURCE_ID")
    sources_parser.set_defaults(func=cmd_sources)

    history_parser = sub.add_parser(
        "history", help="Show recorded price observations for a task, newest first."
    )
    history_parser.add_argument("id", type=int, help="Task ID.")
    history_parser.add_argument("--source", type=int, default=None, help="Only this source ID.")
    history_parser.add_argument("--limit", type=int, default=20, help="How many rows to show.")
    history_parser.set_defaults(func=cmd_history)

    modify_parser = sub.add_parser(
        "modify", help="Change a task's target price, comparison or check frequency."
    )
    modify_parser.add_argument("id", type=int, help="Task ID.")
    modify_parser.add_argument("--target-price", type=str, default=None)
    modify_parser.add_argument("--comparison", type=str, default=None, choices=["lt", "lte"])
    modify_parser.add_argument("--frequency-minutes", type=int, default=None)
    modify_parser.set_defaults(func=cmd_modify)

    for name, handler in [("pause", cmd_pause), ("resume", cmd_resume), ("stop", cmd_stop), ("check", cmd_check)]:
        p = sub.add_parser(name, help=f"{name.capitalize()} a task.")
        p.add_argument("id", type=int, help="Task ID.")
        p.set_defaults(func=handler)

    release_parser = sub.add_parser(
        "release-send",
        help=(
            "Release a send permit left behind by a process that exited mid-send. "
            "Run only after all monitoring processes have stopped."
        ),
    )
    release_parser.add_argument("id", type=int, help="Task ID.")
    release_parser.add_argument(
        "--yes", action="store_true", default=False, help="Skip the confirmation prompt."
    )
    release_parser.set_defaults(func=cmd_release_send)

    run_parser = sub.add_parser("run", help="Run the scheduler loop.")
    run_parser.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.db_path:
        args.db_path = _db_path(args.db_path)
    try:
        return args.func(args)
    except CLIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line interface for managing bargain monitoring tasks."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import agent
import config
import discovery
import scheduler
import storage

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


def _fmt_planned_sources(sources: list[dict]) -> str:
    lines = [f"  Sources to monitor ({len(sources)}):"]
    for source in sources:
        lines.append(f"    - {source.get('retailer') or 'unknown'}: {source['url']}")
    if len(sources) > 1:
        lines.append(
            "  All sources are checked every cycle; one alert is sent per deal, "
            "based on the best usable offer."
        )
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
        parsed = agent.parse_user_instruction(instruction)
    except agent.ParserError as exc:
        print(f"Parse error: {exc}", file=sys.stderr)
        return 1

    # Initialise the database before any read/write so a fresh path is usable
    # even if the user cancels or the request needs clarification.
    _init_or_error(args.db_path)

    if parsed["status"] != "ready":
        print("Need more information before creating a task:")
        print(parsed.get("clarification_question") or "Missing: " + ", ".join(parsed.get("missing_fields", [])))
        return 2

    # M40 cannot verify a product's condition against the scraped page, so a
    # specific new/used/refurbished requirement must not be silently ignored.
    condition = parsed.get("condition")
    if condition not in (None, "any"):
        print(
            f"Condition filtering ({condition}) is not supported yet, so no task was created. "
            "Re-run without a condition requirement, or wait for a later milestone.",
            file=sys.stderr,
        )
        return 4

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
    print(agent.summarise_parsed_task(parsed))
    return 0


def _fmt_search_request(request: dict) -> str:
    bits = [f'Searching for "{request["product_name"]}"']
    if request.get("colour"):
        bits.append(f'colour {request["colour"]}')
    if request.get("capacity"):
        bits.append(f'capacity {request["capacity"]}')
    if request.get("budget"):
        bits.append(f'budget {request.get("currency") or "?"} {request["budget"]}')
    return ", ".join(bits)


def _llm_explain(kind, reasons):
    """Optional plain-English explanation; returns empty string on any failure."""
    if not config.llm_explain_results():
        return ""
    try:
        return agent.explain_result(kind, reasons)
    except Exception:
        return ""


def _fmt_search_result(result: dict) -> str:
    """Render the comparison so every uncertainty is visible to the user."""
    lines: list[str] = ["", "Sources:"]
    for source in result["sources"]:
        if source["status"] == "ok":
            lines.append(f"  - {source['retailer']}: {source['candidates']} candidate(s)")
        elif source["status"] == "disabled":
            lines.append(f"  - {source['retailer']}: NOT SEARCHED (disabled in configuration)")
        else:
            lines.append(f"  - {source['retailer']}: FAILED ({source['error']})")

    counts = result["counts"]
    lines.append("")
    lines.append(
        f"Matched {counts['same']} listing(s) to the request; "
        f"{counts['needs_confirmation']} need confirmation; "
        f"{counts['different']} rejected as a different product."
    )
    for candidate in result["candidates"]:
        if candidate["match"] == discovery.MATCH_DIFFERENT:
            lines.append(f"  rejected: {candidate['title']} — {'; '.join(candidate['reasons'])}")
    for candidate in result["candidates"]:
        if candidate["match"] == discovery.MATCH_NEEDS_CONFIRMATION:
            lines.append(f"  needs confirmation: {candidate['title']} — {'; '.join(candidate['reasons'])}")
            explain = _llm_explain("needs_confirmation", candidate["reasons"])
            if explain:
                lines.append("      why: " + explain)

    lines.append("")
    if not result["offers"]:
        lines.append("No comparable offer was found.")
    else:
        lines.append("Offers (best first; excluded rows listed last):")
        for position, offer in enumerate(result["offers"], start=1):
            price = f"{offer['currency']} {offer['price']}" if offer["price"] else "price unknown"
            stock = {True: "in stock", False: "out of stock", None: "stock unknown"}[offer["in_stock"]]
            flags = []
            if offer["excluded_reason"]:
                flags.append(f"EXCLUDED ({offer['excluded_reason']})")
            elif offer.get("match") != discovery.MATCH_SAME:
                # Shown, but never offered as a choice: we could not confirm it is
                # the product the user asked for. A missing match verdict counts as
                # unconfirmed too — only an explicit `same` is selectable.
                flags.append(f"NOT SELECTABLE ({offer.get('match') or 'unverified'})")
            elif offer["within_budget"] is True:
                flags.append("within budget")
            elif offer["within_budget"] is False:
                flags.append("above budget")
            suffix = f"  [{'; '.join(flags)}]" if flags else ""
            lines.append(f"  {position}. {offer['retailer']}: {price} — {stock}{suffix}")
            lines.append(f"     {offer['title']}")
            lines.append(f"     {offer['url']}")
            lines.append(f"     queried {offer['queried_at']}")
            for note in offer["notes"]:
                lines.append(f"     note: {note}")

    if result.get("unavailable"):
        lines.append("")
        lines.append("Could not read a price:")
        for entry in result["unavailable"]:
            lines.append("  - " + entry["retailer"] + ": " + entry["title"] + " (" + entry["reason"] + ")")
            explain = _llm_explain("unavailable", [entry["reason"]])
            if explain:
                lines.append("      why: " + explain)

    if result["notes"]:
        lines.append("")
        lines.append("Notes:")
        for note in result["notes"]:
            lines.append(f"  - {note}")
    return "\n".join(lines)


def cmd_search(args: argparse.Namespace) -> int:
    """Search supported retailers by product name, then optionally monitor one offer.

    Splits the two decisions on purpose: searching is not permission to monitor, and
    no task is created — and no email can be sent — before the user picks an offer and
    confirms it.
    """
    instruction = " ".join(args.instruction)
    try:
        request = agent.parse_shopping_request(instruction)
    except agent.ParserError as exc:
        print(f"Parse error: {exc}", file=sys.stderr)
        return 1

    if request["status"] != "ready":
        print("Need more information before searching:")
        print(request.get("clarification_question") or "Missing: " + ", ".join(request.get("missing_fields", [])))
        return 2

    # B2: `search` must honour a new/used/refurbished requirement exactly like `add`.
    # The monitoring module does not verify condition against the page, so accepting the
    # request here would create a task that silently ignores what the user asked for.
    # Refuse before searching rather than dropping the condition and carrying on.
    condition = request.get("condition")
    if condition not in (None, "any"):
        print(
            f"Condition filtering ({condition}) is not supported yet, so nothing was searched "
            "and no task was created. Re-run without a condition requirement, or wait for a "
            "later milestone.",
            file=sys.stderr,
        )
        return 4

    if request.get("url"):
        print(
            "You supplied a product URL. Use `add` for the direct-URL monitoring flow; "
            "`search` is for finding products by name.",
            file=sys.stderr,
        )
        return 2

    print(_fmt_search_request(request))
    expand_fn = None
    rank_fn = None
    if config.llm_query_expansion():
        expand_fn = agent.expand_search_queries
    if config.llm_candidate_ranking():
        rank_fn = agent.rank_candidates
    result = discovery.search_and_compare(request, expand_fn=expand_fn, rank_fn=rank_fn)
    print(_fmt_search_result(result))

    offers = result["offers"]
    # Only a confirmed "same" offer can be monitored; unconfirmed ones stay visible so
    # the user can see why they were not offered as a choice.
    selectable = [
        i
        for i, offer in enumerate(offers, start=1)
        if not offer.get("excluded_reason")
        and offer.get("match") == discovery.MATCH_SAME
    ]
    # When the user is being prompted we must have something to offer; when the user
    # explicitly passes --select we let `select_offers` produce a precise error per
    # selection rather than collapsing every unconfirmed pick into one generic line.
    if args.select is None and not selectable:
        print("\nNothing can be monitored from this search.")
        return 2

    if args.select is not None:
        raw = str(args.select)
    else:
        try:
            raw = input(
                f"\nChoose offer(s) to monitor [{'/'.join(str(i) for i in selectable)}], "
                "comma-separated for several, or Enter to cancel: "
            ).strip()
        except EOFError:
            raw = ""
        if not raw:
            print("No offer selected; nothing was created.")
            return 3

    try:
        indices = [part.strip() for part in raw.split(",") if part.strip()]
        chosen = discovery.select_offers(result, indices)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not request.get("budget"):
        print(
            "A target price is required to monitor: re-run with a budget, e.g. \"... 预算 NZD200 ...\".",
            file=sys.stderr,
        )
        return 2
    if not chosen[0].get("currency"):
        print("The selected offer's currency could not be confirmed, so no task was created.", file=sys.stderr)
        return 2

    email = args.email or config._get("DEFAULT_NOTIFY_EMAIL")
    if not email or "@" not in email:
        print(
            "A valid notification email is required. Use --email or set DEFAULT_NOTIFY_EMAIL in .env.",
            file=sys.stderr,
        )
        return 1

    parsed_task = {
        "product_name": chosen[0]["title"] or request["product_name"],
        "url": chosen[0]["url"],
        "target_price": request["budget"],
        "currency": chosen[0]["currency"],
        "comparison": request.get("comparison") or "lte",
        "frequency_minutes": request["frequency_minutes"],
        "condition": request.get("condition"),
    }
    sources = [{"url": offer["url"], "retailer": offer.get("retailer")} for offer in chosen]

    # The source summary is always shown; --yes only skips the prompt. Skipping the
    # prompt is documented as confirming the displayed summary, so the summary must be
    # on screen in both modes.
    print("")
    print(_fmt_summary(parsed_task, email))
    print(_fmt_planned_sources(sources))
    if not args.yes and not _confirm("Create this monitoring task?"):
        print("Task creation cancelled.")
        return 3

    _init_or_error(args.db_path)
    # The confirmed identity travels with the task so every later check can
    # verify a fetched quote is still the product the user asked for. A detail
    # page that later serves an accessory or a different colour must never
    # trigger (or end) a deal episode. Colour/capacity the user did NOT state are
    # inferred from the confirmed offer's title: selecting "Obsidian / 16GB" makes
    # those the confirmed specs, so a later "Lavender / 32GB" page is refused.
    confirmed_title = chosen[0].get("detail_name") or chosen[0].get("title") or ""
    task = {
        **parsed_task,
        "notify_email": email,
        "status": "active",
        "identity": {
            "product_name": request["product_name"],
            "colour": request.get("colour") or discovery._extract_colour(confirmed_title),
            "capacity": request.get("capacity") or discovery._extract_capacity(confirmed_title),
            "confirmed_title": confirmed_title,
        },
    }
    task_id = storage.create_task_with_sources(task, sources, db_path=args.db_path)
    print(f"Created task {task_id}")
    print(agent.summarise_parsed_task(parsed_task))
    print(f"Monitoring {len(sources)} source(s): {', '.join(s['retailer'] or s['url'] for s in sources)}")
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

    search_parser = sub.add_parser(
        "search",
        help="Find products by name across supported retailers, compare offers, then optionally monitor one.",
    )
    search_parser.add_argument(
        "instruction",
        nargs="+",
        help='Natural language request without a URL, e.g. "找 Fitbit Air 黑色 预算 NZD200 每小时看一次".',
    )
    search_parser.add_argument("--email", type=str, default=None, help="Notification email address.")
    search_parser.add_argument(
        "--select",
        type=str,
        default=None,
        help=(
            "Offer number(s) to monitor, skipping the interactive prompt. Use a comma "
            "for several confirmed offers, e.g. --select 1,2, which are saved as "
            "sources of one task."
        ),
    )
    search_parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip the confirmation prompt for the selected offer(s).",
    )
    search_parser.set_defaults(func=cmd_search)

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

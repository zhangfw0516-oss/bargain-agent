"""SQLite persistence for tasks, quotes and notification events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

import config


class StorageError(RuntimeError):
    """Raised on database or schema errors."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    raw = db_path if db_path is not None else config.database_path()
    # Accept str as well as Path: callers include subprocesses and CLI flags.
    path = raw if isinstance(raw, Path) else Path(str(raw))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _transaction(
    connection_or_path: sqlite3.Connection | Path | str | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    if isinstance(connection_or_path, sqlite3.Connection):
        conn = connection_or_path
        should_close = False
    elif connection_or_path is None:
        conn = _connect()
        should_close = True
    else:
        conn = _connect(connection_or_path)
        should_close = True
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if should_close:
            conn.close()


def _fetchone(db_path: Path | None, sql: str, params: tuple) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _fetchall(db_path: Path | None, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with _transaction(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','paused','stopped')),
                product_name TEXT NOT NULL,
                url TEXT NOT NULL,
                target_price TEXT NOT NULL,
                currency TEXT NOT NULL,
                comparison TEXT NOT NULL,
                frequency_minutes INTEGER NOT NULL,
                condition TEXT,
                notify_email TEXT NOT NULL,
                next_check_at TEXT NOT NULL,
                last_check_at TEXT,
                last_quote_id INTEGER,
                last_event_id INTEGER,
                send_in_flight INTEGER NOT NULL DEFAULT 0,
                last_send_started_at TEXT,
                send_owner TEXT,
                config_version INTEGER NOT NULL DEFAULT 0,
                identity_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status_next_check
                ON tasks(status, next_check_at);

            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                price TEXT,
                currency TEXT,
                in_stock INTEGER,
                product_name TEXT,
                retailer TEXT,
                url TEXT,
                error TEXT,
                response_ms INTEGER,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_quotes_task_id ON quotes(task_id);

            CREATE TABLE IF NOT EXISTS notification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                quote_id INTEGER,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','sent','failed','reset')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                sent_at TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id),
                FOREIGN KEY(quote_id) REFERENCES quotes(id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_task_id ON notification_events(task_id);

            CREATE TABLE IF NOT EXISTS task_locks (
                task_id INTEGER PRIMARY KEY,
                owner TEXT NOT NULL,
                acquired_at TEXT NOT NULL
            );

            -- Stage C: one monitoring task may watch several confirmed sources.
            CREATE TABLE IF NOT EXISTS task_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                retailer TEXT,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_error TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id),
                UNIQUE(task_id, url)
            );

            CREATE INDEX IF NOT EXISTS idx_sources_task_id ON task_sources(task_id);
            """
        )
        # Migrations for databases created before these columns existed.
        _ensure_column(conn, "tasks", "send_in_flight", "send_in_flight INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "tasks", "last_send_started_at", "last_send_started_at TEXT")
        _ensure_column(conn, "tasks", "send_owner", "send_owner TEXT")
        _ensure_column(conn, "tasks", "config_version", "config_version INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "tasks", "identity_json", "identity_json TEXT")
        _ensure_column(conn, "quotes", "source_id", "source_id INTEGER")
        # Discovery expansion: where a source's URL came from. Pre-existing rows
        # default to the built-in-shop path they were created by.
        _ensure_column(
            conn,
            "task_sources",
            "source_kind",
            "source_kind TEXT NOT NULL DEFAULT 'retailer_adapter'",
        )

        # One-time backfill: a task created before Stage C has no source row. Give it
        # exactly one, derived from the URL it was already monitoring, so the old
        # single-URL flow keeps working unchanged. Guarded by "no rows exist for this
        # task", so re-running never duplicates and never touches tasks that already
        # have sources.
        _backfill_sources(conn)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _host_of(url: str | None) -> str | None:
    """Best-effort retailer label from a URL. Never raises."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
    except Exception:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _backfill_sources(conn: sqlite3.Connection) -> None:
    """Give every pre-Stage-C task exactly one source row, derived from its URL.

    Idempotent: tasks that already have a source are skipped, so the migration can run
    on every `init_db` without duplicating sources for multi-source tasks.
    """
    rows = conn.execute(
        """
        SELECT t.id, t.url
        FROM tasks t
        WHERE NOT EXISTS (SELECT 1 FROM task_sources s WHERE s.task_id = t.id)
        """
    ).fetchall()
    now = _now()
    for row in rows:
        url = row["url"]
        if not url:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO task_sources
            (task_id, retailer, url, enabled, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (row["id"], _host_of(url), url, now, now),
        )


def create_task(task: dict[str, Any], db_path: Path | None = None) -> int:
    """Create a task, plus one source derived from its URL.

    Delegating keeps a single creation path: every task always has at least one
    source, so the checking logic never has to special-case legacy rows. Callers that
    want several sources use :func:`create_task_with_sources`.
    """
    return create_task_with_sources(task, None, db_path=db_path)


def create_task_with_sources(
    task: dict[str, Any],
    sources: list[dict[str, Any]] | None = None,
    db_path: Path | None = None,
) -> int:
    """Create a task and its sources in one transaction.

    Falls back to the task URL when no source is given, so every task always has at
    least one source and the checking path never has to special-case legacy rows.

    A source entry may carry ``source_kind`` (``retailer_adapter`` for the built-in
    shops, ``web_search`` for a URL found by web search). Anything unspecified is
    the built-in-shop path — old callers keep their behaviour.
    """
    now = _now()
    chosen = sources or [{"url": task["url"], "retailer": _host_of(task["url"])}]
    identity = task.get("identity")
    identity_json = json.dumps(identity, ensure_ascii=False) if identity else None
    with _transaction(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks
            (created_at, updated_at, status, product_name, url, target_price, currency,
             comparison, frequency_minutes, condition, notify_email, next_check_at,
             identity_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                task.get("status", "active"),
                task["product_name"],
                task["url"],
                task["target_price"],
                task["currency"],
                task["comparison"],
                task["frequency_minutes"],
                task.get("condition"),
                task["notify_email"],
                task.get("next_check_at") or now,
                identity_json,
            ),
        )
        task_id = int(cursor.lastrowid)
        for source in chosen:
            url = source.get("url")
            if not url:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO task_sources
                (task_id, retailer, url, source_kind, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    task_id,
                    source.get("retailer") or _host_of(url),
                    url,
                    source.get("source_kind") or "retailer_adapter",
                    now,
                    now,
                ),
            )
        return task_id


def get_task(task_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    return _fetchone(db_path, "SELECT * FROM tasks WHERE id = ?", (task_id,))


def list_tasks(status: str | None = None, db_path: Path | None = None) -> list[dict[str, Any]]:
    if status:
        return _fetchall(db_path, "SELECT * FROM tasks WHERE status = ? ORDER BY id", (status,))
    return _fetchall(db_path, "SELECT * FROM tasks ORDER BY id")


def update_task_status(task_id: int, status: str, db_path: Path | None = None) -> None:
    with _transaction(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), task_id),
        )


def update_next_check(task_id: int, next_check_at: str, db_path: Path | None = None) -> None:
    with _transaction(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET next_check_at = ?, updated_at = ? WHERE id = ?",
            (next_check_at, _now(), task_id),
        )


def update_task_conditions(
    task_id: int,
    *,
    target_price: str | None = None,
    comparison: str | None = None,
    frequency_minutes: int | None = None,
    db_path: Path | None = None,
    end_episode: bool = False,
) -> dict[str, Any] | None:
    """Change a task's threshold, comparison or frequency.

    Returns the updated task row, or None if the task does not exist. Only the
    supplied fields are written. The caller decides whether the change also ends the
    current deal episode; this function never touches notification events by default.

    With ``end_episode=True`` (threshold/comparison changes), the config version is
    bumped and every active notification event is reset **in the same transaction**,
    so a concurrent check can never observe the new threshold together with the old
    episode (or send under the old threshold after the change committed).
    """
    sets: list[str] = []
    params: list[Any] = []
    threshold_changed = target_price is not None or comparison is not None
    if target_price is not None:
        sets.append("target_price = ?")
        params.append(target_price)
    if comparison is not None:
        sets.append("comparison = ?")
        params.append(comparison)
    if frequency_minutes is not None:
        sets.append("frequency_minutes = ?")
        params.append(int(frequency_minutes))
    if not sets:
        return get_task(task_id, db_path=db_path)
    if threshold_changed:
        # A stale config version blocks an in-flight check's send permit (see
        # try_begin_send), so the old conditions can never send after this commit.
        sets.append("config_version = config_version + 1")
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(task_id)
    with _transaction(db_path) as conn:
        if end_episode:
            conn.execute(
                "UPDATE notification_events SET status = 'reset' "
                "WHERE task_id = ? AND status IN ('pending','sent','failed')",
                (task_id,),
            )
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))
    return get_task(task_id, db_path=db_path)


def update_last_check(task_id: int, quote_id: int | None, db_path: Path | None = None) -> None:
    now = _now()
    with _transaction(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET last_check_at = ?, last_quote_id = ?, updated_at = ? WHERE id = ?",
            (now, quote_id, now, task_id),
        )


def save_quote(
    task_id: int,
    quote: dict[str, Any],
    db_path: Path | None = None,
    source_id: int | None = None,
) -> int:
    in_stock = quote.get("in_stock")
    in_stock_int = None if in_stock is None else (1 if in_stock else 0)
    with _transaction(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO quotes
            (task_id, source_id, fetched_at, price, currency, in_stock, product_name,
             retailer, url, error, response_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                source_id,
                quote.get("fetched_at") or _now(),
                quote.get("price"),
                quote.get("currency"),
                in_stock_int,
                quote.get("product_name"),
                quote.get("retailer"),
                quote.get("url"),
                quote.get("error"),
                quote.get("response_ms"),
            ),
        )
        return int(cursor.lastrowid)


# --------------------------------------------------------------------------- Stage C
# Task sources: one task, several confirmed shops.


def add_task_source(
    task_id: int,
    url: str,
    retailer: str | None = None,
    db_path: Path | None = None,
    source_kind: str = "retailer_adapter",
) -> int:
    """Attach a source to a task. The same URL cannot be attached twice."""
    now = _now()
    with _transaction(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO task_sources
            (task_id, retailer, url, source_kind, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (task_id, retailer or _host_of(url), url, source_kind, now, now),
        )
        if int(cursor.rowcount) == 1:
            return int(cursor.lastrowid)
        # Already present: return the existing row so callers can link quotes to it.
        row = conn.execute(
            "SELECT id FROM task_sources WHERE task_id = ? AND url = ?", (task_id, url)
        ).fetchone()
        return int(row["id"]) if row else 0


def list_task_sources(
    task_id: int, db_path: Path | None = None, enabled_only: bool = False
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM task_sources WHERE task_id = ?"
    params: tuple = (task_id,)
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY id"
    return _fetchall(db_path, sql, params)


def get_task_source(source_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    return _fetchone(db_path, "SELECT * FROM task_sources WHERE id = ?", (source_id,))


def set_task_source_enabled(
    source_id: int, enabled: bool, db_path: Path | None = None
) -> dict[str, Any] | None:
    """Enable or disable one source. Returns the updated row, or None if absent."""
    with _transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM task_sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE task_sources SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), source_id),
        )
        updated = dict(row)
        updated["enabled"] = 1 if enabled else 0
        return updated


def set_task_source_enabled_for_task(
    task_id: int,
    source_id: int,
    enabled: bool,
    db_path: Path | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Enable/disable a source **verified against its owning task**.

    The write is one conditional UPDATE keyed on both the source id and the task id,
    so a source from another task can never be modified — not even transiently.

    Returns ``(status, row)`` where status is one of:

    - ``"updated"`` — the row exists, belongs to ``task_id``, and was written;
    - ``"missing"`` — no source with that id exists (row is None);
    - ``"foreign"`` — the source exists but belongs to a different task; nothing
      was written (row is the untouched foreign row).
    """
    with _transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM task_sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return "missing", None
        if int(row["task_id"]) != int(task_id):
            return "foreign", dict(row)
        conn.execute(
            "UPDATE task_sources SET enabled = ?, updated_at = ? WHERE id = ? AND task_id = ?",
            (1 if enabled else 0, _now(), source_id, task_id),
        )
        updated = dict(row)
        updated["enabled"] = 1 if enabled else 0
        return "updated", updated


def record_source_check(
    source_id: int, *, error: str | None = None, db_path: Path | None = None
) -> None:
    """Record that a source was checked, with its error (or success)."""
    now = _now()
    with _transaction(db_path) as conn:
        conn.execute(
            "UPDATE task_sources SET last_checked_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (now, error, now, source_id),
        )


def list_quotes(
    task_id: int | None = None,
    source_id: int | None = None,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Price observations, newest first. Narrow by task and/or source."""
    clauses: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(source_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM quotes{where} ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return _fetchall(db_path, sql, tuple(params))


def get_quote(quote_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    return _fetchone(db_path, "SELECT * FROM quotes WHERE id = ?", (quote_id,))


def create_event(task_id: int, quote_id: int | None, db_path: Path | None = None) -> int:
    with _transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO notification_events (task_id, quote_id, created_at, status) VALUES (?, ?, ?, ?)",
            (task_id, quote_id, _now(), "pending"),
        )
        return int(cursor.lastrowid)


def get_active_event(task_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    """Return the most recent unresolved event for a task.

    Includes pending, sent and failed events so the scheduler can honour the
    retry budget across restarts. A failed event is considered exhausted; the
    scheduler checks attempts against max_retries before attempting again.
    """
    return _fetchone(
        db_path,
        "SELECT * FROM notification_events WHERE task_id = ? AND status IN ('pending','sent','failed') ORDER BY id DESC LIMIT 1",
        (task_id,),
    )


def update_event(
    event_id: int,
    status: str,
    error: str | None = None,
    attempts: int | None = None,
    sent_at: str | None = None,
    db_path: Path | None = None,
) -> None:
    with _transaction(db_path) as conn:
        if attempts is not None:
            conn.execute(
                "UPDATE notification_events SET status = ?, last_error = ?, attempts = ?, sent_at = ? WHERE id = ?",
                (status, error, attempts, sent_at, event_id),
            )
        else:
            conn.execute(
                "UPDATE notification_events SET status = ?, last_error = ?, sent_at = ? WHERE id = ?",
                (status, error, sent_at, event_id),
            )


def reset_active_events(task_id: int, db_path: Path | None = None) -> int:
    """End the current deal episode for a task.

    A *verified* exit from the deal band ends every unresolved event of that
    episode (pending, sent **and** failed). This matters because a `failed`
    event would otherwise block a genuinely new deal forever. Incomparable
    observations must not call this function.

    Returns the number of events that were reset.
    """
    with _transaction(db_path) as conn:
        cursor = conn.execute(
            "UPDATE notification_events SET status = 'reset' WHERE task_id = ? AND status IN ('pending','sent','failed')",
            (task_id,),
        )
        return int(cursor.rowcount)


def try_begin_send(
    task_id: int,
    owner: str,
    db_path: Path | None = None,
    *,
    config_version: int | None = None,
    source_id: int | None = None,
) -> bool:
    """Atomically acquire the right to send one notification for a task.

    This is the shared boundary between "stop wins" and "send wins". A single
    conditional UPDATE decides it: the permit is granted only while the task is
    still `active` and no other send is already in flight. Because pause and stop
    update the same column in the same database, exactly one of the two orderings
    is observable, including across processes.

    `owner` identifies the permit holder so that only that holder (or an explicit
    human recovery step) can release it.

    Race guards for Stage C review fixes:

    - ``config_version``: pass the task version the check decided under. A threshold
      or comparison change bumps the version in the same transaction that ends the
      episode, so an in-flight check that decided under the *old* conditions is
      denied the permit and cannot send stale news.
    - ``source_id``: pass the source whose quote triggered the deal. The permit
      requires that this source is still enabled — disabling it mid-check must not
      produce an alert the user just switched off. The permit also requires at
      least one enabled source to remain at all.
    """
    now = _now()
    with _transaction(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE tasks
            SET send_in_flight = 1, last_send_started_at = :now, send_owner = :owner, updated_at = :now
            WHERE id = :id
              AND status = 'active'
              AND send_in_flight = 0
              AND (:version IS NULL OR config_version = :version)
              AND EXISTS (
                    SELECT 1 FROM task_sources s
                    WHERE s.task_id = tasks.id AND s.enabled = 1
              )
              AND (
                    :source IS NULL
                    OR EXISTS (
                        SELECT 1 FROM task_sources s2
                        WHERE s2.task_id = tasks.id AND s2.id = :source AND s2.enabled = 1
                    )
              )
            """,
            {"now": now, "owner": owner, "id": task_id,
             "version": config_version, "source": source_id},
        )
        return int(cursor.rowcount) == 1


def end_send(task_id: int, owner: str, db_path: Path | None = None) -> bool:
    """Release the send permit, but only if `owner` still holds it.

    Returns True when the permit was released. A stale holder whose permit was
    already recovered (or taken over) cannot release a newer permit, so an old
    worker cannot strand a fresh send.
    """
    with _transaction(db_path) as conn:
        cursor = conn.execute(
            "UPDATE tasks SET send_in_flight = 0, send_owner = NULL, updated_at = ? "
            "WHERE id = ? AND send_owner = ?",
            (_now(), task_id, owner),
        )
        return int(cursor.rowcount) == 1


def get_send_permit(task_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    """Return the current send permit state for a task, or None if the task is missing."""
    return _fetchone(
        db_path,
        "SELECT id, status, send_in_flight, send_owner, last_send_started_at FROM tasks WHERE id = ?",
        (task_id,),
    )


def clear_send_permit(task_id: int, db_path: Path | None = None) -> str:
    """Human recovery for a permit left behind by a process that died mid-send.

    Call this **only after every monitoring process has exited**: it cannot tell a
    truly abandoned permit from one whose sender is still alive. Returns:

    - ``"cleared"``        — a permit was present and has been released
    - ``"not_in_flight"``  — no permit was held; nothing to do
    - ``"missing"``        — no such task

    The task's status is deliberately not changed, so a stopped task does not
    regain the right to send through recovery.
    """
    with _transaction(db_path) as conn:
        row = conn.execute("SELECT send_in_flight FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return "missing"
        if not row["send_in_flight"]:
            return "not_in_flight"
        conn.execute(
            "UPDATE tasks SET send_in_flight = 0, send_owner = NULL, updated_at = ? WHERE id = ?",
            (_now(), task_id),
        )
        return "cleared"


def set_task_status_atomic(
    task_id: int,
    status: str,
    *,
    next_check_at: str | None = None,
    db_path: Path | None = None,
    block_on_send: bool = False,
) -> str:
    """Change a task's status, optionally refusing to race an in-flight send.

    Returns one of:

    - ``"changed"``   — status updated; no send was in flight
    - ``"in_flight"`` — a send had already started; the status is still updated
      (the user asked to stop) but the caller must report that an email may
      already be on its way
    - ``"missing"``   — no such task

    ``block_on_send`` is used by pause/stop. Resume does not block on it.
    """
    now = _now()
    with _transaction(db_path) as conn:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if exists is None:
            return "missing"

        outcome = "changed"
        if block_on_send:
            cursor = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND send_in_flight = 0",
                (status, now, task_id),
            )
            if int(cursor.rowcount) == 0:
                # The send won the race; still honour the stop request.
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, task_id),
                )
                outcome = "in_flight"
        else:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, task_id),
            )

        if next_check_at is not None:
            conn.execute(
                "UPDATE tasks SET next_check_at = ?, updated_at = ? WHERE id = ?",
                (next_check_at, now, task_id),
            )
        return outcome


def acquire_task_lock(
    task_id: int,
    owner: str,
    db_path: Path | None = None,
    stale_seconds: int = 900,
) -> bool:
    """Atomically claim a task for checking, across processes sharing this DB.

    SQLite `INSERT OR IGNORE` on a PRIMARY KEY is atomic, so exactly one caller
    wins. `owner` should include the normalised database path, process id and a
    unique token so lock ownership is auditable. Locks older than
    `stale_seconds` are treated as abandoned and reclaimed (a crashed process
    must not block a task forever).
    """
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
    with _transaction(db_path) as conn:
        conn.execute(
            "DELETE FROM task_locks WHERE task_id = ? AND acquired_at < ?",
            (task_id, cutoff),
        )
        cursor = conn.execute(
            "INSERT OR IGNORE INTO task_locks (task_id, owner, acquired_at) VALUES (?, ?, ?)",
            (task_id, owner, _now()),
        )
        return int(cursor.rowcount) == 1


def release_task_lock(task_id: int, owner: str, db_path: Path | None = None) -> None:
    with _transaction(db_path) as conn:
        conn.execute("DELETE FROM task_locks WHERE task_id = ? AND owner = ?", (task_id, owner))


def list_task_locks(db_path: Path | None = None) -> list[dict[str, Any]]:
    return _fetchall(db_path, "SELECT * FROM task_locks ORDER BY task_id")


def list_events(task_id: int | None = None, db_path: Path | None = None) -> list[dict[str, Any]]:
    if task_id is not None:
        return _fetchall(
            db_path,
            "SELECT * FROM notification_events WHERE task_id = ? ORDER BY id DESC",
            (task_id,),
        )
    return _fetchall(db_path, "SELECT * FROM notification_events ORDER BY id DESC")

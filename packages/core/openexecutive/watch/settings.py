"""Each watch job's settings (table ``watch_jobs``), its runs (``watch_runs``,
one row per job, day and slot, written by ``watch.runner``) and its days
(``watch_days``: the draft cards its runs left, counted in a transaction
across slots, the amount its match cards proposed, and its one failure
notice a day). ``watch_approved`` counts what each approver approved of a
job's amount-capped cards a day (bank matching's), one row per card.

One row per job: ``enabled`` (off until the principal turns it on), who
configured it last and when, ``settings`` (JSON, exactly the job's
``default_settings`` keys and types: ``registry.check_settings``) and an
optional ``reviewer_person_id``: the person a run's notices and draft cards
go to, but only when oe_connector says every read ran as the job's own ERP
user (``watch.cards.route``); the principal otherwise, with notices only.
Whoever configured it never approves its cards (separation of duties,
``integrations.erpnext_drafts``).

Only the principal changes a row, through ``PUT /watch/jobs/{id}``
(``api.routes.watch_jobs``). ``ERPNEXT_WATCH_JOBS_ENABLED`` is the operator's
master switch: off, no job runs whatever its row says.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openexecutive.watch import registry

logger = logging.getLogger(__name__)

JOBS_TABLE = "watch_jobs"
# watch_runs' definition, by its table name (the migration builds the new
# table under the same name).
_RUNS_DDL = """CREATE TABLE IF NOT EXISTS {table} (
        job TEXT NOT NULL,
        day TEXT NOT NULL,
        slot TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 1,
        cards INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        reads TEXT NOT NULL DEFAULT '[]',
        failure_noticed INTEGER NOT NULL DEFAULT 0,
        detail TEXT NOT NULL DEFAULT '{{}}',
        PRIMARY KEY (job, day, slot)
    )"""
RUNS_TABLE = "watch_runs"
DAYS_TABLE = "watch_days"
APPROVED_TABLE = "watch_approved"

_DDL = (
    f"""CREATE TABLE IF NOT EXISTS {JOBS_TABLE} (
        job TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        settings TEXT NOT NULL DEFAULT '{{}}',
        configured_by_person_id INTEGER,
        configured_at TEXT,
        reviewer_person_id INTEGER
    )""",
    # One row per job, day (the company's) and slot (its time that day, "HH:MM";
    # "" for a job that runs once a day): `running` from its claim, then
    # `done` or `failed`, or `skipped` when a later slot was claimed first.
    # `error` is a reason code, never ERPNext's text; `reads` records, per
    # read, whether oe_connector ran it as the job's own user
    # (`acting.as_job_user`), never that user's name.
    _RUNS_DDL.format(table=RUNS_TABLE),
    # One row per job and day: the draft cards its runs left that day (the
    # cap counts across slots, taken in a transaction), and whether its one
    # failure notice for the day was left.
    f"""CREATE TABLE IF NOT EXISTS {DAYS_TABLE} (
        job TEXT NOT NULL,
        day TEXT NOT NULL,
        cards INTEGER NOT NULL DEFAULT 0,
        failure_noticed INTEGER NOT NULL DEFAULT 0,
        amount REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (job, day)
    )""",
    # One row per amount-capped card approved (or claimed for approval: a
    # timeout still counts) on a day, the company's, by its approver.
    f"""CREATE TABLE IF NOT EXISTS {APPROVED_TABLE} (
        job TEXT NOT NULL,
        day TEXT NOT NULL,
        person_id INTEGER NOT NULL,
        decision_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        PRIMARY KEY (job, day, decision_id)
    )""",
)
# Columns added after the table first shipped: name -> its definition.
_RUN_COLUMNS = {"detail": "TEXT NOT NULL DEFAULT '{}'"}
_DAY_COLUMNS = {"amount": "REAL NOT NULL DEFAULT 0"}
# The columns a run row had before slots, copied as they are by the migration.
_PRE_SLOT_COLUMNS = (
    "job", "day", "status", "started_at", "finished_at", "attempts", "cards", "error", "reads",
    "failure_noticed", "detail",
)


def _migrate_runs(conn: sqlite3.Connection) -> None:
    """Move a ``watch_runs`` keyed by (job, day) to (job, day, slot): every
    old row keeps its values with slot "" (a once-a-day run). Under
    ``BEGIN IMMEDIATE`` and checked again inside, so two connections never
    both copy it. A day's failure notice already left is carried to
    ``watch_days`` too."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({RUNS_TABLE})")}
        if "slot" in have:
            conn.execute("COMMIT")
            return
        old = f"{RUNS_TABLE}_before_slots"
        conn.execute(f"ALTER TABLE {RUNS_TABLE} RENAME TO {old}")
        conn.execute(_RUNS_DDL.format(table=RUNS_TABLE))
        columns = ", ".join(c for c in _PRE_SLOT_COLUMNS if c in have)
        conn.execute(
            f"INSERT INTO {RUNS_TABLE} ({columns}, slot) SELECT {columns}, '' FROM {old}"  # noqa: S608
        )
        if "failure_noticed" in have:
            conn.execute(
                f"INSERT OR IGNORE INTO {DAYS_TABLE} (job, day, failure_noticed) "  # noqa: S608
                f"SELECT job, day, 1 FROM {old} WHERE failure_noticed = 1"
            )
        conn.execute(f"DROP TABLE {old}")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def db_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """A connection with both tables in place. ``isolation_level=None``: the
    caller opens its own transaction where it needs one (the runner's claim)."""
    conn = sqlite3.connect(str(db_path(path)), timeout=10, isolation_level=None)
    for statement in _DDL:
        conn.execute(statement)
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({RUNS_TABLE})")}
    if "slot" not in have:
        _migrate_runs(conn)
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({RUNS_TABLE})")}
    for column, definition in _RUN_COLUMNS.items():
        if column not in have:
            try:
                conn.execute(f"ALTER TABLE {RUNS_TABLE} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                # Another connection added it meanwhile.
                logger.debug("watch: column %s already added", column)
    days = {row[1] for row in conn.execute(f"PRAGMA table_info({DAYS_TABLE})")}
    for column, definition in _DAY_COLUMNS.items():
        if column not in days:
            try:
                conn.execute(f"ALTER TABLE {DAYS_TABLE} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                logger.debug("watch: column %s already added", column)
    return conn


def take_day_cards(job_id: str, day: str, n: int, cap: int, *, path: Path | None = None) -> int:
    """Take up to ``n`` of the job's ``cap`` draft cards for ``day``, read and
    counted in one ``BEGIN IMMEDIATE`` transaction so two workers (or two of
    the day's slots) never pass the cap between them. Returns how many were
    taken. A negative ``n`` gives that many back (a card that wasn't left
    after all), never below 0."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"INSERT OR IGNORE INTO {DAYS_TABLE} (job, day) VALUES (?, ?)", (job_id, day)  # noqa: S608
        )
        row = conn.execute(
            f"SELECT cards FROM {DAYS_TABLE} WHERE job = ? AND day = ?", (job_id, day)  # noqa: S608
        ).fetchone()
        used = int(row[0] or 0) if row else 0
        taken = max(-used, n) if n < 0 else max(0, min(n, cap - used))
        if taken:
            conn.execute(
                f"UPDATE {DAYS_TABLE} SET cards = cards + ? WHERE job = ? AND day = ?",  # noqa: S608
                (taken, job_id, day),
            )
        conn.execute("COMMIT")
        return taken
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def take_day_amount(job_id: str, day: str, amount: float, cap: float, *, path: Path | None = None) -> bool:
    """Take ``amount`` of the job's ``cap`` for ``day``'s proposed cards, read
    and counted in one ``BEGIN IMMEDIATE`` transaction, as ``take_day_cards``.
    Whether it was taken: never past the cap, so a card that would pass it
    isn't left. A negative ``amount`` gives that much back (a card not left
    after all), never below 0."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"INSERT OR IGNORE INTO {DAYS_TABLE} (job, day) VALUES (?, ?)", (job_id, day)  # noqa: S608
        )
        row = conn.execute(
            f"SELECT amount FROM {DAYS_TABLE} WHERE job = ? AND day = ?", (job_id, day)  # noqa: S608
        ).fetchone()
        used = float(row[0] or 0) if row else 0.0
        if amount >= 0 and used + amount > cap:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            f"UPDATE {DAYS_TABLE} SET amount = ? WHERE job = ? AND day = ?",  # noqa: S608
            (max(0.0, used + amount), job_id, day),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def take_approved_amount(
    job_id: str, day: str, person_id: int, decision_id: int, amount: float, cap: float, *, path: Path | None = None,
) -> bool:
    """Count one card's ``amount`` against what ``person_id`` approved of the
    job's cards on ``day``, read and written in one ``BEGIN IMMEDIATE``
    transaction: whether it fits ``cap`` with what they approved already.
    A card counted that day is counted once, however often it is approved
    again (a timeout, a refusal), and never given back: it counts from its
    claim. Fails closed: an error raises."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        held = conn.execute(
            f"SELECT 1 FROM {APPROVED_TABLE} WHERE job = ? AND day = ? AND decision_id = ?",  # noqa: S608
            (job_id, day, decision_id),
        ).fetchone()
        if held is not None:
            conn.execute("COMMIT")
            return True
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM {APPROVED_TABLE} WHERE job = ? AND day = ? AND person_id = ?",  # noqa: S608
            (job_id, day, person_id),
        ).fetchone()
        used = float(row[0] or 0) if row else 0.0
        if used + amount > cap:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            f"INSERT INTO {APPROVED_TABLE} (job, day, person_id, decision_id, amount) VALUES (?, ?, ?, ?, ?)",  # noqa: S608
            (job_id, day, person_id, decision_id, amount),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def day_cards(job_id: str, day: str, *, path: Path | None = None) -> int:
    """The draft cards the job's runs took for ``day`` so far."""
    conn = connect(path)
    try:
        row = conn.execute(
            f"SELECT cards FROM {DAYS_TABLE} WHERE job = ? AND day = ?", (job_id, day)  # noqa: S608
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row else 0


def take_failure_notice(job_id: str, day: str, *, path: Path | None = None) -> bool:
    """True once per job and calendar day, whichever of its slots asks first:
    whether this caller leaves the day's failure notice."""
    conn = connect(path)
    try:
        conn.execute(
            f"INSERT OR IGNORE INTO {DAYS_TABLE} (job, day) VALUES (?, ?)", (job_id, day)  # noqa: S608
        )
        cur = conn.execute(
            f"UPDATE {DAYS_TABLE} SET failure_noticed = 1 "  # noqa: S608
            "WHERE job = ? AND day = ? AND failure_noticed = 0",
            (job_id, day),
        )
        return cur.rowcount == 1
    finally:
        conn.close()


@dataclass(frozen=True)
class JobRow:
    job: str
    enabled: bool = False
    settings: dict[str, Any] = field(default_factory=dict)
    configured_by_person_id: int | None = None
    configured_at: str | None = None
    reviewer_person_id: int | None = None


def get_row(job_id: str, *, path: Path | None = None) -> JobRow:
    """The job's row; a job never configured is off, with no settings."""
    conn = connect(path)
    try:
        row = conn.execute(
            f"SELECT enabled, settings, configured_by_person_id, configured_at, reviewer_person_id "  # noqa: S608
            f"FROM {JOBS_TABLE} WHERE job = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return JobRow(job_id)
    try:
        stored = json.loads(row[1] or "{}")
    except ValueError:
        stored = {}
    return JobRow(
        job_id,
        enabled=bool(row[0]),
        settings=stored if isinstance(stored, dict) else {},
        configured_by_person_id=row[2],
        configured_at=row[3],
        reviewer_person_id=row[4],
    )


def effective_settings(job: registry.WatchJob, row: JobRow) -> dict[str, Any]:
    """The settings a run uses: the stored ones while they still fit the job,
    else its defaults (a job whose settings changed in code)."""
    if not row.settings:
        return dict(job.default_settings)
    try:
        return registry.check_settings(job, row.settings)
    except registry.SettingsError:
        logger.warning("watch: job %s's stored settings no longer fit it; using its defaults", job.id)
        return dict(job.default_settings)


def save(
    job: registry.WatchJob,
    *,
    enabled: bool | None,
    settings: dict[str, Any] | None,
    reviewer_person_id: int | None,
    set_reviewer: bool,
    configured_by_person_id: int,
    now: datetime,
    path: Path | None = None,
) -> tuple[JobRow, JobRow]:
    """Write the job's row; returns it before and after. ``settings`` must
    already be ``registry.check_settings``'s."""
    before = get_row(job.id, path=path)
    after = JobRow(
        job.id,
        enabled=before.enabled if enabled is None else enabled,
        settings=dict(before.settings) if settings is None else dict(settings),
        configured_by_person_id=configured_by_person_id,
        configured_at=now.isoformat(),
        reviewer_person_id=reviewer_person_id if set_reviewer else before.reviewer_person_id,
    )
    conn = connect(path)
    try:
        conn.execute(
            f"INSERT INTO {JOBS_TABLE} "  # noqa: S608
            "(job, enabled, settings, configured_by_person_id, configured_at, reviewer_person_id) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job) DO UPDATE SET enabled = excluded.enabled, settings = excluded.settings, "
            "configured_by_person_id = excluded.configured_by_person_id, "
            "configured_at = excluded.configured_at, reviewer_person_id = excluded.reviewer_person_id",
            (
                job.id,
                1 if after.enabled else 0,
                json.dumps(after.settings, sort_keys=True),
                after.configured_by_person_id,
                after.configured_at,
                after.reviewer_person_id,
            ),
        )
    finally:
        conn.close()
    return before, after


def master_on() -> bool:
    """``ERPNEXT_WATCH_JOBS_ENABLED``: the operator's switch over every job."""
    from openexecutive.config import get_settings

    return bool(get_settings().erpnext_watch_jobs_enabled)


def job_enabled(job_id: str, *, path: Path | None = None) -> bool:
    """Whether ``job_id`` may run now: registered, the master switch on and
    its row on. Fails closed: a read error is off."""
    if registry.get(job_id) is None or not master_on():
        return False
    try:
        return get_row(job_id, path=path).enabled
    except Exception:
        logger.warning("watch: couldn't read job %s's row; treating it as off", job_id, exc_info=True)
        return False

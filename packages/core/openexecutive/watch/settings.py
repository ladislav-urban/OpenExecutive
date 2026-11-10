"""Each watch job's settings (table ``watch_jobs``), and its runs
(``watch_runs``, written by ``watch.runner``).

One row per job: ``enabled`` (off until the principal turns it on), who
configured it last and when, ``settings`` (JSON, exactly the job's
``default_settings`` keys and types: ``registry.check_settings``) and an
optional ``reviewer_person_id``, stored for phase 1's separation of duties.
The runner never reads the reviewer: every notice goes to the principal.

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
RUNS_TABLE = "watch_runs"

_DDL = (
    f"""CREATE TABLE IF NOT EXISTS {JOBS_TABLE} (
        job TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        settings TEXT NOT NULL DEFAULT '{{}}',
        configured_by_person_id INTEGER,
        configured_at TEXT,
        reviewer_person_id INTEGER
    )""",
    # One row per job and day (the company's): `running` from its claim, then
    # `done` or `failed`. `error` is a reason code, never ERPNext's text;
    # `reads` records, per read, whether oe_connector ran it as the job's own
    # user (`acting.as_job_user`), never that user's name.
    f"""CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
        job TEXT NOT NULL,
        day TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 1,
        cards INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        reads TEXT NOT NULL DEFAULT '[]',
        failure_noticed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (job, day)
    )""",
)


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
    return conn


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

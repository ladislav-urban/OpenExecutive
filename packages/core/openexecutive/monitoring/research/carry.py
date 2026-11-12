"""Keep the last research run across a demo reset.

The demo install resets its company every night (``cli/demo_reset.py``), and
a fixture load clears the research scan's run history so a freshly seeded
company gets findings. On the demo that made the 6-hourly scan cold-start
every morning: a full 7-specialist research pass a day on the same company.

A reset of the *same* company now keeps the last periodic run instead:

- ``snapshot_last_research`` runs before the reset wipes anything. It reads
  the latest ``watchlist_research_periodic_ran`` row, the alerts the alert
  pipeline stamped with that run's id (``RUN_TAG_PREFIX``), and the watch rows
  the watch policy stamped with it (``config_json._policy.run_id``).
- ``restore_research`` runs after the wipe, in one transaction: alerts (column
  for column, original ids, ``INSERT OR IGNORE`` so a bundle row on the same
  ``(source, external_id)`` wins), then watch rows, re-baselined
  (``baselined_at`` / ``last_polled_at`` NULL, so no signal is carried and the
  first poll starts fresh). Then one ``ran`` row with ``trigger:
  "demo_reset_carry"``, the original run's time in ``details.run_ts`` and the
  post-reset fingerprint, so the next tick skips and the staleness floor counts
  from the real run.

It carries only when at least one of the run's alerts is live (and one landed
after the restore): otherwise it rolls back and the caller clears the history
as before, so the next tick cold-starts. A failure anywhere does the same. It
fails toward a run, never toward a blank demo. Nothing here calls a model or
the network.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.alerts.models import RESEARCH_RUN_TAG_PREFIX as RUN_TAG_PREFIX
from openexecutive.alerts.models import is_run_tag, run_tag

logger = logging.getLogger(__name__)

CARRY_TRIGGER = "demo_reset_carry"
EVENT_CARRIED = "watchlist_research_carried"

# The fictional roster (``cli/demo_reset.FICTIONAL_PEOPLE``) is re-seeded with
# new ids; anyone else keeps theirs.
_PERSON_COLUMNS_ALERT = ("routed_to_person_id", "owner_person_id")
_PERSON_COLUMNS_WATCH = ("route_to_person_id",)


@dataclass
class ResearchCarry:
    run_id: str
    run_ts: str
    findings: int
    alerts: list[dict[str, Any]] = field(default_factory=list)
    watch_rows: list[dict[str, Any]] = field(default_factory=list)
    # person id -> full name, for every routed id in the rows above.
    names: dict[int, str] = field(default_factory=dict)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]  # noqa: S608 — fixed names


def _live(row: dict[str, Any], now: datetime) -> bool:
    """``alerts/lifecycle.is_live`` on a raw row, and not archived."""
    from openexecutive.alerts.lifecycle import is_live
    from openexecutive.alerts.store import _row_to_alert

    if row.get("archived_at"):
        return False
    try:
        # The store's own reader knows every JSON column the table has.
        return is_live(_row_to_alert(row), now)  # type: ignore[arg-type]
    except Exception:
        logger.exception("research.carry: could not read alert %s", row.get("id"))
        return False


def _latest_ran_row() -> tuple[str, str, int] | None:
    """(run_id, run_ts, findings) of the newest ``ran`` row: the real run's
    time even when that row is itself a carry, so chained resets never move
    the staleness floor."""
    from openexecutive.audit import get_audit_logger
    from openexecutive.monitoring.research.scheduler import EVENT_RAN

    rows = get_audit_logger().query(event_type=EVENT_RAN, limit=1)
    if not rows:
        return None
    details = rows[0].details or {}
    run_id = details.get("run_id")
    run_ts = details.get("run_ts") or getattr(rows[0], "ts", None)
    if not isinstance(run_id, str) or not run_id or not run_ts:
        return None
    findings = details.get("findings")
    return run_id, str(run_ts), int(findings) if isinstance(findings, int) else 0


def snapshot_last_research(db_path: Path, *, now: datetime | None = None) -> ResearchCarry | None:
    """What the last periodic run left, or ``None`` when there is nothing to
    carry: no run, or none of its alerts still live."""
    now = now or datetime.now(UTC)
    latest = _latest_ran_row()
    if latest is None or not db_path.exists():
        return None
    run_id, run_ts, findings = latest
    tag = run_tag(run_id)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = _tables(conn)
        if "alerts" not in tables:
            return None
        alerts = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM alerts WHERE topic_tags LIKE ? ESCAPE '\\'",
                (f'%"{_like_escape(tag)}"%',),
            )
            if tag in json.loads(r["topic_tags"] or "[]")
        ]
        if not any(_live(a, now) for a in alerts):
            return None
        watch_rows: list[dict[str, Any]] = []
        if "watchlist" in tables:
            watch_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM watchlist WHERE json_extract(config_json, '$._policy.run_id') = ?",
                    (run_id,),
                )
            ]
        ids = {
            int(row[col])
            for rows, cols in ((alerts, _PERSON_COLUMNS_ALERT), (watch_rows, _PERSON_COLUMNS_WATCH))
            for row in rows
            for col in cols
            if row.get(col) is not None
        }
        names: dict[int, str] = {}
        if ids and "people" in tables:
            marks = ",".join("?" * len(ids))
            names = {
                int(r["id"]): str(r["full_name"])
                for r in conn.execute(f"SELECT id, full_name FROM people WHERE id IN ({marks})", sorted(ids))  # noqa: S608 — placeholders only
            }
    finally:
        conn.close()
    return ResearchCarry(
        run_id=run_id, run_ts=run_ts, findings=findings, alerts=alerts, watch_rows=watch_rows, names=names,
    )


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_person(
    person_id: Any, carry: ResearchCarry, existing: set[int], name_to_id: dict[str, int],
) -> int | None:
    """The same person after the reset: a re-seeded fictional one by the
    bundle's own name map (never a global name lookup), a kept one by id;
    anyone gone is NULL, so the principal owns the row."""
    if person_id is None:
        return None
    pid = int(person_id)
    name = carry.names.get(pid)
    if name is not None and name.strip() in name_to_id:
        return name_to_id[name.strip()]
    return pid if pid in existing else None


def restore_research(
    carry: ResearchCarry,
    db_path: Path,
    *,
    name_to_id: dict[str, int] | None = None,
    now: datetime | None = None,
) -> dict[str, int] | None:
    """Put the carried run back after the wipe. Returns counts, or ``None``
    when nothing was carried (the caller then clears the run history)."""
    from openexecutive.audit import get_audit_logger
    from openexecutive.monitoring.research.scheduler import (
        EVENT_RAN,
        clear_research_run_history,
        compute_research_state_hash,
    )

    now = now or datetime.now(UTC)
    names = name_to_id or {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    alert_ids: list[int] = []
    watch_ids: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = _tables(conn)
        existing = {int(r[0]) for r in conn.execute("SELECT id FROM people")} if "people" in tables else set()
        alert_cols = set(_columns(conn, "alerts"))
        live_landed = 0
        carried_ids = {int(row["id"]) for row in carry.alerts}
        for row in carry.alerts:
            values = {k: v for k, v in row.items() if k in alert_cols}
            # Never point at an alert the reset replaced and didn't carry.
            superseded = values.get("superseded_by_alert_id")
            if superseded is not None and int(superseded) not in carried_ids:
                values["superseded_by_alert_id"] = None
            for col in _PERSON_COLUMNS_ALERT:
                if col in values:
                    values[col] = _resolve_person(values[col], carry, existing, names)
            cols = list(values)
            cur = conn.execute(
                f"INSERT OR IGNORE INTO alerts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",  # noqa: S608 — column names from the table itself
                [values[c] for c in cols],
            )
            if cur.rowcount == 1:
                alert_ids.append(int(values["id"]))
                live_landed += _live(values, now)
        if not live_landed:
            conn.rollback()
            return None
        # Their approvals and held actions point at scheduled actions the
        # reset replaced: a carried card starts clean.
        for table in ("alert_held_actions", "alert_approvals"):
            if table in tables:
                marks = ",".join("?" * len(alert_ids))
                conn.execute(f"DELETE FROM {table} WHERE alert_id IN ({marks})", alert_ids)  # noqa: S608 — fixed names, placeholders
        if carry.watch_rows and "watchlist" in tables:
            watch_cols = set(_columns(conn, "watchlist"))
            for row in carry.watch_rows:
                values = {k: v for k, v in row.items() if k in watch_cols}
                values["baselined_at"] = None
                values["last_polled_at"] = None
                for col in _PERSON_COLUMNS_WATCH:
                    if col in values:
                        values[col] = _resolve_person(values[col], carry, existing, names)
                cols = list(values)
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO watchlist ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",  # noqa: S608 — column names from the table itself
                    [values[c] for c in cols],
                )
                if cur.rowcount == 1:
                    watch_ids.append(int(values["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        clear_research_run_history(db_path)
        state_hash = compute_research_state_hash(db_path=db_path)
        row_id = get_audit_logger().log(
            EVENT_RAN,
            f"Executive research carried over the demo reset — {carry.findings} finding(s)",
            actor="cli",
            details={
                "state_hash": state_hash,
                "trigger": CARRY_TRIGGER,
                "run_id": carry.run_id,
                "run_ts": carry.run_ts,
                "findings": carry.findings,
                "carried": True,
            },
        )
        if row_id is None:
            raise RuntimeError("the carry's run row was not written")
    except Exception:
        _undo(db_path, alert_ids, watch_ids)
        raise
    counts = {"alerts": len(alert_ids), "watch_rows": len(watch_ids), "live_alerts": live_landed}
    try:
        get_audit_logger().log(
            EVENT_CARRIED,
            f"Kept the last research run across the demo reset ({len(alert_ids)} alert(s))",
            actor="cli",
            details=counts,
        )
    except Exception:
        logger.exception("research.carry: audit row failed")
    return counts


def _undo(db_path: Path, alert_ids: list[int], watch_ids: list[int]) -> None:
    """Take the restored rows out again when the run row can't be written,
    so the reset clears as before and the next tick cold-starts."""
    conn = sqlite3.connect(str(db_path))
    try:
        for table, ids in (("alerts", alert_ids), ("watchlist", watch_ids)):
            if ids:
                marks = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM {table} WHERE id IN ({marks})", ids)  # noqa: S608 — fixed names, placeholders
        conn.commit()
    finally:
        conn.close()


__all__ = [
    "CARRY_TRIGGER",
    "EVENT_CARRIED",
    "RUN_TAG_PREFIX",
    "ResearchCarry",
    "is_run_tag",
    "restore_research",
    "run_tag",
    "snapshot_last_research",
]

"""What moved: the records behind an ERPNext alert, shown on its card.

ERPNext's finance extras (oe_connector's finance extras plan, A.2.1). When a
rule fires, oe_connector may send the records behind it with the event, as
``evidence``: at most ``MAX_TABLES`` tables, each a heading, a currency or
none, at most ``MAX_COLUMNS`` typed columns and at most ``MAX_ROWS`` rows.
The shape is fixed on both sides by a shared vector
(``tests/fixtures/erpnext/alert_evidence_vector.json``, a byte copy of
oe_connector's ``tests/fixtures/openexecutive/alert_evidence_vector.json``).

**Never a model's.** Evidence doesn't reach triage or any model call. It is
kept by the event's ref, as ``erpnext_alert_drafts`` keeps a suggestion, and
read back only to show it: Home's card renders every cell as text. Its
strings are ERPNext's data, partly written by outsiders (a party's name), so
they stay data.

**All or nothing.** Evidence that isn't in the shape, is over a cap, or
holds text that isn't plain (control or direction characters, a lone
surrogate; runs of whitespace are folded to one space) is dropped whole and the event is kept without it
(``parse_evidence`` returns None and never raises).

**Which alert it belongs to.** Evidence is kept for exactly the readers its
event's alert has: its ref and the people it names (``named_person_ids``,
the ``:named:<ids>`` suffix the pipeline gives a different set of readers).
So an alert never shows figures the site sent for other readers, and an
event that comes without evidence, or with evidence not in the shape, clears
what those readers' alert had. The month plays no part: next month's event
for the same readers may be folded into this month's open alert, and its
evidence must replace this month's. ``evidence_for``
reads it only for the alert the event became, as
``erpnext_alert_drafts.event_ref`` decides it (private, source ``erpnext``,
``external_id`` ``<ref>:<YYYY-MM>``), and only while it is younger than
``KEEP_FOR``. A demo bundle's alerts aren't private, so they never show any.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import unicodedata
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.memory import episodic

logger = logging.getLogger(__name__)

MAX_TABLES = 3
MAX_COLUMNS = 6
MAX_ROWS = 10
MAX_HEADING = 120
MAX_LABEL = 40
MAX_KEY = 40
MAX_CELL = 140
TYPES = frozenset({"text", "money", "number", "percent", "date", "doc"})
# A figure above this isn't one ERPNext would send.
NUMBER_MAX = 1e15
# The most one alert's evidence may take when stored (its JSON).
# Above the largest well-formed shape in plain text (about 59 KB).
MAX_STORED_BYTES = 64 * 1024
# oe_connector resends a still-matching alert monthly, as with suggestions.
KEEP_FOR = timedelta(days=62)

_KEY_RE = re.compile(rf"[a-z][a-z0-9_]{{0,{MAX_KEY - 1}}}")
# Keys a JavaScript object already has; a row is read by key on the page.
_RESERVED_KEYS = frozenset({"constructor", "prototype", "hasownproperty", "tostring", "valueof"})
# Characters that reorder or hide text, never part of ERPNext's data.
_DIRECTION = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMERIC = frozenset({"money", "number", "percent"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS erpnext_alert_evidence (
    alert       TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS erpnext_alert_evidence_received ON erpnext_alert_evidence (received_at);
"""


class _Bad(ValueError):
    """Evidence not in the shape: dropped whole."""


def _db_path(db_path: Path | None) -> Path:
    # Read at call time so a test's patch of episodic.DB_PATH takes effect.
    return db_path if db_path is not None else episodic.DB_PATH


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(db_path))
    conn.executescript(_SCHEMA)
    return conn


def _string(value: Any, cap: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > cap:
        raise _Bad("string")
    # A tab or a line break in ERPNext's text is shown as a space.
    value = " ".join(value.split()) if any(c.isspace() for c in value) else value
    for char in value:
        if char in _DIRECTION or unicodedata.category(char) in ("Cc", "Cs"):
            raise _Bad("string")
    shown = any(not char.isspace() and unicodedata.category(char) != "Cf" for char in value)
    if not empty and not shown:
        raise _Bad("string")
    return value


def _cell(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind in _NUMERIC:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _Bad("number")
        # The range first: a huge int has no float, so isfinite would raise.
        if isinstance(value, int):
            if abs(value) > NUMBER_MAX:
                raise _Bad("number")
        elif not math.isfinite(value) or abs(value) > NUMBER_MAX:
            raise _Bad("number")
        return value
    if kind == "date":
        if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
            raise _Bad("date")
        try:
            date.fromisoformat(value)
        except ValueError as bad:
            raise _Bad("date") from bad
        return value
    if kind == "doc":
        if not isinstance(value, dict) or set(value) != {"doctype", "name"}:
            raise _Bad("doc")
        return {"doctype": _string(value["doctype"], MAX_CELL), "name": _string(value["name"], MAX_CELL)}
    return _string(value, MAX_CELL, empty=True)


def _table(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"heading", "currency", "columns", "rows"}:
        raise _Bad("table")
    currency = raw["currency"]
    if currency is not None and (not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency)):
        raise _Bad("currency")
    columns_raw, rows_raw = raw["columns"], raw["rows"]
    if not isinstance(columns_raw, list) or not 1 <= len(columns_raw) <= MAX_COLUMNS:
        raise _Bad("columns")
    if not isinstance(rows_raw, list) or len(rows_raw) > MAX_ROWS:
        raise _Bad("rows")
    columns: list[dict[str, str]] = []
    for column in columns_raw:
        if not isinstance(column, dict) or set(column) != {"key", "label", "type"}:
            raise _Bad("column")
        key, kind = column["key"], column["type"]
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key) or key in _RESERVED_KEYS:
            raise _Bad("column")
        if not isinstance(kind, str) or kind not in TYPES:
            raise _Bad("column")
        if any(c["key"] == key for c in columns):
            raise _Bad("column")
        columns.append({"key": key, "label": _string(column["label"], MAX_LABEL), "type": kind})
    keys = {c["key"] for c in columns}
    rows: list[dict[str, Any]] = []
    for row in rows_raw:
        if not isinstance(row, dict) or not set(row) <= keys:
            raise _Bad("row")
        rows.append({c["key"]: _cell(c["type"], row.get(c["key"])) for c in columns})
    return {"heading": _string(raw["heading"], MAX_HEADING), "currency": currency, "columns": columns, "rows": rows}


def parse_evidence(raw: Any) -> list[dict[str, Any]] | None:
    """``raw`` as it will be kept and shown, or None when it isn't evidence
    in the shape or is over a cap: then the event goes without it."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TABLES:
        return None
    try:
        tables = [_table(table) for table in raw]
        stored = json.dumps(tables)
    except Exception:
        # _Bad, or anything else a hostile shape could raise: dropped whole.
        return None
    return tables if len(stored.encode("utf-8")) <= MAX_STORED_BYTES else None


def alert_identity(ref: str, readers: list[int]) -> str:
    """The alert an event becomes: its ref and its named readers, as the
    pipeline keys them (sorted ids; none for everyone else's alert)."""
    named = ",".join(str(i) for i in sorted(set(readers)))
    return f"{ref}#{named}"


def remember(
    kind: str, key: str, raw: Any, *, readers: list[int], db_path: Path | None = None
) -> bool:
    """Keep the evidence an event carried for the alert it becomes, or clear
    what that alert had when it carried none in the shape; whether some was
    kept. Never raises: an alert is taken whatever happens here."""
    evidence = parse_evidence(raw) if raw is not None else None
    if raw is not None and evidence is None:
        logger.info("erpnext_evidence: dropped evidence not in the shape kind=%s", kind)
    identity = alert_identity(f"erpnext:{kind}:{key}", readers)
    now = datetime.now(UTC)
    try:
        with _connect(db_path) as conn:
            if evidence is None:
                conn.execute("DELETE FROM erpnext_alert_evidence WHERE alert = ?", (identity,))
            else:
                conn.execute(
                    "INSERT INTO erpnext_alert_evidence (alert, kind, evidence, received_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(alert) DO UPDATE SET kind = excluded.kind, evidence = excluded.evidence, "
                    "received_at = excluded.received_at",
                    (identity, kind, json.dumps(evidence), now.isoformat()),
                )
            conn.execute(
                "DELETE FROM erpnext_alert_evidence WHERE received_at < ?", ((now - KEEP_FOR).isoformat(),)
            )
    except sqlite3.Error:
        logger.warning("erpnext_evidence: couldn't keep evidence", exc_info=True)
        return False
    return evidence is not None


def evidence_for(alert: Any, *, db_path: Path | None = None) -> list[dict[str, Any]] | None:
    """The evidence ``alert``'s event carried, read back and checked again,
    or None."""
    from openexecutive.integrations.erpnext_alert_drafts import event_ref
    from openexecutive.integrations.erpnext_events import KINDS

    found = event_ref(alert, KINDS)
    if found is None:
        return None
    ref, kind, _key = found
    # The readers the pipeline named in its key, if any.
    suffix = (getattr(alert, "dedup_key", "") or "")[len(f"private:{ref}"):]
    readers: list[int] = []
    if suffix:
        if not suffix.startswith(":named:"):
            return None
        try:
            readers = [int(i) for i in suffix[len(":named:"):].split(",")]
        except ValueError:
            return None
    identity = alert_identity(ref, readers)
    oldest = (datetime.now(UTC) - KEEP_FOR).isoformat()
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT kind, evidence FROM erpnext_alert_evidence WHERE alert = ? AND received_at >= ?",
                (identity, oldest),
            ).fetchone()
    except sqlite3.Error:
        logger.warning("erpnext_evidence: couldn't read evidence", exc_info=True)
        return None
    if row is None or row[0] != kind:
        return None
    try:
        parsed = json.loads(row[1])
    except ValueError:
        return None
    return parse_evidence(parsed)

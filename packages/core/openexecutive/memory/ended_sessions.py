"""Sign-in sessions the identity provider ended (OpenID Connect Back-Channel
Logout).

The web app takes the provider's logout token, checks it
(``packages/ui/src/lib/backchannel.ts``) and records here what ended: a
provider session (``sid``), or every session of a person (``sub``) that
began before ``ended_at``. Every server of the web app reads the list back
(``GET /auth/ended-sessions``) and signs those sessions out on their next
request (docs/auth.md, "Signing out"). A token's ``jti`` is taken once, so a
replayed token is refused across servers too.

Kept for ``KEEP_SECONDS``, longer than any web session lasts.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

TABLE = "ended_sessions"
# Longer than the web app's 24-hour session.
KEEP_SECONDS = 2 * 24 * 60 * 60

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iss TEXT NOT NULL,
    sid TEXT,
    sub TEXT,
    ended_at REAL NOT NULL,
    jti TEXT NOT NULL UNIQUE,
    recorded_at REAL NOT NULL
)
"""


def _db(db_path: Path | None) -> Path:
    # Resolved at call time, so a test that points episodic.DB_PATH elsewhere
    # (and an EPISODIC_DB_PATH deployment) uses the same file.
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_SQL)
    return conn


def record(
    *, iss: str, sid: str | None, sub: str | None, ended_at: float, jti: str, db_path: Path | None = None,
) -> bool:
    """Record one ended session. False when this ``jti`` was recorded
    already (a replayed token). Prunes what is past ``KEEP_SECONDS``."""
    now = time.time()
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(f"DELETE FROM {TABLE} WHERE recorded_at < ?", (now - KEEP_SECONDS,))
            try:
                conn.execute(
                    f"INSERT INTO {TABLE} (iss, sid, sub, ended_at, jti, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (iss, sid, sub, ended_at, jti, now),
                )
            except sqlite3.IntegrityError:
                return False
    finally:
        conn.close()
    return True


def recent(db_path: Path | None = None) -> list[dict[str, Any]]:
    """What ended in the last ``KEEP_SECONDS``: ``iss``, ``sid``, ``sub``, ``ended_at``."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT iss, sid, sub, ended_at FROM {TABLE} WHERE recorded_at >= ? ORDER BY id",
            (time.time() - KEEP_SECONDS,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]

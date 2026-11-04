"""Chat stamps are UTC with an offset (the reshoot on the pilot, Nov 4, 2026).

``Session.created_at`` was naive (``datetime.utcnow()``), and is the first
turn's ``created_at`` and ``updated_at``; ``update_session_timestamp`` wrote
"+00:00". A browser east of UTC read the offset-less stamp as local time, so a
one-turn conversation looked hours older than the others, and ERPNext's panel
went on with the wrong conversation.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from openexecutive.api.models import SessionSummary
from openexecutive.memory import episodic, session_store
from openexecutive.orchestrator.session import Session


def test_a_new_sessions_stamp_is_aware_utc() -> None:
    created = Session().created_at
    assert created.tzinfo is not None and created.utcoffset() == UTC.utcoffset(None)
    assert created.isoformat().endswith("+00:00")


def test_a_first_turns_stamps_carry_the_offset(tmp_path: Path) -> None:
    db = tmp_path / "chats.db"
    episodic.initialize_db(db)
    session = Session()
    session_store.create_session(session.session_id, "t", session.created_at.isoformat(), db_path=db)
    with sqlite3.connect(db) as conn:
        created, updated = conn.execute(
            "SELECT created_at, updated_at FROM sessions WHERE session_id = ?", (session.session_id,)
        ).fetchone()
    for stamp in (created, updated):
        assert datetime.fromisoformat(stamp).tzinfo is not None, stamp


def test_a_stored_stamp_without_an_offset_is_returned_as_utc() -> None:
    row = SessionSummary(
        session_id="s",
        title="t",
        created_at="2026-11-04T10:04:01.123456",
        updated_at="2026-11-04T10:04:01.123456",
        pinned_at="2026-11-04T10:05:00",
        shared_at="2026-11-04T10:06:00",
    )
    assert row.created_at == "2026-11-04T10:04:01.123456+00:00"
    assert row.updated_at == "2026-11-04T10:04:01.123456+00:00"
    assert row.pinned_at == "2026-11-04T10:05:00+00:00"
    assert row.shared_at == "2026-11-04T10:06:00+00:00"


def test_stamps_with_an_offset_or_none_are_kept() -> None:
    row = SessionSummary(
        session_id="s",
        title="t",
        created_at="2026-11-04T10:04:01+00:00",
        updated_at="2026-11-04T11:04:01+01:00",
    )
    assert row.created_at == "2026-11-04T10:04:01+00:00"
    assert row.updated_at == "2026-11-04T11:04:01+01:00"
    assert row.pinned_at is None and row.shared_at is None
    odd = SessionSummary(session_id="s", title="t", created_at="", updated_at="not a date")
    assert (odd.created_at, odd.updated_at) == ("", "not a date")

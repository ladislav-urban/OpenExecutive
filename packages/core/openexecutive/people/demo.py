"""Demo people: what is held back for someone trying the product.

A demo person (``Person.demo``) is a team member the directory sync marks
because they are also in ``DIRECTORY_SYNC_DEMO_GROUP``
(``integrations.directory_sync``). They are on the roster to see what the
Executive can do on demo data, so:

- their chat turns only read and advise (``demo_turn``; the same narrow tool
  list as ERPNext's panel, ``schedule_tools.ERPNEXT_PANEL_TOOLS``);
- they never approve for anyone (``people.store.find_approvers``);
- the Executive sends them nothing outside their own chat
  (``orchestrator.outbound_guard``);
- they may ask ``DEMO_DAILY_QUESTIONS`` questions a day, across the web chat
  and ERPNext's panel (``take_question``), so one registrant can't run up the
  AI bill.

docs/auth.md, "Demo people", has the whole picture.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "demo_questions"

LIMIT_REACHED = (
    "You've asked today's {limit} questions for this demo. "
    "Come back tomorrow, or ask the person who invited you for more."
)


def is_demo_person(person_id: int | None) -> bool:
    """Whether ``person_id`` is a demo person. Fails closed: a roster that
    can't be read answers yes, so a demo turn is never given more."""
    if person_id is None:
        return False
    try:
        from openexecutive.people.store import get_person

        person = get_person(person_id)
    except Exception:
        logger.warning("demo: roster read failed for person %s; treating as demo", person_id, exc_info=True)
        return True
    return person is not None and person.demo


def demo_turn(session: Any) -> bool:
    """Whether ``session`` is a demo person's turn (``Session.demo_caller``)."""
    return session is not None and getattr(session, "demo_caller", False) is True


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def take_question(person_id: int, limit: int, *, day: str | None = None, db_path: Path | None = None) -> bool:
    """Count one question for a demo person today (UTC); False, and nothing
    counted, once they've had ``limit``. ``limit`` 0 means no limit. Fails
    closed: a count that can't be written refuses the question."""
    if limit <= 0:
        return True
    from openexecutive.people.store import _get_conn

    try:
        with _get_conn(db_path) as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {TABLE} ("
                " person_id INTEGER NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,"
                " PRIMARY KEY (person_id, day))"
            )
            # One statement, so two questions at once can't both take the last one.
            taken = conn.execute(
                f"INSERT INTO {TABLE} (person_id, day, count) VALUES (?, ?, 1)"
                " ON CONFLICT (person_id, day) DO UPDATE SET count = count + 1 WHERE count < ?",
                (person_id, day or _today(), limit),
            ).rowcount
    except Exception:
        logger.warning("demo: question count failed for person %s; refusing", person_id, exc_info=True)
        return False
    return taken > 0


def questions_today(person_id: int, *, day: str | None = None, db_path: Path | None = None) -> int:
    """How many questions a demo person has asked today (UTC)."""
    from openexecutive.people.store import _get_conn

    try:
        with _get_conn(db_path) as conn:
            row = conn.execute(
                f"SELECT count FROM {TABLE} WHERE person_id = ? AND day = ?", (person_id, day or _today())
            ).fetchone()
    except Exception:
        return 0
    return int(row["count"]) if row is not None else 0


def limit_message(limit: int) -> str:
    return LIMIT_REACHED.format(limit=limit)

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
  AI bill; and all of them together ``DEMO_DAILY_QUESTIONS_TOTAL`` (``admit``),
  so many registrants can't either. The first refusal of a day at that cap is
  audited and tells the principal.
- they never see another demo person, nor anyone's email or chat ids but
  their own (``shown_to_demo_viewer``; the org prompt block's demo variant),
  so one prospect never learns who else is trying the product or how to
  reach the team;
- they never see the principal's name, only their role (``PRINCIPAL_LABEL``,
  ``by_role``): the install's owner is a real person, the demo company is not
  (owner's decision, Oct 30, 2026).

docs/auth.md, "Demo people", has the whole picture.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

TABLE = "demo_questions"

LIMIT_REACHED = (
    "You've asked today's {limit} questions for this demo. "
    "Come back tomorrow, or ask the person who invited you for more."
)

# What a demo person reads once every demo person together has asked the
# day's DEMO_DAILY_QUESTIONS_TOTAL.
SITE_LIMIT_REACHED = "The demo is busy today. Try again tomorrow."

# Rows of TABLE that are no person's (person ids start at 1): the day's
# questions from every demo person together, and the day's refusals at that cap.
SITE_ROW = 0
SITE_REFUSED_ROW = -1


def demo_state(person_id: int | None) -> bool | None:
    """Whether ``person_id`` is a demo person; None when the roster can't be
    read (the caller decides how to fail closed)."""
    if person_id is None:
        return False
    try:
        from openexecutive.people.store import get_person

        person = get_person(person_id)
    except Exception:
        logger.warning("demo: roster read failed for person %s", person_id, exc_info=True)
        return None
    return person is not None and person.demo


def is_demo_person(person_id: int | None) -> bool:
    """Whether ``person_id`` is a demo person. Fails closed: a roster that
    can't be read answers yes, so a demo turn is never given more."""
    return demo_state(person_id) is not False


# What reaches a person: hidden from a demo viewer on everyone but themselves.
_REACH_FIELDS: dict[str, Any] = {
    "email": None,
    "email_aliases": [],
    "slack_user_id": None,
    "telegram_chat_id": None,
    "discord_user_id": None,
}


# What a demo viewer reads in place of the principal's name.
PRINCIPAL_LABEL = "The principal"


def by_role(person: Any) -> Any:
    """``person`` with the principal's name replaced by ``PRINCIPAL_LABEL``;
    anyone else as they are."""
    if getattr(person, "is_principal", False) is not True:
        return person
    return person.model_copy(update={"full_name": PRINCIPAL_LABEL})


# What a demo turn's tool result says when the principal's name couldn't be
# checked (the roster unread): the result is withheld, never sent unchecked.
DEMO_PRINCIPAL_WITHHELD = "That result isn't available just now. Answer from what you already have."


def principal_name_pattern(
    names: list[str] | None = None, addresses: list[str] | None = None
) -> re.Pattern[str] | None:
    """A whole-word pattern, any case, for each principal's full name, first
    name and email addresses (``names`` and ``addresses``, else the roster's
    principals, archived and suspended ones too, since old texts still name
    them); None when there is nothing to hide. Fails closed: a roster that
    can't be read raises, so the caller shows nothing rather than the name."""
    if names is None:
        from openexecutive.people.store import list_people

        principals = [
            p for p in list_people(include_archived=True, include_suspended=True) if p.is_principal
        ]
        names = [p.full_name for p in principals if p.full_name]
        addresses = [a for p in principals for a in (p.email, *p.email_aliases) if a]
    words: set[str] = {a.strip() for a in addresses or [] if a.strip()}
    for name in names:
        full = " ".join(name.split())
        if full:
            words.add(full)
            words.add(full.split()[0])
    if not words:
        return None
    alternation = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![\w.@])(?:{alternation})(?![\w@])", re.IGNORECASE)


def scrub_principal_name(value: Any, pattern: re.Pattern[str] | None) -> Any:
    """``value`` with every mention of the principal's name replaced by
    ``PRINCIPAL_LABEL``: a string, or each string inside a list, dict,
    pydantic model or SimpleNamespace, which is changed in place (a response
    built for this request, never a stored record) and returned. Anything
    else (numbers, enums, classes) is left as it is, so there is no graph to
    walk into."""
    if pattern is None or value is None:
        return value
    if isinstance(value, str):
        return pattern.sub(lambda _m: PRINCIPAL_LABEL, value)
    if isinstance(value, list):
        for i, item in enumerate(value):
            value[i] = scrub_principal_name(item, pattern)
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            value[key] = scrub_principal_name(item, pattern)
        return value
    if isinstance(value, BaseModel):
        for field in type(value).model_fields:
            setattr(value, field, scrub_principal_name(getattr(value, field), pattern))
        return value
    if isinstance(value, SimpleNamespace):
        for field, item in vars(value).items():
            setattr(value, field, scrub_principal_name(item, pattern))
    return value


def shown_to_demo_viewer(people: list[Any], viewer_id: int | None) -> list[Any]:
    """``people`` as a demo person ``viewer_id`` may see them: no other demo
    person, nobody's email or chat ids but their own, and the principal by
    role only."""
    return [
        p if p.id == viewer_id else by_role(p.model_copy(update=_REACH_FIELDS))
        for p in people
        if p.id == viewer_id or not p.demo
    ]


def demo_turn(session: Any) -> bool:
    """Whether ``session`` is a demo person's turn (``Session.demo_caller``)."""
    return session is not None and getattr(session, "demo_caller", False) is True


# Anything a demo person asks beyond reading is refused outright, never put on
# an Approve card (the owner, Nov 8, 2026): a stranger trying the demo must not
# fill the principal's queue, or word a request for them to approve.
DEMO_REFUSAL = (
    "This is a demo account: it can read and answer, but nothing it asks for "
    "beyond that is done or sent to anyone for approval."
)


def demo_refusal(tool_name: str) -> str:
    """The JSON error tool_result for a step a demo person's turn may not take."""
    import json

    return json.dumps({"error": f"{str(tool_name)[:200]} was not done. {DEMO_REFUSAL} Do not retry."})


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


@dataclass(frozen=True)
class Taken:
    """How counting one demo question went."""

    ok: bool
    # Refused at the site-wide cap (DEMO_DAILY_QUESTIONS_TOTAL), not the person's.
    site: bool = False
    # The day's first refusal at the site-wide cap: the one that tells the principal.
    first_site_refusal: bool = False


def _count_one(conn: Any, row: int, day: str, limit: int) -> bool:
    # One statement, so two questions at once can't both take the last one.
    return conn.execute(
        f"INSERT INTO {TABLE} (person_id, day, count) VALUES (?, ?, 1)"
        " ON CONFLICT (person_id, day) DO UPDATE SET count = count + 1 WHERE count < ?",
        (row, day, limit),
    ).rowcount > 0


def take(
    person_id: int, limit: int, site_limit: int = 0, *, day: str | None = None, db_path: Path | None = None
) -> Taken:
    """Count one question for a demo person today (UTC), against their own
    ``limit`` and every demo person's together, ``site_limit``; 0 means no
    limit for either. A refused question counts toward neither: both counts
    are one transaction. Fails closed: a count that can't be written refuses
    the question."""
    if limit <= 0 and site_limit <= 0:
        return Taken(True)
    from openexecutive.people.store import _get_conn

    today = day or _today()
    try:
        with _get_conn(db_path) as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {TABLE} ("
                " person_id INTEGER NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,"
                " PRIMARY KEY (person_id, day))"
            )
            if limit > 0 and not _count_one(conn, person_id, today, limit):
                return Taken(False)
            if site_limit > 0 and not _count_one(conn, SITE_ROW, today, site_limit):
                conn.rollback()  # the person's count above
                refusals = conn.execute(
                    f"INSERT INTO {TABLE} (person_id, day, count) VALUES (?, ?, 1)"
                    " ON CONFLICT (person_id, day) DO UPDATE SET count = count + 1 RETURNING count",
                    (SITE_REFUSED_ROW, today),
                ).fetchone()
                return Taken(False, site=True, first_site_refusal=int(refusals[0]) == 1)
    except Exception:
        logger.warning("demo: question count failed for person %s; refusing", person_id, exc_info=True)
        return Taken(False)
    return Taken(True)


def take_question(person_id: int, limit: int, *, day: str | None = None, db_path: Path | None = None) -> bool:
    """Count one question for a demo person today (UTC); False, and nothing
    counted, once they've had ``limit``. ``limit`` 0 means no limit. Fails
    closed: a count that can't be written refuses the question."""
    return take(person_id, limit, day=day, db_path=db_path).ok


# The principal's notices in flight (a task with no reference can be collected).
_notices: set[asyncio.Task[None]] = set()


def site_limit_notice(site_limit: int) -> str:
    return (
        f"The demo reached today's limit of {site_limit} questions from all demo people together "
        "(DEMO_DAILY_QUESTIONS_TOTAL). Demo people are told it's busy until midnight UTC; "
        "nobody else is affected. Raise the limit in the environment if that was too few."
    )


async def _tell_principal(site_limit: int) -> None:
    try:
        from openexecutive.orchestrator.schedule_tools import set_session
        from openexecutive.scheduler.runner import _deliver_to_principal

        # Nothing sent here is the demo person's turn's outbound.
        with set_session(None):
            delivery = await _deliver_to_principal(site_limit_notice(site_limit), label="Demo limit")
        if not delivery.ok:
            logger.warning("demo: the principal wasn't told of the site limit: %s", delivery.detail)
    except Exception:
        logger.warning("demo: telling the principal of the site limit failed", exc_info=True)


def _site_refused(site_limit: int) -> None:
    """The day's first refusal at the site-wide cap: audit it, and tell the
    principal without holding up the refusal."""
    try:
        from openexecutive.audit import log_event

        log_event(
            "demo_site_limit",
            f"Demo questions reached today's limit of {site_limit} from all demo people together",
            actor="demo",
            details={"limit": site_limit, "day": _today()},
        )
    except Exception:
        logger.warning("demo: audit of the site limit failed", exc_info=True)
    task = asyncio.get_running_loop().create_task(_tell_principal(site_limit))
    _notices.add(task)
    task.add_done_callback(_notices.discard)


async def admit(person_id: int) -> str | None:
    """Count one question for demo person ``person_id`` (only ever called for
    one); None when they may ask it, else what to tell them. The caller
    refuses with 429 before anything is spent."""
    from openexecutive.config import get_settings

    settings = get_settings()
    limit, site_limit = settings.demo_daily_questions, settings.demo_daily_questions_total
    taken = await asyncio.to_thread(take, person_id, limit, site_limit)
    if taken.ok:
        return None
    if not taken.site:
        return limit_message(limit)
    if taken.first_site_refusal:
        _site_refused(site_limit)
    return SITE_LIMIT_REACHED


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

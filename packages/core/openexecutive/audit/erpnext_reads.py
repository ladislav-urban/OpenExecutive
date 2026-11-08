"""Whether a conversation read ERPNext, from the audit log.

A conversation that read ERPNext can restate the asker's own ERPNext figures,
so what it said stays out of everything other people's turns read (peer and
department memory, open loops, standing facts). The Executive's turn sets
``session.read_erpnext`` when it reads; a channel adapter builds a new Session
per message and a restart rebuilds them all, so the audit log's
``mcp:erpnext__`` rows for the conversation's id are the record that lasts.
Both answers fail closed: an unreadable audit log answers yes."""
from __future__ import annotations

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


# Audit event for a conversation that took in what another one read from
# ERPNext (a past-chat hit): from then on it counts as having read it too.
CARRIED_EVENT = "erpnext_carried"


def session_read_erpnext(session_id: str | None) -> bool:
    """Whether the conversation ``session_id`` read ERPNext in any turn, or
    took in what one read (``CARRIED_EVENT``), by the audit log. False for
    no id. Fails closed."""
    if not session_id:
        return False
    try:
        from openexecutive.audit.logger import get_audit_logger

        audit = get_audit_logger()
        # Every owner's rows: a teammate's turn writes its rows as theirs
        # (private_to_person), and those count as much as the principal's.
        return bool(
            audit.query(
                event_type="tool_invocation", session_id=session_id, q="mcp:erpnext__", limit=1, all_owners=True
            )
            or audit.query(event_type=CARRIED_EVENT, session_id=session_id, limit=1, all_owners=True)
        )
    except Exception:
        logger.warning("couldn't check whether a conversation read ERPNext — keeping it private", exc_info=True)
        return True


def conversation_read_erpnext(session: Any) -> bool:
    """Whether this conversation has read ERPNext in any turn: the session's
    flag, else :func:`session_read_erpnext` for its id (and then the flag is
    set). Fails closed."""
    if getattr(session, "read_erpnext", False) is True:
        return True
    found = session_read_erpnext(getattr(session, "session_id", None))
    if found and session is not None:
        # A frozen stand-in still gets the answer.
        with contextlib.suppress(Exception):
            session.read_erpnext = True
    return found


def mark_carried(session_id: str | None, from_session_ids: set[str]) -> bool:
    """Record, lastingly, that ``session_id`` took in what the conversations
    ``from_session_ids`` read from ERPNext, so every later turn of it counts
    as having read ERPNext (a channel adapter builds a new Session per
    message, and a restart rebuilds them all). False when it couldn't be
    recorded: the caller then withholds what it would have carried."""
    if not session_id:
        return False
    try:
        from openexecutive.audit.logger import get_audit_logger

        row = get_audit_logger().log(
            CARRIED_EVENT,
            "Took in an earlier chat that read ERPNext",
            session_id=session_id,
            details={"from_sessions": sorted(from_session_ids)[:20]},
        )
        return row is not None
    except Exception:
        logger.warning("couldn't record that a conversation took in ERPNext figures", exc_info=True)
        return False

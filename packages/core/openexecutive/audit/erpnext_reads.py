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


def session_read_erpnext(session_id: str | None) -> bool:
    """Whether the conversation ``session_id`` read ERPNext in any turn, by
    the audit log. False for no id. Fails closed."""
    if not session_id:
        return False
    try:
        from openexecutive.audit.logger import get_audit_logger

        return bool(
            get_audit_logger().query(
                event_type="tool_invocation", session_id=session_id, q="mcp:erpnext__", limit=1
            )
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

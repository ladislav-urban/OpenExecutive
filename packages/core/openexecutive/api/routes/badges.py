"""GET /badges — the counts the navigation shows next to Home, Knowledge and
the Watchlist, for the caller.

Light enough to poll after every navigation: it reads the alert board, the
review queue and the watchlist, and builds no page. Each count follows the
rule of the page it stands for, so a badge never disagrees with what opening
that page shows. A count that can't be read is left out rather than shown as
zero.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


class BadgeCounts(BaseModel):
    """``counts`` maps a navigation destination to its number; a destination
    whose count failed is absent."""

    counts: dict[str, int]


def _home(request: Request) -> int:
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.api.routes.today import _is_principal, needs_you_count

    caller = _resolve_caller_person_id(request)
    return needs_you_count(caller, principal=_is_principal(caller), now=datetime.now(UTC))


def _knowledge() -> int:
    from openexecutive.api.routes.review import _store

    counts = _store().count_by_status()
    return counts.get("pending", 0) + counts.get("needs_revision", 0)


def _watchlist() -> int:
    from openexecutive.monitoring import store as monitoring_store

    return len(monitoring_store.list_pending_suggestions())


@router.get("/badges", response_model=BadgeCounts, tags=["today"])
def get_badges(request: Request) -> BadgeCounts:
    """Home's "Needs you" count (the same number as GET /today's
    ``needs_you_count``), the review queue's pending and needs-revision items
    (GET /review/stats), and the research watch suggestions waiting on the
    Watchlist."""
    readers: dict[str, Callable[[], int]] = {
        "home": lambda: _home(request),
        "knowledge": _knowledge,
        "watchlist": _watchlist,
    }
    counts: dict[str, int] = {}
    for key, read in readers.items():
        try:
            counts[key] = read()
        except Exception:
            logger.warning("badges: the %s count failed", key, exc_info=True)
    return BadgeCounts(counts=counts)

"""The limit on turns from a host origin's page (``api.caller.from_host``).

A host page's scripts aren't Open Executive's: any script on that origin can
use the person's session through the UI proxy to send ``POST /chat``. So every
host turn counts against one limit per person, whichever of their
conversations it is in (ERPNext's docked panel and its native Chats alike):

- one in flight at a time, so a second turn while one still answers gets 429;
- ``HOURLY`` in a rolling hour. Demo people are exempt from this part, and
  their daily cap (``DEMO_DAILY_QUESTIONS``) governs them.

The route takes a slot before a demo question is spent, so a 429 never spends
one. The slot is released when the turn's stream ends (a dropped connection
included, once the stream has started), on every exit before the stream, and,
as a backstop for a response whose body never starts, once it is older than a
turn can run. ``POST /chat/add`` takes none: it folds into the turn already in
flight. Kept in this process's memory: there is one API process.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from fastapi import HTTPException

HOURLY = 60
WINDOW_S = 3600.0
HOURLY_MESSAGE = "You've asked a lot in the last hour. Try again in a few minutes."
IN_FLIGHT_MESSAGE = "Another question is still being answered. Try again when it has finished."

# key -> (slot token, monotonic start)
_in_flight: dict[str, tuple[str, float]] = {}
# key -> monotonic times of the turns taken in the last hour
_hits: dict[str, deque[float]] = {}


@dataclass
class HostSlot:
    """A host turn's place in the limit; ``release`` is idempotent and frees
    only this slot, never a later one under the same key."""

    key: str
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        held = _in_flight.get(self.key)
        if held is not None and held[0] == self.token:
            del _in_flight[self.key]


def take(key: str, *, hourly: bool, stale_after_s: float, now: float | None = None) -> HostSlot:
    """A slot for ``key``, or 429: one in flight first, then (when ``hourly``)
    ``HOURLY`` an hour. Synchronous, so a burst of requests can't all pass the
    check before any of them records its slot."""
    at = time.monotonic() if now is None else now
    held = _in_flight.get(key)
    if held is not None and at - held[1] < stale_after_s:
        raise HTTPException(status_code=429, detail=IN_FLIGHT_MESSAGE)
    if hourly:
        hits = _hits.get(key)
        if hits is not None:
            while hits and at - hits[0] >= WINDOW_S:
                hits.popleft()
            if not hits:
                del _hits[key]
                hits = None
        if hits is not None and len(hits) >= HOURLY:
            raise HTTPException(status_code=429, detail=HOURLY_MESSAGE)
        _hits.setdefault(key, deque()).append(at)
    slot = HostSlot(key)
    _in_flight[key] = (slot.token, at)
    return slot


def reset() -> None:
    """Forget every slot and count (tests)."""
    _in_flight.clear()
    _hits.clear()

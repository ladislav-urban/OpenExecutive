"""A demo person changes nothing of the company's (people.demo).

Many write routes take any signed-in caller: departments and goals, the
knowledge base, monitors, agents and personas, company documents, fixtures,
skills, the workflow designer. On a team install that is the team's trust;
a demo person is a prospect trying the product on shared demo data, so a
write of theirs could change what everyone sees, or put text every other
turn then reads. This gate refuses them every write outside a short list,
whichever route forgets to check, and the routes on the list still check
their own rules (an alert or decision not theirs, a chat not theirs).

It fails closed: a write by someone it can't look up on the roster is
answered 503, never let through on the chance they aren't a demo person.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Writes a demo person makes: their chat and its turns, their own chats,
# answering a card routed to them, their own notification and history
# settings, and a knowledge search (a read sent as POST). Each route keeps
# its own checks.
DEMO_WRITE_PREFIXES: tuple[str, ...] = (
    "/chat",
    "/sessions",
    "/alerts",
    "/decisions",
    "/notifications/settings",
    "/memories/history/settings",
    "/knowledge/search",
)

# Their own working style: /people/<their id>/attunement only.
_OWN_ATTUNEMENT = re.compile(r"/people/(\d+)/attunement")

DEMO_REFUSED = "In the demo you can look around and ask, but not change this."
_ROSTER_UNREADABLE = "Who you are can't be checked right now. Try again in a moment."


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def demo_may_write(path: str, person_id: int | None = None) -> bool:
    """Whether a demo person's (``person_id``) write to ``path`` reaches its
    route. Webhooks and the rest of the system's own callers are not on the
    list: a request there that carries a signed-in demo person is refused."""
    if _under(path, DEMO_WRITE_PREFIXES):
        return True
    own = _OWN_ATTUNEMENT.fullmatch(path)
    return own is not None and person_id is not None and int(own.group(1)) == person_id


def _route_path(request: Request) -> str:
    """The path the router matches (``root_path`` taken off), not the URL."""
    scope = request.scope
    path: str = scope.get("path", "")
    root = scope.get("root_path", "")
    return path[len(root):] if root and path.startswith(root) else path


def _person(email: str) -> Any:
    from openexecutive.people.store import find_person_by_email

    return find_person_by_email(email)


async def demo_write_gate(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.method in READ_ONLY_METHODS:
        return await call_next(request)
    from openexecutive.api.caller import caller

    email = caller(request).email
    if not email:
        # Names no one: the operator, the CLI, a service, a signed webhook.
        return await call_next(request)
    path = _route_path(request)
    if _under(path, DEMO_WRITE_PREFIXES):
        return await call_next(request)
    try:
        person = await asyncio.to_thread(_person, email)
    except Exception:
        logger.warning("demo gate: roster lookup failed — refusing the write", exc_info=True)
        return JSONResponse({"detail": _ROSTER_UNREADABLE}, status_code=503)
    if person is None or not getattr(person, "demo", False) or getattr(person, "is_principal", False):
        return await call_next(request)
    if demo_may_write(path, person.id):
        return await call_next(request)
    logger.info("demo gate: refused %s %s for person %s", request.method, path, person.id)
    return JSONResponse({"detail": DEMO_REFUSED}, status_code=403)

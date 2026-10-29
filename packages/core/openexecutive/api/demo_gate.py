"""A demo person changes nothing of the company's (people.demo).

Many write routes take any signed-in caller: departments and goals, the
knowledge base, monitors, agents and personas, company documents, fixtures,
skills, the workflow designer. On a team install that is the team's trust;
a demo person is a prospect trying the product on shared demo data, so a
write of theirs could change what everyone sees, or put text every other
turn then reads. This gate refuses them every write outside a short list,
whichever route forgets to check, and the routes on the list still check
their own rules (an alert or decision not theirs, a chat not theirs).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Writes a demo person makes: their chat and its turns, their own chats,
# answering a card routed to them, their notification settings, and a
# knowledge search (a read sent as POST). Each route keeps its own checks.
DEMO_WRITE_PREFIXES: tuple[str, ...] = (
    "/chat",
    "/sessions",
    "/alerts",
    "/decisions",
    "/notifications/settings",
    "/knowledge/search",
)

# Not a person's browser: signed by the system that calls them, or the UI
# server's own (proxySegments.ts SERVER_ONLY). The gate leaves them alone.
_NOT_A_SIGNED_IN_PERSON: tuple[str, ...] = ("/webhook", "/a2a", "/auth")

DEMO_REFUSED = "In the demo you can look around and ask, but not change this."


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def demo_may_write(path: str) -> bool:
    """Whether a demo person's write to ``path`` reaches its route."""
    return _under(path, DEMO_WRITE_PREFIXES) or _under(path, _NOT_A_SIGNED_IN_PERSON)


def _demo_caller(request: Request) -> bool:
    from openexecutive.api.routes.people import _demo_viewer

    return _demo_viewer(request)[0]


async def demo_write_gate(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.method in READ_ONLY_METHODS or demo_may_write(request.url.path):
        return await call_next(request)
    import asyncio

    try:
        demo = await asyncio.to_thread(_demo_caller, request)
    except Exception:
        # Who is calling can't be read: the route's own checks decide, as
        # they did before this gate.
        logger.warning("demo gate: caller check failed", exc_info=True)
        demo = False
    if demo:
        logger.info("demo gate: refused %s %s", request.method, request.url.path)
        return JSONResponse({"detail": DEMO_REFUSED}, status_code=403)
    return await call_next(request)

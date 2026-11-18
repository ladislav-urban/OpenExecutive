"""A route dependency that lets only the principal through.

For routes whose writes change what every turn of the principal's reads or
runs on (the Executive's prompts, the company context, the install's data):
``Depends(principal_only("..."))`` on the route, or on the router, answers
403 before the route does any work.

The rule is the one for starting a principal-only workflow
(``api.routes.workflows._caller_is_the_principal``, read through
``api.caller``): the principal's own email on a non-archived principal entry,
or a request that names no one and runs as the principal (open mode with no
``x-caller-email``: the CLI, local login, a direct call; or the signed
operator). Anyone else is refused: a teammate, a demo person, a signed-in
email on nobody's entry, a verified service call, a roster that can't be
read, and any call from a host origin's page.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import HTTPException, Request

from openexecutive.api import caller as api_caller

logger = logging.getLogger(__name__)


def caller_is_the_principal(request: Request) -> bool:
    """Whether this caller is the principal, as ``principal_only`` decides it."""
    from openexecutive.api.routes.workflows import _caller_is_the_principal

    return not api_caller.from_host(request) and _caller_is_the_principal(request)


def principal_only(detail: str) -> Callable[[Request], None]:
    """A dependency that raises 403 with ``detail`` unless the caller is the
    principal (``caller_is_the_principal``)."""

    def _gate(request: Request) -> None:
        if caller_is_the_principal(request):
            return
        logger.warning(
            "principal-only route refused method=%s path=%s",
            request.method, request.url.path[:200],
        )
        raise HTTPException(status_code=403, detail=detail)

    return _gate

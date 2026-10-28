"""The demo admin console's view of OpenExecutive's demo people.

oe_connector's demo admin console (its ``docs/demo/demo-admin-console-plan.md``,
step 10) shows each demo person's questions today and, when it deletes a demo
user, archives their OpenExecutive Person too, so Delete removes all three
accounts (Keycloak, ERPNext, OpenExecutive). Two routes:

- ``POST /webhook/erpnext/demo-people``, body ``{}``: every non-archived demo
  person (``Person.demo``), suspended ones too, as ``email``, ``name``,
  ``questions_today`` (today's UTC count, ``people/demo.py``) and
  ``suspended``, with the day's ``limit`` (``DEMO_DAILY_QUESTIONS``).
- ``POST /webhook/erpnext/demo-people/archive``, body ``{"email": ...}``: archive
  the demo person with that address. 404, with no detail, when nobody has it;
  ``{"archived": false, "reason": "already"}`` when they are archived already;
  409 for anyone who isn't a demo person, isn't directory-managed, or is the
  principal. Audited ``demo_person_archived``, private to the principal.

**Who is asking.** oe_connector signs each request as it signs the panel's
(``erpnext_panel.verify_panel_assertion``: the ``oe_connector_panel_private_key``
/ ``ERPNEXT_PANEL_PUBLIC_KEYS`` pair, over the exact body, used once), with an
audience of each route's own, so no other assertion can be replayed here. The
console is the principal's alone, so ``sub`` must be the principal's email, or
the request is refused (403). That email is ERPNext's word, which an ERPNext
administrator can change, so these routes stay narrow: the most a forged claim
can do is read demo people's counts, or archive a demo, directory-managed
person, whom the console's Delete removes from Keycloak and ERPNext anyway.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from openexecutive.api.caller import CallerKeysError, JtiLedger
from openexecutive.integrations import erpnext_panel
from openexecutive.integrations.erpnext_panel import (
    PanelRefused,
    panel_keys,
    verify_panel_assertion,
)

logger = logging.getLogger(__name__)

router = APIRouter()

PATH = "/webhook/erpnext/demo-people"
ARCHIVE_PATH = "/webhook/erpnext/demo-people/archive"
ASSERTION_HEADER = erpnext_panel.ASSERTION_HEADER
# Kept in step with oe_connector's demo admin console (panel.mint's audience).
AUDIENCE = "openexecutive-erpnext-demo-people"
ARCHIVE_AUDIENCE = "openexecutive-erpnext-demo-people-archive"

MAX_BODY_BYTES = 4096
MAX_EMAIL_LEN = 254

_ledger = JtiLedger()


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def _read_capped(request: Request) -> bytes | None:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _verified_principal(request: Request, audience: str) -> tuple[bytes, None] | tuple[None, JSONResponse]:
    """The request body once its signature names the principal; else the answer."""
    try:
        keys = panel_keys()
    except CallerKeysError:
        logger.error("ERPNEXT_PANEL_PUBLIC_KEYS is malformed; refusing the demo admin console")
        return None, _error(503, "not_configured", "The demo admin console is not set up on OpenExecutive.")
    if not keys:
        return None, _error(503, "not_configured", "The demo admin console is not set up on OpenExecutive.")
    body = await _read_capped(request)
    if body is None:
        return None, _error(413, "too_large", "The request is too large.")
    tokens = request.headers.getlist(ASSERTION_HEADER)
    try:
        if len(tokens) != 1:
            raise PanelRefused("missing" if not tokens else "duplicate_header")
        email = verify_panel_assertion(
            tokens[0].strip(),
            keys=keys,
            body=body,
            now=int(time.time()),
            ledger=_ledger,
            audience=audience,
            with_subject=True,
        )
    except PanelRefused as refused:
        logger.warning("demo admin console refused reason=%s", refused.reason)
        return None, _error(401, "unauthorized", "This request was not signed by the ERPNext site.")
    from openexecutive.people.store import find_principal_person, normalize_email

    principal = find_principal_person()
    if principal is None or normalize_email(principal.email or "") != email:
        logger.warning("demo admin console refused: the signed user is not the principal")
        return None, _error(403, "forbidden", "Only the principal may use the demo admin console.")
    return body, None


@router.post(PATH)
async def demo_people(request: Request) -> JSONResponse:
    _, refused = await _verified_principal(request, AUDIENCE)
    if refused is not None:
        return refused
    from openexecutive.config import get_settings
    from openexecutive.people.demo import questions_today
    from openexecutive.people.store import list_people

    people: list[dict[str, Any]] = [
        {
            "email": person.email or "",
            "name": person.full_name,
            "questions_today": questions_today(person.id),
            "suspended": person.suspended,
        }
        for person in list_people(include_suspended=True)
        if person.demo and person.id is not None
    ]
    return JSONResponse({"limit": get_settings().demo_daily_questions, "people": people})


@router.post(ARCHIVE_PATH, response_model=None)
async def archive_demo_person(request: Request) -> Response:
    body, refused = await _verified_principal(request, ARCHIVE_AUDIENCE)
    if refused is not None:
        return refused
    assert body is not None
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return _error(400, "bad_request", "The body must be JSON.")
    raw = data.get("email") if isinstance(data, dict) else None
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_EMAIL_LEN:
        return _error(400, "bad_request", "The body must name an email.")
    from openexecutive.people import registry, store

    address = store.normalize_email(raw)
    person = store.find_person_by_own_address(address)
    if person is None:
        return Response(status_code=404)
    if person.archived:
        return JSONResponse({"archived": False, "reason": "already"})
    if person.is_principal or not person.demo or not person.directory_managed:
        return _error(409, "not_a_demo_person", "Only a demo person the directory manages is archived here.")
    assert person.id is not None
    archived = store.archive_person(person.id)
    registry.invalidate()
    if not archived:
        return JSONResponse({"archived": False, "reason": "already"})
    from openexecutive.audit import log_event

    log_event(
        "demo_person_archived",
        f"demo person #{person.id} archived by the demo admin console",
        actor="erpnext_demo_admin",
        details={"person_id": person.id},
        private=True,
    )
    return JSONResponse({"archived": True})


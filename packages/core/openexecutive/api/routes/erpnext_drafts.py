"""ERPNext draft cards waiting for the caller (ERPNext phase 4).

Routes:
  GET /erpnext/drafts — the caller's own draft cards still waiting
                        (``integrations.erpnext_drafts``), from the database;
                        no ERPNext call. Approve and Dismiss are
                        ``POST /decisions/{id}/approve`` and ``/reject``.
  GET /erpnext/alert-drafts — the principal's open ERPNext alerts that
                        suggest a draft, for their "Prepare" button
                        (``integrations.erpnext_alert_drafts``); [] for anyone
                        else, as ERPNext alerts are the principal's alone.
  POST /erpnext/alert-drafts/{alert_id} — the principal's tap: ERPNext checks
                        the suggested draft as them and the usual card is left.
  GET /erpnext/drafts/oversight — one oversight record per ERPNext draft card
                        (``integrations.erpnext_oversight``), for the principal
                        alone: ``since`` / ``until`` (ISO dates or times),
                        ``format`` json or csv, and ``erpnext=1`` for what
                        became of each draft in ERPNext since.

A card is its person's alone: the caller is resolved the way the decisions
routes resolve an approver (``chat._resolve_caller_person_id``), and anyone
with no People entry gets an empty list, never someone else's cards.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


class DraftFieldOut(BaseModel):
    label: str
    value: str


class DraftLineOut(BaseModel):
    item_code: str
    qty: str
    warehouse: str
    # A Quotation's or Purchase Order's line (ERPNext's figures, formatted);
    # "" on the others and where ERPNext gave none.
    rate: str = ""
    amount: str = ""


class DraftCardOut(BaseModel):
    decision_id: int
    created_at: str
    expires_at: str
    why: str
    doctype: str
    fields: list[DraftFieldOut]
    lines: list[DraftLineOut]
    notes: list[str]
    last_error: str
    # "erpnext_panel" when asked for in ERPNext's panel, else "".
    asked_in: str = ""


class DraftCardsOut(BaseModel):
    cards: list[DraftCardOut]


@router.get("/erpnext/drafts", response_model=DraftCardsOut)
def get_erpnext_drafts(request: Request) -> DraftCardsOut:
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.integrations.erpnext_drafts import cards
    from openexecutive.people.store import get_person

    person_id = _resolve_caller_person_id(request)
    person = get_person(person_id) if person_id is not None else None
    if person is None or person.id is None or person.archived:
        return DraftCardsOut(cards=[])
    # A teammate's cards hold ERPNext figures read as them, which ERPNext
    # gives a teammate in the web app only under signed sign-ins
    # (erpnext_caller.caller_for); without them, whoever holds the shared
    # secret could name any teammate.
    if not person.is_principal:
        from openexecutive.api.caller import signing_on

        if not signing_on():
            return DraftCardsOut(cards=[])
    try:
        found = cards(person)
    except Exception as exc:
        logger.exception("erpnext_drafts: reading the draft cards failed")
        raise HTTPException(
            status_code=503,
            detail={"code": "unavailable", "message": "Couldn't read the drafts waiting for you."},
        ) from exc
    return DraftCardsOut(cards=[DraftCardOut(**asdict(card)) for card in found])


class AlertDraftOut(BaseModel):
    alert_id: int
    label: str


class AlertDraftsOut(BaseModel):
    suggestions: list[AlertDraftOut]


class PreparedOut(BaseModel):
    decision_id: int
    # The same draft was already waiting on this card.
    already_waiting: bool


@router.get("/erpnext/alert-drafts", response_model=AlertDraftsOut)
def get_erpnext_alert_drafts(request: Request) -> AlertDraftsOut:
    from openexecutive.api.routes.people import caller_is_principal
    from openexecutive.integrations.erpnext_alert_drafts import open_suggestions
    from openexecutive.integrations.erpnext_drafts import drafts_on

    if not caller_is_principal(request) or not drafts_on():
        return AlertDraftsOut(suggestions=[])
    try:
        found = open_suggestions()
    except Exception:
        logger.exception("erpnext_alert_drafts: reading the suggestions failed")
        return AlertDraftsOut(suggestions=[])
    return AlertDraftsOut(suggestions=[AlertDraftOut(alert_id=i, label=label) for i, label in found])


@router.post("/erpnext/alert-drafts/{alert_id}", response_model=PreparedOut)
async def prepare_erpnext_alert_draft(alert_id: int, request: Request) -> PreparedOut:
    from openexecutive.alerts import store
    from openexecutive.api import caller as api_caller
    from openexecutive.api.routes.people import caller_is_principal
    from openexecutive.integrations.erpnext_alert_drafts import prepare
    from openexecutive.integrations.erpnext_drafts import ApproveRefused

    alert = store.get_alert(alert_id)
    # The principal's alone: anyone else is told it isn't there.
    if alert is None or alert.source != "erpnext" or not caller_is_principal(request):
        raise HTTPException(status_code=404, detail={"code": "gone", "message": "That alert isn't there."})
    if alert.status not in ("unread", "read"):
        raise HTTPException(status_code=409, detail={"code": "closed", "message": "That alert is already closed."})
    try:
        decision_id, new = await prepare(alert, caller=api_caller.caller(request))
    except ApproveRefused as refused:
        raise HTTPException(
            status_code=refused.status, detail={"code": refused.code, "message": refused.message},
        ) from None
    return PreparedOut(decision_id=decision_id, already_waiting=not new)


def _moment(value: str | None, name: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be an ISO date or time") from None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _audit_export(request: Request, details: dict[str, Any]) -> None:
    """One row per export: the record is accountability evidence, so taking
    it is evidenced too. Private, since the record names everyone who asked;
    what was asked and how many came back, never the records themselves."""
    from openexecutive.api import caller as api_caller
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "erpnext_oversight_exported",
        f"Exported the ERPNext draft oversight record ({details['records']} records)",
        actor=api_caller.actor(request), details=details, private=True,
    )


@router.get("/erpnext/drafts/oversight")
async def get_erpnext_draft_oversight(
    request: Request,
    since: str | None = None,
    until: str | None = None,
    format: Literal["json", "csv"] = "json",  # noqa: A002 - the query parameter's name
    erpnext: bool = False,
) -> Any:
    """Every ERPNext draft card in [since, until), with who asked, what it
    showed, who decided and what came of it: the evidence a person oversaw
    each draft. The principal's alone (it names everyone who asked); what
    someone else's card said stays theirs (``erpnext_oversight``)."""
    from openexecutive.api.routes.people import caller_is_principal
    from openexecutive.integrations import erpnext_oversight

    if not caller_is_principal(request):
        raise HTTPException(status_code=403, detail="Only the account owner can read the oversight record.")
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    start, end = _moment(since, "since"), _moment(until, "until")
    rows = erpnext_oversight.records(since=start, until=end, reader=principal.id if principal else None)
    asked: dict[str, Any] = {
        "since": start.isoformat() if start else None, "until": end.isoformat() if end else None,
        "format": format, "erpnext": erpnext,
    }
    if erpnext:
        try:
            rows = await erpnext_oversight.with_erpnext_state(rows)
        except Exception:
            logger.exception("erpnext_oversight: asking ERPNext what became of the drafts failed")
            _audit_export(request, {**asked, "erpnext_answered": False, "records": len(rows)})
            raise HTTPException(
                status_code=502, detail="ERPNext didn't answer, so the drafts' state there isn't included."
            ) from None
    _audit_export(request, {**asked, "erpnext_answered": True if erpnext else None, "records": len(rows)})
    if format == "csv":
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        return Response(
            content=erpnext_oversight.to_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="erpnext-draft-oversight-{stamp}.csv"'},
        )
    return {"generated_at": datetime.now(UTC).isoformat(), "erpnext_state": erpnext, "records": rows}

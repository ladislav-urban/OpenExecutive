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

A card is its person's alone: the caller is resolved the way the decisions
routes resolve an approver (``chat._resolve_caller_person_id``), and anyone
with no People entry gets an empty list, never someone else's cards.
"""
from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
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

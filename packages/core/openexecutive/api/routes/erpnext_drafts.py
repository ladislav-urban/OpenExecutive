"""ERPNext draft cards waiting for the caller (ERPNext phase 4a).

Routes:
  GET /erpnext/drafts — the caller's own draft cards still waiting
                        (``integrations.erpnext_drafts``), from the database;
                        no ERPNext call. Approve and Dismiss are
                        ``POST /decisions/{id}/approve`` and ``/reject``.

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
    try:
        found = cards(person)
    except Exception as exc:
        logger.exception("erpnext_drafts: reading the draft cards failed")
        raise HTTPException(
            status_code=503,
            detail={"code": "unavailable", "message": "Couldn't read the drafts waiting for you."},
        ) from exc
    return DraftCardsOut(cards=[DraftCardOut(**asdict(card)) for card in found])

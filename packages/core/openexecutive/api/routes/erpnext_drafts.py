"""ERPNext draft cards waiting for the caller (ERPNext phase 4).

Routes:
  GET /erpnext/drafts — the caller's own draft cards still waiting
                        (``integrations.erpnext_drafts``), from the database.
                        Its only ERPNext calls: up to three of the caller's
                        own credit-raise cards read again as them, each at
                        most once a minute (``erpnext_credit.recheck``),
                        marking ``changed`` where ERPNext now reads one
                        differently. Approve and Dismiss are
                        ``POST /decisions/{id}/approve`` and ``/reject``.
  GET /erpnext/alert-drafts — the open ERPNext alerts that suggest a draft
                        the caller may prepare, for their "Prepare" button
                        (``integrations.erpnext_alert_drafts``): the
                        principal's, and a credit hold's raise for the people
                        it names (with teammates' drafts on and signed
                        sign-ins); [] for anyone else.
  POST /erpnext/alert-drafts/{alert_id} — the tap: ERPNext checks the
                        suggested draft as the person who tapped and the usual
                        card is left (a raise's for whoever approves it,
                        ``integrations.erpnext_credit``). ``decision_id`` is
                        null when no card was left and ``message`` says why.
  GET /erpnext/drafts/oversight — one oversight record per ERPNext draft card
                        (``integrations.erpnext_oversight``), for the principal
                        alone: ``since`` / ``until`` (ISO dates or times),
                        ``format`` json or csv, and ``erpnext=1`` for what
                        became of each draft in ERPNext since and the
                        share submitted unchanged (``unchanged_share``).
                        The JSON also has ``gate``, the approval gate's
                        own numbers against the range just before and
                        its flags (``erpnext_oversight.gate``).

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

from openexecutive.briefing.home_rules import register_waiting_cards

router = APIRouter()
logger = logging.getLogger(__name__)


def _waiting_drafts(person: Any) -> int:
    """Draft cards waiting for ``person``, for Home's Needs you count: what
    GET /erpnext/drafts lists them (a teammate's only under signed sign-ins)."""
    from openexecutive.api.caller import signing_on
    from openexecutive.integrations.erpnext_drafts import cards

    if person.id is None or not person.active or (not person.is_principal and not signing_on()):
        return 0
    return len(cards(person))


register_waiting_cards("erpnext_drafts", _waiting_drafts)


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
    # "create", "update" or "comment" (integrations.erpnext_changes).
    operation: str = "create"
    fields: list[DraftFieldOut]
    lines: list[DraftLineOut]
    notes: list[str]
    last_error: str
    # "erpnext_panel" when asked for in ERPNext's panel, "erpnext_desk" when
    # from ERPNext's desk, else "".
    asked_in: str = ""
    # A credit-limit raise ERPNext now reads differently: approving is refused.
    changed: bool = False


class DraftCardsOut(BaseModel):
    cards: list[DraftCardOut]


@router.get("/erpnext/drafts", response_model=DraftCardsOut)
async def get_erpnext_drafts(request: Request) -> DraftCardsOut:
    from openexecutive.api import caller as api_caller
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.integrations.erpnext_credit import recheck
    from openexecutive.integrations.erpnext_drafts import cards
    from openexecutive.people.store import get_person

    person_id = _resolve_caller_person_id(request)
    person = get_person(person_id) if person_id is not None else None
    if person is None or person.id is None or not person.active:
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
    # Here only, never in cards(), which Home's count calls.
    try:
        await recheck(person, found, api_caller.caller(request))
    except Exception:
        logger.warning("erpnext_drafts: reading the credit raises again failed", exc_info=True)
    return DraftCardsOut(cards=[DraftCardOut(**asdict(card)) for card in found])


class AlertDraftOut(BaseModel):
    alert_id: int
    label: str


class AlertDraftsOut(BaseModel):
    suggestions: list[AlertDraftOut]


class PreparedOut(BaseModel):
    # None when no card was left (a raise only someone with credit authority
    # may prepare): ``message`` says who can.
    decision_id: int | None
    # The same draft was already waiting on this card.
    already_waiting: bool
    # What to tell the person who tapped, for a raise; "" otherwise.
    message: str = ""


def _tapper(request: Request) -> Any:
    """The caller's own active team entry, or None."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.people.store import get_person

    person_id = _resolve_caller_person_id(request)
    person = get_person(person_id) if person_id is not None else None
    if person is None or person.id is None or not person.active or person.kind != "team":
        return None
    return person


@router.get("/erpnext/alert-drafts", response_model=AlertDraftsOut)
def get_erpnext_alert_drafts(request: Request) -> AlertDraftsOut:
    from openexecutive.api.caller import signing_on
    from openexecutive.api.routes.people import caller_is_principal
    from openexecutive.integrations.erpnext_alert_drafts import open_suggestions
    from openexecutive.integrations.erpnext_drafts import drafts_on, teammates_on

    if not drafts_on():
        return AlertDraftsOut(suggestions=[])
    viewer: Any = None
    if not caller_is_principal(request):
        # The same gate as GET /erpnext/drafts: a teammate only under signed
        # sign-ins, and only when teammates may have drafts at all.
        if not signing_on() or not teammates_on():
            return AlertDraftsOut(suggestions=[])
        viewer = _tapper(request)
        if viewer is None or viewer.is_principal:
            return AlertDraftsOut(suggestions=[])
    try:
        found = open_suggestions(viewer=viewer)
    except Exception:
        logger.exception("erpnext_alert_drafts: reading the suggestions failed")
        return AlertDraftsOut(suggestions=[])
    return AlertDraftsOut(suggestions=[AlertDraftOut(alert_id=i, label=label) for i, label in found])


@router.post("/erpnext/alert-drafts/{alert_id}", response_model=PreparedOut)
async def prepare_erpnext_alert_draft(alert_id: int, request: Request) -> PreparedOut:
    from openexecutive.alerts import store
    from openexecutive.alerts.models import named_for
    from openexecutive.api import caller as api_caller
    from openexecutive.api.routes.people import caller_is_principal
    from openexecutive.integrations import erpnext_credit
    from openexecutive.integrations.erpnext_alert_drafts import prepare, suggestion_for
    from openexecutive.integrations.erpnext_drafts import ApproveRefused

    gone = HTTPException(status_code=404, detail={"code": "gone", "message": "That alert isn't there."})
    alert = store.get_alert(alert_id)
    if alert is None or alert.source != "erpnext":
        raise gone
    principal = caller_is_principal(request)
    tapper = _tapper(request)
    if principal and (tapper is None or not tapper.is_principal):
        from openexecutive.people.store import find_principal_person

        tapper = find_principal_person()
    suggestion = suggestion_for(alert)
    raise_card = suggestion is not None and suggestion["doctype"] == erpnext_credit.DOCTYPE
    # The principal's alone, but for a credit hold's raise, which the people
    # it names may prepare too: anyone else is told it isn't there.
    if not principal and not (raise_card and tapper is not None and named_for(alert, tapper.id)):
        raise gone
    if alert.status not in ("unread", "read"):
        raise HTTPException(status_code=409, detail={"code": "closed", "message": "That alert is already closed."})
    try:
        if raise_card:
            prepared = await erpnext_credit.prepare(alert, caller=api_caller.caller(request), tapper=tapper)
            return PreparedOut(
                decision_id=prepared.decision_id, already_waiting=prepared.decision_id is not None and not prepared.new,
                message=prepared.message,
            )
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
    now = datetime.now(UTC)
    # The gate's trend compares the range of equal length just before; a
    # range with no start has none. Checked before anything is audited.
    earlier = None
    if start is not None:
        if start > (end or now):
            raise HTTPException(status_code=422, detail="since must be before until (or now)")
        try:
            earlier = start - ((end or now) - start)
        except OverflowError:
            raise HTTPException(status_code=422, detail="since and until span too many years") from None
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
    out: dict[str, Any] = {"generated_at": now.isoformat(), "erpnext_state": erpnext}
    if erpnext:
        out["unchanged_share"] = erpnext_oversight.unchanged_share(rows)
    # Only who decided and when is read from the range before.
    before, previous = None, None
    if start is not None and earlier is not None:
        before = erpnext_oversight.records(since=earlier, until=start)
        previous = {"since": earlier.isoformat(), "until": start.isoformat()}
    out["gate"] = {
        "range": {"since": asked["since"], "until": (end or now).isoformat()}, "before_range": previous,
        **erpnext_oversight.gate(rows, before, erpnext=erpnext),
    }
    return {**out, "records": rows}

"""ERPNext drafts a person approves: ``propose_erpnext_draft`` and its cards.

ERPNext phase 4a (design: oe_connector's ``docs/erpnext-integration.md``,
"Writes come last", and ``docs/erpnext-phase4-plan.md``). The Executive can
prepare two kinds of ERPNext document, a ToDo (a follow-up assigned to
someone) and a Material Request (a restock), as unsubmitted drafts. Nothing
is written until a person taps Approve on a card that shows exactly what will
be written, and then once, with no model call. A person finishes every
document in ERPNext: nothing here submits, cancels, amends or deletes.

**Propose** (``handle_propose``). The model gives the DocType and its fields.
They are checked for shape here (``check_fields``: only the fields
oe_connector takes, the right types and lengths), then by oe_connector itself,
as the asking person, without saving (``erpnext__draft_check``, inside
``erpnext_caller.draft_check_run``, the only way the gateway lets it through
from a turn). What it returns fills in the card (``card_view``), as plain
text labelled as ERPNext's. Offered only where ERPNext reads are and in 4a to
the principal alone (``offer_refusal``): never on an unattended run, a turn
private to the principal, an email turn, ERPNext's own panel or a turn that
read their mail (``delegation.lockdown``).

**The card** is an ``erpnext_draft`` decision for that person, theirs alone
(``approver_only``) and never an alert: ``CARDS_PER_TURN`` a turn,
``OPEN_CARDS_MAX`` waiting, dropped after ``CARD_TTL``. It is shown on Today
and in the chat that proposed it (``GET /erpnext/drafts``).

**Approve** (``approve``, from ``POST /decisions/{id}/approve``). Only the
card's person, known to the API as them (``verified.caller_refusal``). The
card is claimed first, so it runs once; then the stored call is made exactly
as stored, inside ``erpnext_caller.draft_executor_run``: the only marker under
which the gateway passes a draft tool, signed as the approver with
``purpose: "draft"``. oe_connector inserts it as their ERPNext user, with
``docstatus = 0``, keyed by the card (``idempotency_key``), so a retried
approval returns the same draft. On failure the card waits again with the
reason (``last_error``), up to ``ATTEMPTS_MAX`` tries.

Take the lead never makes one: unattended runs are not offered the tool, and
the gateway refuses every ``erpnext__draft*`` call outside the executor's
marker, so a draft always waits on its card.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

DECISION_CLASS = "erpnext_draft"
PROPOSE = "propose_erpnext_draft"

# What 4a may draft, and the oe_connector tool that inserts each. Quotation
# and Purchase Order (oe_connector has them) wait for 4b.
DRAFT_TOOLS: dict[str, str] = {
    "ToDo": "erpnext__draft_todo",
    "Material Request": "erpnext__draft_material_request",
}

CARDS_PER_TURN = 3
OPEN_CARDS_MAX = 10
CARD_TTL = timedelta(days=3)
ATTEMPTS_MAX = 5
CALL_TIMEOUT_S = 60.0
MAX_LINES = 20
TEXT_MAX = 1000
NAME_MAX = 140
WHY_MAX = 300
QTY_MAX = 1_000_000
# What the card shows of any one ERPNext value, and of a failure.
SHOWN_MAX = 300

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# oe_connector's own key rule (drafts._KEY_RE).
_KEY_RE = re.compile(r"[A-Za-z0-9_:.-]{8,140}")
# A draft's name as ERPNext gives it (MAT-MR-2026-00012, a ToDo's hash).
_DOC_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,139}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_TODO_FIELDS = frozenset({"description", "allocated_to", "priority", "date", "reference_type", "reference_name"})
_MR_FIELDS = frozenset({"company", "material_request_type", "schedule_date", "items"})
_MR_LINE_FIELDS = frozenset({"item_code", "qty", "warehouse"})


class ApproveRefused(Exception):
    """Why an approval was refused: an HTTP status, a code and what to tell them."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class DraftCard:
    decision_id: int
    created_at: str
    expires_at: str
    why: str
    doctype: str
    # What will be written, as ERPNext checked it: label and value pairs, and
    # a Material Request's lines.
    fields: list[dict[str, str]] = field(default_factory=list)
    lines: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Why the last approval didn't make it, when it was tried.
    last_error: str = ""


def _one_line(value: Any, limit: int = SHOWN_MAX) -> str:
    from openexecutive.utils.prompt_blocks import plain

    return " ".join(plain(str(value if value is not None else "")).split())[:limit].strip()


def _error(message: str) -> str:
    return json.dumps({"error": message})


# ---------------------------------------------------------------------------
# What a draft may say
# ---------------------------------------------------------------------------

def _name(value: Any, label: str, *, required: bool) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"`{label}` is required.")
        return None
    if not isinstance(value, str):
        raise ValueError(f"`{label}` must be text.")
    text = _one_line(value, NAME_MAX + 1)
    if not text and required:
        raise ValueError(f"`{label}` is required.")
    if len(text) > NAME_MAX:
        raise ValueError(f"`{label}` is longer than {NAME_MAX} characters.")
    return text or None


def _date(value: Any, label: str, *, required: bool) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"`{label}` is required, as YYYY-MM-DD.")
        return None
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
        raise ValueError(f"`{label}` must be a date as YYYY-MM-DD.")
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"`{label}` is not a real date.") from None
    return value.strip()


def _todo(fields: dict[str, Any]) -> dict[str, Any]:
    from openexecutive.utils.prompt_blocks import plain

    extra = set(fields) - _TODO_FIELDS
    if extra:
        raise ValueError(f"A ToDo takes only {', '.join(sorted(_TODO_FIELDS))}; not {', '.join(sorted(extra))}.")
    description = fields.get("description")
    if not isinstance(description, str) or not plain(description).strip():
        raise ValueError("`description` is required: what the follow-up is.")
    text = plain(description).strip()
    if len(text) > TEXT_MAX:
        raise ValueError(f"`description` is longer than {TEXT_MAX} characters.")
    out: dict[str, Any] = {
        "description": text,
        "allocated_to": _name(fields.get("allocated_to"), "allocated_to", required=True),
    }
    priority = fields.get("priority")
    if priority not in (None, ""):
        if priority not in ("Low", "Medium", "High"):
            raise ValueError("`priority` must be Low, Medium or High.")
        out["priority"] = priority
    if (due := _date(fields.get("date"), "date", required=False)) is not None:
        out["date"] = due
    reference_type = _name(fields.get("reference_type"), "reference_type", required=False)
    reference_name = _name(fields.get("reference_name"), "reference_name", required=False)
    if bool(reference_type) != bool(reference_name):
        raise ValueError("Give `reference_type` and `reference_name` together (e.g. Sales Invoice and its name), or neither.")
    if reference_type:
        out["reference_type"], out["reference_name"] = reference_type, reference_name
    return out


def _material_request(fields: dict[str, Any]) -> dict[str, Any]:
    extra = set(fields) - _MR_FIELDS
    if extra:
        raise ValueError(
            f"A Material Request takes only {', '.join(sorted(_MR_FIELDS))}; not {', '.join(sorted(extra))}."
        )
    out: dict[str, Any] = {
        "company": _name(fields.get("company"), "company", required=True),
        "schedule_date": _date(fields.get("schedule_date"), "schedule_date", required=True),
    }
    kind = fields.get("material_request_type")
    if kind not in (None, ""):
        if kind not in ("Purchase", "Material Transfer"):
            raise ValueError("`material_request_type` must be Purchase or Material Transfer.")
        out["material_request_type"] = kind
    items = fields.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("`items` must be a list of {item_code, qty, warehouse}.")
    if len(items) > MAX_LINES:
        raise ValueError(f"At most {MAX_LINES} items on one draft.")
    lines: list[dict[str, Any]] = []
    for number, raw in enumerate(items, start=1):
        if not isinstance(raw, dict) or set(raw) - _MR_LINE_FIELDS:
            raise ValueError(f"Item {number}: give only item_code, qty and warehouse.")
        qty = raw.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int | float) or not 0 < qty <= QTY_MAX:
            raise ValueError(f"Item {number}: `qty` must be a positive number.")
        lines.append({
            "item_code": _name(raw.get("item_code"), f"item {number} item_code", required=True),
            "qty": qty,
            "warehouse": _name(raw.get("warehouse"), f"item {number} warehouse", required=True),
        })
    out["items"] = lines
    return out


def check_fields(doctype: Any, fields: Any) -> dict[str, Any] | str:
    """``fields`` as they will be stored and sent, or why they can't be.
    Shape only: oe_connector checks what they name (``draft_check``)."""
    if doctype not in DRAFT_TOOLS:
        return f"`doctype` must be one of {', '.join(sorted(DRAFT_TOOLS))}; nothing else can be drafted."
    if not isinstance(fields, dict):
        return "`fields` must be an object."
    try:
        return _todo(fields) if doctype == "ToDo" else _material_request(fields)
    except ValueError as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Who may propose one
# ---------------------------------------------------------------------------

def drafts_on() -> bool:
    """``ERPNEXT_DRAFTS_ENABLED`` with per-person access on, and ERPNext
    connected to the running gateway."""
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_SERVER, erpnext_drafts_on

    return erpnext_drafts_on(get_settings()) and ERPNEXT_SERVER in gateway_server_names(get_active_gateway())


def offer_refusal(session: Any) -> str | None:
    """Why this turn may not propose a draft, or None. In 4a: the principal,
    on a surface that proved it, on their own turn (not an email, which
    quotes mail; not ERPNext's panel; not a run nobody is watching; not a
    turn private to them). Fails closed."""
    from openexecutive.orchestrator.content_trust import principal_speaking
    from openexecutive.orchestrator.schedule_tools import erpnext_panel_turn

    try:
        if not drafts_on():
            return "ERPNext drafts are not turned on here."
        if session is None or getattr(session, "unattended", False) is True:
            return "ERPNext drafts are proposed only in a conversation with the person, never in a run nobody is watching."
        if erpnext_panel_turn(session):
            return "ERPNext drafts can't be proposed from ERPNext's panel yet; the person can ask in the OpenExecutive app."
        if getattr(session, "private_to_principal", False) is True or str(getattr(session, "origin_channel", "") or "") == "email":
            return "ERPNext drafts are proposed only when the owner asks in a chat, never from an email."
        if not principal_speaking(session):
            return "For now only the account owner can have ERPNext drafts prepared."
    except Exception:
        logger.warning("erpnext_drafts: couldn't tell who is asking — refusing", exc_info=True)
        return "Who is asking could not be checked, so no draft can be proposed right now."
    return None


def offered(session: Any) -> bool:
    """Whether this turn is offered ``propose_erpnext_draft`` at all."""
    return offer_refusal(session) is None


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

def _payload(card: Any) -> dict[str, Any]:
    try:
        payload = json.loads(card.proposed_payload_json)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _created_at(card: Any) -> datetime | None:
    try:
        created = datetime.fromisoformat(card.created_at)
    except (TypeError, ValueError):
        return None
    return created if created.tzinfo is not None else created.replace(tzinfo=UTC)


def _live(card: Any, now: datetime) -> bool:
    created = _created_at(card)
    return created is not None and now - created <= CARD_TTL


# Cards an approval in this process is carrying out right now. The API runs
# as one process (docker/Dockerfile, make dev), so a draft card left
# `executing` and not in here was abandoned mid-approval: a cancelled
# request or a restart. As inbox.SENDING is for reply cards.
_APPROVING: set[int] = set()


def _tidy(person_id: int, now: datetime) -> None:
    """Put back ``person_id``'s draft cards an approval abandoned, and close
    the ones past ``CARD_TTL``, so neither lingers in the ledger. Putting one
    back is safe even if ERPNext made the draft: approving again sends the
    same card key, and oe_connector returns the draft it already made."""
    from openexecutive.memory.decision_ledger import (
        STATUS_EXECUTING,
        STATUS_PROPOSED,
        close_externally,
        list_instances,
        release_claim,
    )

    for card in list_instances(DECISION_CLASS, status=STATUS_EXECUTING, approver_person_id=person_id, limit=1000):
        if card.id not in _APPROVING and release_claim(card.id):
            logger.warning("erpnext_drafts: card %d was left mid-approval; it waits again", card.id)
    for card in list_instances(DECISION_CLASS, status=STATUS_PROPOSED, approver_person_id=person_id, limit=1000):
        if not _live(card, now):
            close_externally(card.id, reason="expired")


def open_cards(person_id: int, *, now: datetime | None = None) -> list[Any]:
    """``person_id``'s draft cards still waiting, newest first (``_tidy``
    first, so an abandoned card is back and an expired one is gone)."""
    from openexecutive.memory.decision_ledger import STATUS_PROPOSED, list_instances

    at = now or datetime.now(UTC)
    try:
        _tidy(person_id, at)
    except Exception:
        logger.warning("erpnext_drafts: couldn't tidy the draft cards", exc_info=True)
    found = list_instances(DECISION_CLASS, status=STATUS_PROPOSED, approver_person_id=person_id, limit=1000)
    return sorted((c for c in found if _live(c, at)), key=lambda c: c.created_at, reverse=True)


def _shown(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        # The number as sent, not rounded: 0.004 must not read as 0.
        return format(value, ".15g")
    if isinstance(value, int):
        return str(value)
    return _one_line(value)


def card_view(doctype: str, summary: Any, fields: dict[str, Any]) -> dict[str, Any]:
    """What the card shows: what approval will send (the stored ``fields``,
    and a Material Request's stored lines), as plain text. ERPNext's own
    reading (``draft_check``'s summary) fills only what the fields leave to
    it (a default it will apply) and the extras: a ToDo's reference, the
    estimated value, the items it couldn't price."""
    got: dict[str, Any] = summary if isinstance(summary, dict) else {}
    shown: list[dict[str, str]] = []
    lines: list[dict[str, str]] = []
    notes: list[str] = []

    def add(label: str, *keys: str) -> None:
        for key in keys:
            value = fields.get(key)
            if value in (None, "", []):
                value = got.get(key)
            if value not in (None, "", []):
                shown.append({"label": label, "value": _shown(value)})
                return

    if doctype == "ToDo":
        add("Assigned to", "allocated_to")
        # The stored description in full, as approval sends it: up to
        # TEXT_MAX, longer than SHOWN_MAX, so a cut here would hide text the
        # assignee receives under the approver's name. Already plain()ed.
        if fields.get("description"):
            shown.append({"label": "What", "value": str(fields["description"])})
        add("Priority", "priority")
        add("Due", "date")
        reference = got.get("reference")
        if reference in (None, "") and fields.get("reference_type"):
            reference = f"{fields['reference_type']} {fields['reference_name']}"
        if reference not in (None, ""):
            shown.append({"label": "On", "value": _one_line(reference)})
    else:
        add("Company", "company")
        add("Type", "material_request_type")
        add("Needed by", "schedule_date")
        # The stored lines, every one approval inserts; never ERPNext's list,
        # which could merge or leave one out.
        for row in (fields.get("items") or [])[:MAX_LINES]:
            if isinstance(row, dict):
                lines.append({
                    "item_code": _one_line(row.get("item_code"), NAME_MAX),
                    "qty": _shown(row.get("qty")),
                    "warehouse": _one_line(row.get("warehouse"), NAME_MAX),
                })
        value = got.get("estimated_value")
        currency = got.get("currency")
        if isinstance(value, int | float) and not isinstance(value, bool):
            code = currency if isinstance(currency, str) and _CURRENCY_RE.fullmatch(currency) else ""
            shown.append({"label": "Estimated value", "value": f"{value:,.2f} {code}".strip()})
        unpriced = got.get("unpriced_items")
        if isinstance(unpriced, list) and unpriced:
            names = ", ".join(_one_line(u, NAME_MAX) for u in unpriced[:MAX_LINES])
            notes.append(f"No valuation or last purchase rate in ERPNext, so not in the estimate: {names}")
    return {"fields": shown, "lines": lines, "notes": notes}


def _digest(person_id: int, doctype: str, fields: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps([person_id, doctype, fields], sort_keys=True).encode()
    ).hexdigest()[:32]


def propose(
    person: Any, doctype: str, fields: dict[str, Any], view: dict[str, Any], why: str,
    *, asked_by: str, session_id: str | None, waiting: list[Any] | None = None,
) -> tuple[int, bool]:
    """Store a card for ``person`` (``fields`` already checked); returns its
    decision id and whether it is new. The same draft still waiting is that
    card, not a new one. ``waiting``: ``open_cards(person.id)``, if read."""
    from openexecutive.memory.decision_ledger import create_decision_instance

    digest = _digest(person.id, doctype, fields)
    for card in open_cards(person.id) if waiting is None else waiting:
        if _payload(card).get("digest") == digest:
            return int(card.id), False
    card_key = f"oe:{DECISION_CLASS}:{uuid.uuid4().hex}"
    payload = {
        # A card is its person's alone (approver_only hides the class); this
        # keeps every reader that checks the payload in step.
        "private": True,
        "person_id": person.id,
        "why": _one_line(why, WHY_MAX),
        "digest": digest,
        "doctype": doctype,
        "tool": DRAFT_TOOLS[doctype],
        "fields": fields,
        "asked_by": asked_by,
        "card_key": card_key,
        "view": view,
        "attempts": 0,
    }
    return create_decision_instance(
        decision_class=DECISION_CLASS,
        department="executive",
        originating_session_id=session_id,
        proposed_payload=payload,
        idempotency_key=card_key,
        gate_mode="propose",
        approver_person_id=person.id,
        confidence=None,
    ), True


def cards(person: Any) -> list[DraftCard]:
    """``person``'s waiting draft cards, for ``GET /erpnext/drafts``."""
    out: list[DraftCard] = []
    for card in open_cards(person.id):
        payload = _payload(card)
        stored = payload.get("view")
        view: dict[str, Any] = stored if isinstance(stored, dict) else {}
        created = _created_at(card)
        out.append(DraftCard(
            decision_id=card.id,
            created_at=card.created_at,
            expires_at=(created + CARD_TTL).isoformat() if created else "",
            why=str(payload.get("why") or ""),
            doctype=str(payload.get("doctype") or ""),
            fields=[f for f in view.get("fields") or [] if isinstance(f, dict)],
            lines=[ln for ln in view.get("lines") or [] if isinstance(ln, dict)],
            notes=[str(n) for n in view.get("notes") or []],
            last_error=str(payload.get("last_error") or ""),
        ))
    return out


def audit(event: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    person_id = details.get("person_id")
    try:
        log_event(
            event, summary, actor="executive", details=details, private=True,
            private_to_person=person_id if isinstance(person_id, int) else None,
        )
    except Exception:
        logger.warning("erpnext_drafts: couldn't write the audit row", exc_info=True)


def _result(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _refusal_text(raw: Any) -> str:
    """What ERPNext (or the gateway) said when it refused, as one plain line."""
    parsed = _result(raw)
    said = parsed.get("error") or parsed.get("message") if parsed else raw
    return _one_line(said or "no reason given", SHOWN_MAX)


async def _call(tool: str, arguments: dict[str, Any]) -> str:
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway

    gateway = get_active_gateway()
    if gateway is None:
        return _error("ERPNext isn't connected right now.")
    return await asyncio.wait_for(
        gateway.call_tool({"name": tool, "arguments": arguments}), timeout=CALL_TIMEOUT_S
    )


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

PROPOSE_TOOL: dict[str, Any] = {
    "name": PROPOSE,
    "description": (
        "Prepare an ERPNext draft for the person you are speaking with to approve: a "
        "`ToDo` (a follow-up assigned to someone, optionally on a document) or a "
        "`Material Request` (a restock). ERPNext checks it as them, and they get a card "
        "on Today in the OpenExecutive web app (and under this reply when they are in "
        "its chat, but not in Slack, Telegram or Discord), showing exactly what will be "
        "written; nothing is "
        "created until they tap Approve, and then only an unsubmitted draft they finish "
        "in ERPNext. Use it only when they ask for the draft in their own words. ToDo "
        "fields: description, allocated_to (the assignee's ERPNext user email), optional "
        "priority (Low/Medium/High), date (YYYY-MM-DD), reference_type and "
        "reference_name (e.g. Sales Invoice, ACC-SINV-2026-00042). Material Request "
        "fields: company, schedule_date (YYYY-MM-DD), items (item_code, qty, warehouse; "
        "at most 20), optional material_request_type (Purchase or Material Transfer). "
        "Tell them the card is waiting; never say the draft exists."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "why": {
                "type": "string",
                "description": "One line on what this is for, e.g. 'Restock ITEM-1, below its reorder level'.",
            },
            "doctype": {"type": "string", "enum": sorted(DRAFT_TOOLS)},
            "fields": {"type": "object", "description": "The draft's fields, as listed above for its doctype."},
        },
        "required": ["why", "doctype", "fields"],
    },
}


async def handle_propose(tool_input: dict[str, Any]) -> str:
    from openexecutive.delegation.lockdown import outside_reach_refusal
    from openexecutive.delegation.settings import turn_delegation
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.orchestrator.content_trust import wrap_untrusted
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.people.store import find_principal_person

    refused = outside_reach_refusal(PROPOSE)
    if refused is not None:
        return refused
    session = current_session.get()
    why_not = offer_refusal(session)
    if why_not is not None:
        return _error(f"{PROPOSE} is not available here: {why_not} Say so; do not retry.")
    pinned = turn_delegation(session)
    if pinned is None:
        return _error(f"{PROPOSE} is not available outside a chat turn.")
    if pinned.erpnext_drafts >= CARDS_PER_TURN:
        return _error(f"That's {CARDS_PER_TURN} draft cards this turn. Ask them to approve those first.")
    fields = check_fields(tool_input.get("doctype"), tool_input.get("fields"))
    if isinstance(fields, str):
        return _error(fields)
    doctype = str(tool_input["doctype"])
    try:
        person = find_principal_person()
    except Exception:
        logger.warning("erpnext_drafts: couldn't read the principal", exc_info=True)
        person = None
    email = (getattr(person, "email", None) or "").strip().lower()
    if person is None or person.id is None or "@" not in email:
        return _error(
            "A draft is made as the owner's own ERPNext user, found by the email on their "
            "People entry, and it has none. Say adding it fixes this."
        )
    try:
        waiting = open_cards(person.id)
        if len(waiting) >= OPEN_CARDS_MAX:
            return _error(f"They already have {OPEN_CARDS_MAX} draft cards waiting. Ask them to clear some first.")
    except Exception:
        logger.warning("erpnext_drafts: couldn't count the open cards", exc_info=True)
        return _error("Couldn't leave the card just now. Try again in a moment.")
    check_args = {"doctype": doctype, "fields": fields}
    try:
        with erpnext_caller.draft_check_run(check_args):
            raw = await _call(erpnext_caller.DRAFT_CHECK_TOOL, check_args)
    except Exception:
        logger.warning("erpnext_drafts: draft_check failed", exc_info=True)
        return _error("ERPNext didn't answer the check, so no card was left. Try again in a moment.")
    checked = _result(raw)
    if checked.get("ok") is not True:
        said = wrap_untrusted(_refusal_text(raw), source="erpnext", author="draft_check")
        return _error(
            "ERPNext did not accept this draft, so no card was left. Tell them why, in "
            f"your own words, and fix it only with what they tell you. ERPNext said:\n{said}"
        )
    view = card_view(doctype, checked.get("summary"), fields)
    try:
        decision_id, new = propose(
            person, doctype, fields, view, str(tool_input.get("why") or ""),
            asked_by=email, session_id=getattr(session, "session_id", None), waiting=waiting,
        )
    except Exception:
        logger.warning("erpnext_drafts: couldn't store the card", exc_info=True)
        return _error("Couldn't leave the card just now. Try again in a moment.")
    # Only a card actually left counts towards the turn's cards and the audit:
    # the same draft again is the card already waiting.
    if new:
        pinned.erpnext_drafts += 1
        audit("erpnext_draft_proposed", f"Left person {person.id} an ERPNext {doctype} draft card", {
            "person_id": person.id, "decision_id": decision_id, "doctype": doctype,
        })
    return json.dumps({
        "status": "waiting_for_approval",
        "decision_id": decision_id,
        "doctype": doctype,
        "card": wrap_untrusted(json.dumps(view, ensure_ascii=False), source="erpnext", author="draft_check"),
        "note": (
            ("" if new else "This is the card already waiting for the same draft. ")
            + "Nothing is in ERPNext yet. The card is on Today in the OpenExecutive web app, "
            "and under this reply in its chat, for them to approve; it then becomes an "
            "unsubmitted draft they review and submit in ERPNext. Outside the web app, say "
            "where to find it."
        ),
    })


ERPNEXT_DRAFT_TOOLS: list[dict[str, Any]] = [PROPOSE_TOOL]
ERPNEXT_DRAFT_TOOL_HANDLERS: dict[str, Any] = {PROPOSE: handle_propose}


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------

def _desk_link(value: Any) -> str:
    """ERPNext's desk link for the draft, when it is one (``/desk/…`` from
    Frappe 16 on, ``/app/…`` before, over http or https); else "" and the
    card names the draft only."""
    from urllib.parse import urlsplit

    if not isinstance(value, str) or len(value) > 500 or any(c.isspace() for c in value):
        return ""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.path.startswith(("/desk/", "/app/")):
        return ""
    return value


async def approve(instance: Any, *, caller: Any, resolver: int | None) -> dict[str, Any]:
    """Make ``instance``'s draft in ERPNext for its person, once, exactly as
    stored. Returns what was made. Raises ``ApproveRefused``."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.verified import NOT_YOURS, caller_refusal
    from openexecutive.memory.decision_ledger import claim_for_execution
    from openexecutive.people.store import get_person

    payload = _payload(instance)
    person_id = payload.get("person_id")
    person = get_person(person_id) if isinstance(person_id, int) and not isinstance(person_id, bool) else None
    if person is None or person.id is None or person.archived or person.id != instance.approver_person_id:
        raise ApproveRefused(404, "gone", "This card is no longer there.")
    email = normalize_email(person.email or "")
    refused = caller_refusal(caller, email)
    if refused == NOT_YOURS:
        raise ApproveRefused(403, "not_yours", "Only the person this card is for can approve it.")
    if refused is not None:
        raise ApproveRefused(
            409, "caller_signing_required",
            "Approving needs signed sign-ins on this server, so that nobody else can approve it for you.",
        )
    if not person.is_principal:
        raise ApproveRefused(409, "principal_only", "For now only the account owner can approve ERPNext drafts.")
    if not drafts_on():
        raise ApproveRefused(409, "drafts_off", "ERPNext drafts are turned off here, or ERPNext isn't connected.")
    if not _live(instance, datetime.now(UTC)):
        raise ApproveRefused(409, "expired", "This card is more than three days old. Ask again if it's still needed.")
    doctype, fields, key = payload.get("doctype"), payload.get("fields"), payload.get("card_key")
    tool = DRAFT_TOOLS.get(doctype) if isinstance(doctype, str) else None
    rechecked = check_fields(doctype, fields)
    if (
        tool is None or payload.get("tool") != tool or isinstance(rechecked, str) or rechecked != fields
        or not isinstance(key, str) or not _KEY_RE.fullmatch(key) or key != instance.idempotency_key
        or not isinstance(doctype, str)
    ):
        raise ApproveRefused(409, "malformed", "This card can't be carried out. Dismiss it and ask again.")
    if "@" not in email:
        raise ApproveRefused(409, "no_email", "Your People entry has no email, so ERPNext can't tell who you are.")
    if not claim_for_execution(instance.id, resolver_person_id=resolver):
        raise ApproveRefused(409, "already_handled", "This card was already handled.")
    _APPROVING.add(instance.id)
    try:
        return await _carry_out(instance, payload, person, email, tool, rechecked, key)
    finally:
        _APPROVING.discard(instance.id)


async def _carry_out(
    instance: Any, payload: dict[str, Any], person: Any, email: str,
    tool: str, fields: dict[str, Any], key: str,
) -> dict[str, Any]:
    """``approve`` once it holds the claim: the one ERPNext call, then the
    card finished, or back to waiting with why. Raises ``ApproveRefused``."""
    from openexecutive.memory.decision_ledger import (
        STATUS_APPROVED_UNCHANGED,
        STATUS_FAILED,
        finish_execution,
        release_claim,
        update_open_payload,
    )
    from openexecutive.orchestrator import erpnext_caller

    doctype = payload["doctype"]
    asked_by = payload.get("asked_by") if isinstance(payload.get("asked_by"), str) else email
    arguments = {**fields, "idempotency_key": key, "asked_by": asked_by}
    raw: Any = None
    try:
        with erpnext_caller.draft_executor_run(tool, arguments, email):
            raw = await _call(tool, arguments)
    except Exception as exc:
        logger.warning("erpnext_drafts: the draft call for card %d failed (%s)", instance.id, type(exc).__name__)
        raw = _error("ERPNext didn't answer in time; it may or may not have made the draft.")
    except BaseException:
        # Cancelled mid-call (the request went away, the API is stopping):
        # the card waits again rather than staying claimed. Approving again
        # sends the same key, so ERPNext returns the draft if it was made.
        release_claim(instance.id)
        raise
    result = _result(raw)
    name = result.get("name")
    if result.get("ok") is True and result.get("docstatus") == 0 and isinstance(name, str) and _DOC_NAME_RE.fullmatch(name):
        created = {
            "doctype": doctype, "name": name, "url": _desk_link(result.get("url")),
            "already_created": result.get("already_created") is True,
        }
        if not finish_execution(
            instance.id, STATUS_APPROVED_UNCHANGED,
            final_payload={**payload, "created": created}, external_event_id=name,
        ):
            # The claim was lost meanwhile; the card waits again, and approving
            # it once more returns this same draft (already_created).
            logger.warning("erpnext_drafts: card %d's draft was made but the card wasn't closed", instance.id)
        audit("erpnext_draft_created", f"Person {person.id} approved an ERPNext {doctype} draft", {
            "person_id": person.id, "decision_id": instance.id, "doctype": doctype, "name": name,
            "already_created": created["already_created"],
        })
        return created
    reason = _refusal_text(raw)
    attempts = int(payload.get("attempts") or 0) + 1
    audit("erpnext_draft_failed", f"Person {person.id}'s ERPNext {doctype} draft wasn't made", {
        "person_id": person.id, "decision_id": instance.id, "doctype": doctype, "attempts": attempts,
    })
    if attempts >= ATTEMPTS_MAX:
        finish_execution(instance.id, STATUS_FAILED, final_payload={**payload, "attempts": attempts, "last_error": reason})
        raise ApproveRefused(502, "draft_failed", f"ERPNext didn't make the draft: {reason} This card has been tried {attempts} times, so it is closed.")
    # Back to waiting, with the reason: approving again sends the same card
    # key, so ERPNext returns the draft if it was made after all. The reason
    # and count are written while the claim is still held, then released, so
    # a second tap can't claim it in between and the count is never lost.
    if not update_open_payload(
        instance.id, {**payload, "attempts": attempts, "last_error": reason},
        expected_json=instance.proposed_payload_json, while_executing=True,
    ):
        logger.warning("erpnext_drafts: couldn't note card %d's failed attempt", instance.id)
    release_claim(instance.id)
    raise ApproveRefused(
        502, "draft_failed",
        f"ERPNext didn't make the draft: {reason} You can approve it again; it won't make a second one.",
    )


async def dismissed(instance: Any) -> None:
    """After a card was dismissed: the audit row (nothing was written)."""
    payload = _payload(instance)
    person_id = payload.get("person_id")
    audit("erpnext_draft_dismissed", f"Person {person_id} dismissed an ERPNext draft card", {
        "person_id": person_id, "decision_id": instance.id, "doctype": payload.get("doctype"),
    })

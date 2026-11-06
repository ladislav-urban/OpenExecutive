"""ERPNext drafts a person approves: ``propose_erpnext_draft`` and its cards.

ERPNext phase 4 (design: oe_connector's ``docs/erpnext-integration.md``,
"Writes come last", and ``docs/finished_work/erpnext-phase4-plan.md``). The Executive can
prepare ERPNext documents as unsubmitted drafts: in 4a a ToDo (a follow-up
assigned to someone) and a Material Request (a restock); in 4b a Quotation
(to a customer) and a Purchase Order (to a supplier), which ERPNext prices
(``PRICED``). An install offers only the DocTypes ``ERPNEXT_DRAFT_DOCTYPES``
lists (``enabled_doctypes``), by default 4a's two. Nothing is written until a
person taps Approve on a card that shows exactly what will be written, and
then once, with no model call. A person finishes every document in ERPNext:
nothing here submits, cancels, amends, deletes or sends.

**Propose** (``handle_propose``). The model gives the DocType and its fields.
They are checked for shape here (``check_fields``: only the fields
oe_connector takes, the right types and lengths), then by oe_connector itself,
as the asking person, without saving (``erpnext__draft_check``, inside
``erpnext_caller.draft_check_run``, the only way the gateway lets it through
from a turn). What it returns fills in the card (``card_view``), as plain
text labelled as ERPNext's. Offered only where ERPNext reads are, to the
person speaking for themselves (``offer_refusal``, ``_proposer``): the
principal, and with ``ERPNEXT_DRAFTS_TEAMMATES`` (4b) a verified teammate in
a conversation nobody else reads; never on an unattended run, a turn private
to the principal, an email turn, ERPNext's own panel or a turn that read
their mail (``delegation.lockdown``).

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

**Only what the card showed.** ``draft_check`` returns a fingerprint of the
draft it read; the card keeps it and the approval sends it back
(``expected_fingerprint``). oe_connector builds the draft again then, and
makes nothing if it now comes out differently (a source document, a price
or a rate changed since): that card is closed with the reason
(``CHANGED_SINCE_PROPOSED``), and asking again leaves a card with the draft
as it is now. Asking again while the card still waits, when ERPNext now reads
it differently, closes that card and leaves a new one: a card is never
changed under a screen that shows it.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

DECISION_CLASS = "erpnext_draft"
PROPOSE = "propose_erpnext_draft"

# What may be drafted, and the oe_connector tool that inserts each. An
# install offers only those ERPNEXT_DRAFT_DOCTYPES lists (enabled_doctypes).
DRAFT_TOOLS: dict[str, str] = {
    "ToDo": "erpnext__draft_todo",
    "Material Request": "erpnext__draft_material_request",
    # Phase 4b: they face a customer or commit money once submitted.
    "Quotation": "erpnext__draft_quotation",
    "Purchase Order": "erpnext__draft_purchase_order",
}
# The drafts ERPNext prices: its draft_check returns each line's rate and
# amount and the totals (a missing rate comes from its price list).
PRICED: frozenset[str] = frozenset({"Quotation", "Purchase Order"})

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
# oe_connector's own bound on a line's rate (drafts._priced_lines).
RATE_MAX = 1_000_000_000
# On a card asked for in ERPNext's panel, whose word named the person.
PANEL_NOTE = (
    "Asked for in ERPNext's panel. Approve it only if you asked for it there; "
    "otherwise dismiss it."
)
# What the card shows of any one ERPNext value, and of a failure.
SHOWN_MAX = 300

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# oe_connector's own key rule (drafts._KEY_RE).
_KEY_RE = re.compile(r"[A-Za-z0-9_:.-]{8,140}")
# A draft's name as ERPNext gives it (MAT-MR-2026-00012, a ToDo's hash).
_DOC_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,139}")
# draft_check's fingerprint of the draft a card shows (oe_connector's
# drafts.fingerprint), and how oe_connector's refusal of a draft that no
# longer matches it begins (drafts.CHANGED_SINCE_PROPOSED): keep both in step.
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
CHANGED_SINCE_PROPOSED = "This draft changed since its card was proposed"
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_TODO_FIELDS = frozenset({"description", "allocated_to", "priority", "date", "reference_type", "reference_name"})
_MR_FIELDS = frozenset({"company", "material_request_type", "schedule_date", "items"})
_MR_LINE_FIELDS = frozenset({"item_code", "qty", "warehouse"})
_QUOTATION_FIELDS = frozenset({"company", "customer", "valid_till", "items"})
_PO_FIELDS = frozenset({"company", "supplier", "schedule_date", "items", "material_request", "supplier_quotation"})
_PRICED_LINE_FIELDS = frozenset({"item_code", "qty", "rate", "warehouse"})


def enabled_doctypes() -> tuple[str, ...]:
    """The DocTypes this install drafts (``connected_systems.erpnext_draft_doctypes``)."""
    from openexecutive.config import get_settings
    from openexecutive.prompts.connected_systems import erpnext_draft_doctypes

    return erpnext_draft_doctypes(get_settings())


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
    # Where it was asked for when that was not OpenExecutive itself:
    # "erpnext_panel" (ERPNext's word named the person), else "".
    asked_in: str = ""


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


def _only(fields: dict[str, Any], allowed: frozenset[str], doctype: str) -> None:
    extra = set(fields) - allowed
    if extra:
        raise ValueError(f"A {doctype} takes only {', '.join(sorted(allowed))}; not {', '.join(sorted(extra))}.")


def _lines(items: Any, *, priced: bool, warehouse_required: bool) -> list[dict[str, Any]]:
    """A draft's item lines: item_code, qty and a warehouse, and on a priced
    draft an optional rate (left out, ERPNext's price list sets it)."""
    allowed = _PRICED_LINE_FIELDS if priced else _MR_LINE_FIELDS
    listed = "item_code, qty, rate and warehouse" if priced else "item_code, qty and warehouse"
    if not isinstance(items, list) or not items:
        raise ValueError(f"`items` must be a list of {{{', '.join(sorted(allowed))}}}.")
    if len(items) > MAX_LINES:
        raise ValueError(f"At most {MAX_LINES} items on one draft.")
    lines: list[dict[str, Any]] = []
    for number, raw in enumerate(items, start=1):
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError(f"Item {number}: give only {listed}.")
        qty = raw.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int | float) or not 0 < qty <= QTY_MAX:
            raise ValueError(f"Item {number}: `qty` must be a positive number.")
        line: dict[str, Any] = {
            "item_code": _name(raw.get("item_code"), f"item {number} item_code", required=True),
            "qty": qty,
        }
        rate = raw.get("rate")
        if rate is not None:
            if isinstance(rate, bool) or not isinstance(rate, int | float) or not 0 <= rate <= RATE_MAX:
                raise ValueError(f"Item {number}: `rate` must be a number of at least 0, or left out for the price list.")
            line["rate"] = rate
        warehouse = _name(raw.get("warehouse"), f"item {number} warehouse", required=warehouse_required)
        if warehouse is not None:
            line["warehouse"] = warehouse
        lines.append(line)
    return lines


def _material_request(fields: dict[str, Any]) -> dict[str, Any]:
    _only(fields, _MR_FIELDS, "Material Request")
    out: dict[str, Any] = {
        "company": _name(fields.get("company"), "company", required=True),
        "schedule_date": _date(fields.get("schedule_date"), "schedule_date", required=True),
    }
    kind = fields.get("material_request_type")
    if kind not in (None, ""):
        if kind not in ("Purchase", "Material Transfer"):
            raise ValueError("`material_request_type` must be Purchase or Material Transfer.")
        out["material_request_type"] = kind
    out["items"] = _lines(fields.get("items"), priced=False, warehouse_required=True)
    return out


def _quotation(fields: dict[str, Any]) -> dict[str, Any]:
    _only(fields, _QUOTATION_FIELDS, "Quotation")
    out: dict[str, Any] = {
        "company": _name(fields.get("company"), "company", required=True),
        "customer": _name(fields.get("customer"), "customer", required=True),
    }
    if (valid_till := _date(fields.get("valid_till"), "valid_till", required=False)) is not None:
        out["valid_till"] = valid_till
    out["items"] = _lines(fields.get("items"), priced=True, warehouse_required=False)
    return out


def _purchase_order(fields: dict[str, Any]) -> dict[str, Any]:
    """From lines (supplier, schedule_date, items with a warehouse), or from
    a submitted Material Request (with the supplier to order from) or
    Supplier Quotation (its own supplier), whose lines ERPNext's own mapper
    brings over: then no items are given."""
    _only(fields, _PO_FIELDS, "Purchase Order")
    out: dict[str, Any] = {"company": _name(fields.get("company"), "company", required=True)}
    material_request = _name(fields.get("material_request"), "material_request", required=False)
    supplier_quotation = _name(fields.get("supplier_quotation"), "supplier_quotation", required=False)
    if material_request and supplier_quotation:
        raise ValueError("Give a `material_request` or a `supplier_quotation`, not both.")
    source = material_request or supplier_quotation
    if source and fields.get("items") not in (None, []):
        raise ValueError("With a `material_request` or `supplier_quotation`, ERPNext takes the lines from it: leave out `items`.")
    supplier = _name(fields.get("supplier"), "supplier", required=not supplier_quotation)
    if supplier is not None:
        out["supplier"] = supplier
    schedule_date = _date(fields.get("schedule_date"), "schedule_date", required=not source)
    if schedule_date is not None:
        out["schedule_date"] = schedule_date
    if material_request:
        out["material_request"] = material_request
    elif supplier_quotation:
        out["supplier_quotation"] = supplier_quotation
    else:
        out["items"] = _lines(fields.get("items"), priced=True, warehouse_required=True)
    return out


_SHAPES = {
    "ToDo": _todo,
    "Material Request": _material_request,
    "Quotation": _quotation,
    "Purchase Order": _purchase_order,
}


def check_fields(doctype: Any, fields: Any) -> dict[str, Any] | str:
    """``fields`` as they will be stored and sent, or why they can't be.
    Shape only: oe_connector checks what they name (``draft_check``)."""
    enabled = enabled_doctypes()
    if doctype not in enabled:
        return f"`doctype` must be one of {', '.join(enabled)}; nothing else can be drafted here."
    if not isinstance(fields, dict):
        return "`fields` must be an object."
    try:
        return _SHAPES[doctype](fields)
    except ValueError as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Who may propose one
# ---------------------------------------------------------------------------

def drafts_on() -> bool:
    """``ERPNEXT_DRAFTS_ENABLED`` with per-person access on and a DocType
    listed, and ERPNext connected to the running gateway."""
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_SERVER, erpnext_drafts_on

    return erpnext_drafts_on(get_settings()) and ERPNEXT_SERVER in gateway_server_names(get_active_gateway())


def teammates_on() -> bool:
    """``ERPNEXT_DRAFTS_TEAMMATES``: verified teammates may have drafts too."""
    from openexecutive.config import get_settings

    return bool(get_settings().erpnext_drafts_teammates)


def _proposer(session: Any) -> tuple[Any, str] | str:
    """Who this turn's draft is for, and their ERPNext email: the person
    ERPNext would answer on this turn as their own user
    (``erpnext_caller.caller_for``), when that is the principal or, with
    ``teammates_on``, a verified teammate (which ``caller_for`` already holds
    to a conversation nobody else reads, and in the web chat to signed
    sign-ins). In ERPNext's panel (4b) it is the team member whose People
    email is the ERPNext user oe_connector signed: ERPNext's word, never
    proof of who is speaking, which is why a card asked for there is only
    approved in the OpenExecutive app, signed in (``approve``). Otherwise why
    not. Raises if a lookup fails."""
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.orchestrator.content_trust import principal_speaking
    from openexecutive.orchestrator.people_tools import teammate_on_verified_surface
    from openexecutive.orchestrator.schedule_tools import erpnext_panel_turn, host_turn
    from openexecutive.people.store import find_person_by_email, find_principal_person, get_person

    panel_person: Any = None
    host_person: Any = None
    if erpnext_panel_turn(session):
        panel_person = find_person_by_email(str(session.erpnext_panel_email).strip().lower())
        if panel_person is None or not panel_person.active or panel_person.kind != "team":
            return "This ERPNext user is not on the team in OpenExecutive, so no draft can be proposed for them."
        principal = bool(panel_person.is_principal)
    elif host_turn(session):
        # ERPNext's desk (114.a): the person signed in to this app, whom
        # caller_for below holds to a signed sign-in and a private
        # conversation (erpnext_caller._host_caller). The page names no one.
        pid = getattr(session, "caller_person_id", None)
        host_person = get_person(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None
        if host_person is None or not host_person.active or host_person.kind != "team":
            return "Who is asking could not be matched to someone on the team, so no draft can be proposed."
        principal = bool(host_person.is_principal)
    else:
        principal = principal_speaking(session)
    if not principal and not teammates_on():
        return "For now only the account owner can have ERPNext drafts prepared."
    try:
        caller = erpnext_caller.caller_for(session)
    except erpnext_caller.CallerRefused as refused:
        return refused.message
    if caller.kind != "user" or "@" not in caller.email:
        return "ERPNext drafts are made only as a person's own ERPNext user."
    if panel_person is not None:
        person = panel_person
    elif host_person is not None:
        person = host_person
    else:
        person = find_principal_person() if principal else teammate_on_verified_surface(session)
    email = (getattr(person, "email", None) or "").strip().lower()
    if person is None or getattr(person, "id", None) is None or email != caller.email:
        return "Who is asking could not be matched to their ERPNext user, so no draft can be proposed."
    return person, email


def offer_refusal(session: Any) -> str | None:
    """Why this turn may not propose a draft, or None: the person speaking,
    for themselves (``_proposer``: the principal, or with ``teammates_on`` a
    verified teammate in a private conversation, or the team member ERPNext's
    panel names), on their own turn (not an email, which quotes mail; not a
    run nobody is watching; not a turn private to the principal). Fails
    closed."""
    try:
        if not drafts_on():
            return "ERPNext drafts are not turned on here."
        if session is None or getattr(session, "unattended", False) is True:
            return "ERPNext drafts are proposed only in a conversation with the person, never in a run nobody is watching."
        # A host origin's page (ERPNext's desk: its Conversations screen and
        # the docked panel's chat) may propose, as the person signed in to
        # this app (``_proposer``: erpnext_caller's host caller, signed, on a
        # conversation private to them). It never approves: a card is
        # approved only in this app's own page or frame (``approve``), signed
        # in (the owner's decision 114.a, Nov 6, 2026).
        if getattr(session, "private_to_principal", False) is True or str(getattr(session, "origin_channel", "") or "") == "email":
            return "ERPNext drafts are proposed only when the person asks in a chat, never from an email."
        who = _proposer(session)
        if isinstance(who, str):
            return who
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
    elif doctype in PRICED:
        _priced_view(doctype, got, fields, add, shown, lines, notes)
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


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _money(value: Any) -> str:
    amount = _number(value)
    return "" if amount is None else f"{amount:,.2f}"


def _currency(value: Any) -> str:
    return value if isinstance(value, str) and _CURRENCY_RE.fullmatch(value) else ""


def _priced_view(
    doctype: str, got: dict[str, Any], fields: dict[str, Any], add: Callable[..., None],
    shown: list[dict[str, str]], lines: list[dict[str, str]], notes: list[str],
) -> None:
    """``card_view`` for a Quotation or Purchase Order. Stored lines are shown
    as stored, with ERPNext's rate and amount beside each where its reading
    is that same line. A Purchase Order from a source document has no stored
    lines: approval sends the source, and ERPNext's mapper makes the lines,
    so the card shows the ones it made at the check and says so."""
    add("Company", "company")
    if doctype == "Quotation":
        add("Customer", "customer")
        add("Valid till", "valid_till")
    else:
        add("Supplier", "supplier")
        add("Needed by", "schedule_date")
    source_doctype, source = (
        ("Material Request", fields.get("material_request")) if fields.get("material_request")
        else ("Supplier Quotation", fields.get("supplier_quotation")) if fields.get("supplier_quotation")
        else ("", None)
    )
    if source:
        shown.append({"label": "From", "value": f"{source_doctype} {_one_line(source, NAME_MAX)}"})
    items = got.get("items")
    checked = [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []

    def line(row: dict[str, Any], rate: Any, amount: Any) -> dict[str, str]:
        return {
            "item_code": _one_line(row.get("item_code"), NAME_MAX),
            "qty": _shown(row.get("qty")),
            "warehouse": _one_line(row.get("warehouse"), NAME_MAX),
            "rate": _money(rate),
            "amount": _money(amount),
        }

    if source:
        for row in checked[:MAX_LINES]:
            lines.append(line(row, row.get("rate"), row.get("amount")))
        notes.append(
            f"ERPNext takes these lines from {source_doctype} {_one_line(source, NAME_MAX)}. If what it would "
            "bring over, or its prices, change before you approve, nothing is made and you're asked to look "
            "again."
        )
        if len(checked) > MAX_LINES:
            notes.append(f"And {len(checked) - MAX_LINES} more lines, as ERPNext lists them.")
    else:
        priced_by_erpnext: list[str] = []
        unmatched: list[str] = []
        for index, row in enumerate(fields.get("items") or []):
            if not isinstance(row, dict):
                continue
            match = checked[index] if index < len(checked) else {}
            # ERPNext may give an item's code in its own case.
            same = (
                str(match.get("item_code") or "").casefold() == str(row.get("item_code") or "").casefold()
                and _number(match.get("qty")) == _number(row.get("qty"))
            )
            rate = row.get("rate") if "rate" in row else (match.get("rate") if same else None)
            lines.append(line(row, rate, match.get("amount") if same else None))
            name = _one_line(row.get("item_code"), NAME_MAX)
            if not same:
                unmatched.append(name)
            elif "rate" not in row and _number(match.get("rate")) is not None:
                priced_by_erpnext.append(name)
            elif "rate" in row and _number(match.get("rate")) not in (None, _number(row["rate"])):
                notes.append(f"ERPNext's pricing rules made {name}'s rate {_money(match.get('rate'))}.")
        if priced_by_erpnext:
            notes.append(
                f"Rates from ERPNext's price list at the check, for {', '.join(priced_by_erpnext)}; "
                "if they change before you approve, nothing is made and you're asked to look again."
            )
        if unmatched:
            notes.append(
                f"ERPNext's check didn't give figures for {', '.join(unmatched)}; its rate is "
                "ERPNext's to set (a pricing rule can change a stated one), so check it in ERPNext."
            )
    currency = _currency(got.get("currency"))
    company_currency = _currency(got.get("company_currency"))
    if _number(got.get("grand_total")) is not None:
        shown.append({"label": "Grand total", "value": f"{_money(got['grand_total'])} {currency}".strip()})
        if company_currency and company_currency != currency and _number(got.get("base_grand_total")) is not None:
            shown.append({
                "label": f"In {company_currency}",
                "value": f"{_money(got['base_grand_total'])} {company_currency}",
            })


def _digest(person_id: int, doctype: str, fields: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps([person_id, doctype, fields], sort_keys=True).encode()
    ).hexdigest()[:32]


def fingerprint_of(checked: dict[str, Any]) -> str | None:
    """draft_check's fingerprint of the draft it read, if it gave a sound one."""
    value = checked.get("fingerprint")
    return value if isinstance(value, str) and _FINGERPRINT_RE.fullmatch(value) else None


def propose(
    person: Any, doctype: str, fields: dict[str, Any], view: dict[str, Any], why: str,
    *, asked_by: str, session_id: str | None, waiting: list[Any] | None = None, asked_in: str = "",
    from_alert: int | None = None, fingerprint: str | None = None,
) -> tuple[int, bool, dict[str, Any]]:
    """Store a card for ``person`` (``fields`` already checked); returns its
    decision id, whether it is new and the view it shows. The same draft
    still waiting is that card, not a new one, with the view it was left
    with, unless ERPNext now reads it differently (another ``fingerprint``):
    then that card is closed and a new one left. Never changed in place,
    since a screen still showing the old card would approve the new draft
    (security review); the old card's screen gets "already handled".
    ``waiting``: ``open_cards(person.id)``, if read."""
    from openexecutive.memory.decision_ledger import close_externally, create_decision_instance

    digest = _digest(person.id, doctype, fields)
    for card in open_cards(person.id) if waiting is None else waiting:
        stored = _payload(card)
        if stored.get("digest") == digest:
            shown = stored.get("view")
            # A replacement only once the old card is closed: if another API
            # process claimed or closed it meanwhile, no second card for the
            # same draft; that one answers for it.
            if (
                fingerprint is not None and stored.get("fingerprint") != fingerprint
                and close_externally(card.id, reason="changed")
            ):
                break
            return int(card.id), False, shown if isinstance(shown, dict) else view
            return int(card.id), False, shown if isinstance(shown, dict) else view
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
        **({"asked_in": asked_in} if asked_in else {}),
        # The ERPNext alert whose suggestion this is (erpnext_alert_drafts),
        # so that alert offers no second card.
        **({"from_alert": from_alert} if from_alert is not None else {}),
        "card_key": card_key,
        "view": view,
        # What ERPNext read, sent back on approval so only this draft is made.
        **({"fingerprint": fingerprint} if fingerprint is not None else {}),
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
    ), True, view


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
            asked_in="erpnext_panel" if payload.get("asked_in") == "erpnext_panel" else "",
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

# How each DocType's fields read in the tool's description.
_FIELD_HELP: dict[str, str] = {
    "ToDo": (
        "ToDo fields: description, allocated_to (the assignee's ERPNext user email), "
        "optional priority (Low/Medium/High), date (YYYY-MM-DD), reference_type and "
        "reference_name (e.g. Sales Invoice, ACC-SINV-2026-00042)."
    ),
    "Material Request": (
        "Material Request fields: company, schedule_date (YYYY-MM-DD), items "
        "(item_code, qty, warehouse; at most 20), optional material_request_type "
        "(Purchase or Material Transfer)."
    ),
    "Quotation": (
        "Quotation fields: company, customer, optional valid_till (YYYY-MM-DD), items "
        "(item_code, qty, optional rate and warehouse; at most 20)."
    ),
    "Purchase Order": (
        "Purchase Order fields: company and either supplier, schedule_date "
        "(YYYY-MM-DD) and items (item_code, qty, warehouse, optional rate; at most "
        "20), or a submitted material_request (a purchase request) with the "
        "supplier to order from, or a submitted supplier_quotation, with no items: "
        "ERPNext takes the lines from it (schedule_date optional)."
    ),
}


def propose_tool(doctypes: tuple[str, ...]) -> dict[str, Any]:
    """``propose_erpnext_draft`` for the DocTypes this install drafts.
    Process-constant, so the turn's tool list stays cacheable."""
    priced = (
        "Give a Quotation's or Purchase Order's rate only when they state one; "
        "ERPNext's price list sets the rest. OpenExecutive sends nothing to the "
        "customer or supplier (only ERPNext's own notifications for a new document "
        "might, if the site has any). " if PRICED & set(doctypes) else ""
    )
    return {
        "name": PROPOSE,
        "description": (
            f"Prepare an ERPNext draft for the person you are speaking with to approve: "
            f"{', '.join(f'`{d}`' for d in doctypes)}. ERPNext checks it as them, and "
            "they get a card on Today in the OpenExecutive web app (and under this reply "
            "when they are in its chat, but not in Slack, Telegram or Discord), showing "
            "exactly what will be written; nothing is created until they tap Approve, and "
            "then only an unsubmitted draft they finish in ERPNext. Use it only when they "
            "ask for the draft in their own words. "
            + " ".join(_FIELD_HELP[d] for d in doctypes)
            + " "
            + priced
            + "Tell them the card is waiting; never say the draft exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "why": {
                    "type": "string",
                    "description": "One line on what this is for, e.g. 'Restock ITEM-1, below its reorder level'.",
                },
                "doctype": {"type": "string", "enum": list(doctypes)},
                "fields": {"type": "object", "description": "The draft's fields, as listed above for its doctype."},
            },
            "required": ["why", "doctype", "fields"],
        },
    }


async def handle_propose(tool_input: dict[str, Any]) -> str:
    from openexecutive.delegation.lockdown import outside_reach_refusal
    from openexecutive.delegation.settings import turn_delegation
    from openexecutive.orchestrator.schedule_tools import current_session

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
    # The card is the person speaking's own: the principal's, or a verified
    # teammate's, approved by them alone and made as their ERPNext user.
    try:
        who = _proposer(session)
    except Exception:
        logger.warning("erpnext_drafts: couldn't tell who is asking", exc_info=True)
        who = "Who is asking could not be checked, so no draft can be proposed right now."
    if isinstance(who, str):
        return _error(f"{PROPOSE} is not available here: {who} Say so; do not retry.")
    person, email = who
    full = _cards_full(person.id, doctype, fields)
    if full is not None:
        return _error(full)
    # The turn's slot is taken before the check, which waits on ERPNext, so
    # calls made together in one round can't each pass CARDS_PER_TURN; it is
    # given back below unless a new card is left.
    pinned.erpnext_drafts += 1
    new = False
    try:
        result, new = await _check_and_leave(session, person, email, doctype, fields, str(tool_input.get("why") or ""))
        return result
    finally:
        if not new:
            pinned.erpnext_drafts -= 1


def _cards_full(person_id: int, doctype: str, fields: dict[str, Any]) -> str | None:
    """Why no card can be left for ``person_id`` now, or None: OPEN_CARDS_MAX
    waiting and none of them this same draft."""
    try:
        waiting = open_cards(person_id)
    except Exception:
        logger.warning("erpnext_drafts: couldn't count the open cards", exc_info=True)
        return "Couldn't leave the card just now. Try again in a moment."
    digest = _digest(person_id, doctype, fields)
    if len(waiting) >= OPEN_CARDS_MAX and not any(_payload(c).get("digest") == digest for c in waiting):
        return f"They already have {OPEN_CARDS_MAX} draft cards waiting. Ask them to clear some first."
    return None


async def _check_and_leave(
    session: Any, person: Any, email: str, doctype: str, fields: dict[str, Any], why: str,
) -> tuple[str, bool]:
    """``handle_propose`` once the turn's slot is taken: ERPNext's check as
    the turn's caller, then the card. The tool result, and whether a new
    card was left."""
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.orchestrator.content_trust import wrap_untrusted
    from openexecutive.orchestrator.schedule_tools import erpnext_panel_turn

    check_args = {"doctype": doctype, "fields": fields}
    try:
        with erpnext_caller.draft_check_run(check_args):
            raw = await _call(erpnext_caller.DRAFT_CHECK_TOOL, check_args)
    except Exception:
        logger.warning("erpnext_drafts: draft_check failed", exc_info=True)
        return _error("ERPNext didn't answer the check, so no card was left. Try again in a moment."), False
    checked = _result(raw)
    if checked.get("ok") is not True:
        said = wrap_untrusted(_refusal_text(raw), source="erpnext", author="draft_check")
        return _error(
            "ERPNext did not accept this draft, so no card was left. Tell them why, in "
            f"your own words, and fix it only with what they tell you. ERPNext said:\n{said}"
        ), False
    view = card_view(doctype, checked.get("summary"), fields)
    from_panel = erpnext_panel_turn(session)
    if from_panel:
        view["notes"] = [*view["notes"], PANEL_NOTE]
    # Read again after the check, with no await before the card is stored,
    # so calls made together never pass OPEN_CARDS_MAX.
    full = _cards_full(person.id, doctype, fields)
    if full is not None:
        return _error(full), False
    try:
        decision_id, new, view = propose(
            person, doctype, fields, view, why,
            asked_by=email, session_id=getattr(session, "session_id", None),
            asked_in="erpnext_panel" if from_panel else "", fingerprint=fingerprint_of(checked),
        )
    except Exception:
        logger.warning("erpnext_drafts: couldn't store the card", exc_info=True)
        return _error("Couldn't leave the card just now. Try again in a moment."), False
    if from_panel:
        # The panel's reply names it, so ERPNext can offer to open it.
        from openexecutive.integrations.erpnext_panel import note_panel_card

        note_panel_card(decision_id)
    # Only a card actually left counts towards the turn's cards and the audit:
    # the same draft again is the card already waiting.
    if new:
        audit("erpnext_draft_proposed", f"Left person {person.id} an ERPNext {doctype} draft card", {
            "person_id": person.id, "decision_id": decision_id, "doctype": doctype,
            **({"asked_in": "erpnext_panel"} if from_panel else {}),
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
    }), new


def draft_tools() -> list[dict[str, Any]]:
    """The turn tools a turn ``offered`` drafts gets."""
    return [propose_tool(enabled_doctypes())]


def chip(tool_input: dict[str, Any], parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    """The chat chip for a proposal: only a stored card earns one, and it says
    nothing is in ERPNext yet. The chat shows the card itself under its
    message, by ``decision_id``."""
    if (parsed or {}).get("status") != "waiting_for_approval":
        return None
    doctype = (parsed or {}).get("doctype")
    noun = doctype if doctype in DRAFT_TOOLS else "ERPNext"
    return {
        "summary": f"Waiting for your approval: {noun} draft",
        "link": "/",
        "decision_id": (parsed or {}).get("decision_id"),
    }


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
    if person is None or person.id is None or not person.active or person.id != instance.approver_person_id:
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
    # A teammate's card waits on teammates still being let in: turned off,
    # their cards stay but can't be carried out.
    if not person.is_principal and not (teammates_on() and person.kind == "team"):
        raise ApproveRefused(409, "principal_only", "For now only the account owner can approve ERPNext drafts.")
    if not drafts_on():
        raise ApproveRefused(409, "drafts_off", "ERPNext drafts are turned off here, or ERPNext isn't connected.")
    if payload.get("doctype") in DRAFT_TOOLS and payload.get("doctype") not in enabled_doctypes():
        raise ApproveRefused(409, "drafts_off", f"ERPNext {payload['doctype']} drafts are turned off here.")
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
    # The card was checked as the person who asked: if their People email
    # changed since, it may now name someone else, so it isn't theirs to make.
    asked_by = payload.get("asked_by")
    if not isinstance(asked_by, str) or normalize_email(asked_by) != email:
        raise ApproveRefused(409, "changed", "Your email changed since this card was left. Dismiss it and ask again.")
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
    # Made only if ERPNext still builds the draft the card showed. A card
    # left before fingerprints has none, and is made as before.
    expected = payload.get("fingerprint")
    if isinstance(expected, str) and _FINGERPRINT_RE.fullmatch(expected):
        arguments["expected_fingerprint"] = expected
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
        # The "OE Draft" row's seal in oe_connector's hash chain: kept here too,
        # so the log in ERPNext and this record check each other.
        seal, place = result.get("row_hash"), result.get("chain_index")
        if isinstance(seal, str) and _FINGERPRINT_RE.fullmatch(seal):
            created["row_hash"] = seal
            # Its place too, so a check can tell the newest rows are gone.
            if isinstance(place, int) and not isinstance(place, bool) and place > 0:
                created["chain_index"] = place
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
            **{k: created[k] for k in ("row_hash", "chain_index") if k in created},
        })
        return created
    reason = _refusal_text(raw)
    # Anywhere in it: the MCP layer may put its own words before a tool's error.
    if CHANGED_SINCE_PROPOSED in reason:
        # Nothing was made, and approving again would refuse again: the card
        # is closed, and asking again leaves one with the draft as it is now.
        finish_execution(instance.id, STATUS_FAILED, final_payload={**payload, "last_error": reason})
        audit("erpnext_draft_changed", f"Person {person.id}'s ERPNext {doctype} draft changed since its card", {
            "person_id": person.id, "decision_id": instance.id, "doctype": doctype,
        })
        raise ApproveRefused(
            409, "changed_since_proposed",
            "This draft changed in ERPNext since the card was left (a source document, a price or a "
            "rate), so nothing was made and the card is closed. Ask for it again to see it as it is now.",
        )
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


def recorded_seals() -> dict[str, dict[str, Any]]:
    """Every draft made from a card, as oe_connector's "OE Draft" log should
    hold it: {card key: {"row_hash", "chain_index"}}. The JSON
    ``oe-connector-verify-drafts --expect`` compares with the log, so rows
    removed or rewritten there show (``scripts/erpnext-draft-seals.py``)."""
    from openexecutive.memory.decision_ledger import list_instances

    seals: dict[str, dict[str, Any]] = {}
    for card in list_instances(DECISION_CLASS, limit=100000):
        try:
            created = json.loads(card.final_payload_json or "{}").get("created") or {}
        except (TypeError, ValueError):
            continue
        seal, place = created.get("row_hash"), created.get("chain_index")
        if isinstance(seal, str) and _FINGERPRINT_RE.fullmatch(seal) and isinstance(card.idempotency_key, str):
            seals[card.idempotency_key] = {"row_hash": seal, "chain_index": place if isinstance(place, int) else 0}
    return seals

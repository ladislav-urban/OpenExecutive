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
reason (``last_error``), up to ``ATTEMPTS_MAX`` tries. A card an approval
closes keeps our own code for why (``closed_code``), and the conversation's
later turns are told what became of its cards in fixed words, never the ERP's
(``decided_cards_note``, in the user turn).

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

**A watch job's cards** (``leave_job_card``, ``source: "watch"``): a job's
code builds the card's fields (never a model), ERPNext checks them as the
job's reviewer (``erpnext_caller.draft_check_for_job``), and the card goes
through ``propose`` like any other, for the reviewer, keyed by the job's
finding. Approving one also needs (``_job_approval``): the job registered
and on, the approver its stored and current reviewer (never the principal,
never a demo person), the fields as the job's ``check_fields`` builds them,
a fingerprint, and **separation of duties** whatever ``ERPNEXT_DRAFT_SOD``
says: whoever set the job up (``configured_by_person_id``) never approves its
cards, and a card whose configurer is gone is refused too
(``SOD_JOB_TEXT``, closed as ``sod_refused``). The write tool then gets
``asked_by: "job:<id>"`` and ``configured_by``, the configurer's current
email, so oe_connector applies its own rule as well.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable
from contextvars import ContextVar
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
    # Phase 0 (S2): a payment reminder for an overdue invoice. Submitting it
    # sends nothing; the letter goes out only when a person prints or emails it.
    "Dunning": "erpnext__draft_dunning",
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
    "Asked for in the ERP's panel. Approve it only if you asked for it there; "
    "otherwise dismiss it."
)
# On a card asked for from ERPNext's desk (114.a): signed in, but the turn's
# words came through another app's page, which a script there may have written.
DESK_NOTE = (
    "Asked for from the ERP's desk. Approve it only if you asked for it there; "
    "otherwise dismiss it."
)
# Where a card was asked for, when not in this app's own chat.
ASKED_IN_PANEL = "erpnext_panel"
ASKED_IN_DESK = "erpnext_desk"
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
# How oe_connector's separation-of-duties refusal begins (drafts.SOD_REFUSED).
SOD_REFUSED = "Separation of duties"
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_TODO_FIELDS = frozenset({"description", "allocated_to", "priority", "date", "reference_type", "reference_name"})
_MR_FIELDS = frozenset({"company", "material_request_type", "schedule_date", "items"})
_MR_LINE_FIELDS = frozenset({"item_code", "qty", "warehouse"})
_QUOTATION_FIELDS = frozenset({"company", "customer", "valid_till", "items"})
_PO_FIELDS = frozenset({"company", "supplier", "schedule_date", "items", "material_request", "supplier_quotation"})
_PRICED_LINE_FIELDS = frozenset({"item_code", "qty", "rate", "warehouse"})
_DUNNING_FIELDS = frozenset({"sales_invoice", "dunning_type", "posting_date"})
# How oe_connector refuses a Dunning whose invoice gained an overdue
# installment after the card's check (build_dunning): nothing was made, and
# approving again would refuse again, so the card closes as changed.
_DUNNING_STALE = "posting_date must be after every overdue installment"
# What the card shows of the letter's text: a preview, marked when cut.
LETTER_SHOWN_MAX = 600
# How much of a letter's text is read to make that preview.
LETTER_READ_MAX = 20_000


def enabled_doctypes() -> tuple[str, ...]:
    """The DocTypes this install drafts (``connected_systems.erpnext_draft_doctypes``)."""
    from openexecutive.config import get_settings
    from openexecutive.prompts.connected_systems import erpnext_draft_doctypes

    return erpnext_draft_doctypes(get_settings())


class ApproveRefused(Exception):
    """Why an approval was refused: an HTTP status, a code and what to tell them."""

    def __init__(self, status: int, code: str, message: str) -> None:
        # Shown to the person as it is: in the product's words on an install
        # with a name ("changed in the ERP", never the part's name).
        from openexecutive.prompts.connected_systems import in_product_reply

        message = in_product_reply(message)
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
    # "erpnext_panel" (ERPNext's word named the person), "erpnext_desk"
    # (ERPNext's desk page sent the words, signed in), else "".
    asked_in: str = ""
    # "create" for a new draft; "update" or "comment" for a card that
    # changes or comments on an existing document (erpnext_changes); "credit"
    # for a credit-limit raise (erpnext_credit).
    operation: str = "create"
    # A credit-limit raise ERPNext reads differently now than when the card
    # was left (erpnext_credit.recheck, GET /erpnext/drafts only): approving
    # it is refused.
    changed: bool = False
    # A watch job's card: the job's title (code's), and the reason step's
    # line for it, a model's words shown as "Why now".
    job_title: str = ""
    why_now: str = ""


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


def _dunning(fields: dict[str, Any]) -> dict[str, Any]:
    _only(fields, _DUNNING_FIELDS, "Dunning")
    out: dict[str, Any] = {"sales_invoice": _name(fields.get("sales_invoice"), "sales_invoice", required=True)}
    dunning_type = _name(fields.get("dunning_type"), "dunning_type", required=False)
    if dunning_type:
        out["dunning_type"] = dunning_type
    posting_date = _date(fields.get("posting_date"), "posting_date", required=False)
    if posting_date:
        # A day's grace for ERPNext's own time zone; it refuses a future date itself.
        if posting_date > (datetime.now(UTC) + timedelta(days=1)).date().isoformat():
            raise ValueError("posting_date can't be in the future.")
        out["posting_date"] = posting_date
    return out


def pin_from_check(doctype: str, fields: dict[str, Any], summary: Any) -> dict[str, Any] | str:
    """``fields`` with what ERPNext's check settled that approval must send
    again, or why no card can be left. A Dunning keeps the letter date the
    check used (its summary's ``posting_date``), so a card approved on a
    later day builds the same letter with the same interest. Only the format
    is checked here: ERPNext dates it in its own time zone, and checks the
    date again at approval."""
    if doctype != "Dunning" or fields.get("posting_date"):
        return fields
    got = summary.get("posting_date") if isinstance(summary, dict) else None
    if not isinstance(got, str) or not _DATE_RE.fullmatch(got):
        return "ERPNext didn't say which date the letter has, so no card was left. Try again in a moment."
    try:
        datetime.strptime(got, "%Y-%m-%d")
    except ValueError:
        return "ERPNext gave the letter an impossible date, so no card was left."
    # The same bound approval re-checks (_dunning), so no card is left that
    # approval would refuse.
    if got > (datetime.now(UTC) + timedelta(days=1)).date().isoformat():
        return "ERPNext dated the letter in the future, so no card was left."
    return {**fields, "posting_date": got}


_SHAPES = {
    "ToDo": _todo,
    "Material Request": _material_request,
    "Quotation": _quotation,
    "Purchase Order": _purchase_order,
    "Dunning": _dunning,
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
        # in (decided Nov 6, 2026).
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


def card_live(card: Any, now: datetime | None = None) -> bool:
    """Whether a draft card is within its time (``CARD_TTL``)."""
    return _live(card, now or datetime.now(UTC))


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
    elif doctype == "Dunning":
        _dunning_view(got, fields, add, shown, notes)
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
            notes.append(f"No valuation or last purchase rate in the ERP, so not in the estimate: {names}")
    return {"fields": shown, "lines": lines, "notes": notes}


def _letter(value: Any) -> str:
    """A letter's text as the card shows it: tags out, one line, a preview
    marked when cut."""
    from openexecutive.utils.html_tags import strip_tags

    # A preview needs only the start: a template of any size costs the same.
    raw = str(value or "")[:LETTER_READ_MAX]
    # A paragraph or line break is a space, so its words don't run together.
    # Unambiguous, so it stays linear on any input (a `<br` then a long run
    # of spaces with no `>` can't backtrack).
    spaced = re.sub(r"<\s*(?:/\s*(?:p|div|li|h\d)\s*|br\s*(?:/\s*)?)>", " ", raw, flags=re.IGNORECASE)
    text = " ".join(strip_tags(spaced).split())
    return text if len(text) <= LETTER_SHOWN_MAX else text[:LETTER_SHOWN_MAX].rstrip() + "…"


def _dunning_view(
    got: dict[str, Any], fields: dict[str, Any], add: Callable[..., None],
    shown: list[dict[str, str]], notes: list[str],
) -> None:
    """A Dunning's card: whom it reminds of what, each overdue installment,
    the charges it adds and what the letter says. All of it ERPNext's own
    reading (draft_check's summary) of the stored fields."""
    currency = _currency(got.get("currency"))
    add("Customer", "customer_name", "customer")
    add("Invoice", "sales_invoice")
    add("Reminder type", "dunning_type")
    add("Letter date", "posting_date")
    raw_rows = got.get("rows")
    rows: list[Any] = raw_rows if isinstance(raw_rows, list) else []
    for row in rows[:MAX_LINES]:
        if not isinstance(row, dict):
            continue
        due = _one_line(row.get("due_date"), 20)
        days = row.get("overdue_days")
        days_text = f", {days} days overdue" if isinstance(days, int) and not isinstance(days, bool) else ""
        shown.append({
            "label": f"Due {due}",
            "value": f"{_money(row.get('outstanding'))} {currency} outstanding{days_text}, "
                     f"interest {_money(row.get('interest'))}".replace("  ", " "),
        })
    if len(rows) > MAX_LINES:
        notes.append(f"And {len(rows) - MAX_LINES} more overdue installments.")
    for label, key in (("Fee", "dunning_fee"), ("Interest", "total_interest"),
                       ("Charges it adds", "dunning_amount"), ("The letter asks for", "grand_total")):
        if _number(got.get(key)) is not None:
            shown.append({"label": label, "value": f"{_money(got.get(key))} {currency}".strip()})
    # The letter as the customer would read it: a template's text, shown as
    # a field (never in the app's own notes), tags out, a marked preview.
    for label, key in (("Letter", "body_text"), ("Closing", "closing_text")):
        text = _letter(got.get(key))
        if text:
            shown.append({"label": label, "value": text})


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
            notes.append(f"And {len(checked) - MAX_LINES} more lines, as the ERP lists them.")
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
                notes.append(f"The ERP's pricing rules made {name}'s rate {_money(match.get('rate'))}.")
        if priced_by_erpnext:
            notes.append(
                f"Rates from the ERP's price list at the check, for {', '.join(priced_by_erpnext)}; "
                "if they change before you approve, nothing is made and you're asked to look again."
            )
        if unmatched:
            notes.append(
                f"The ERP's check didn't give figures for {', '.join(unmatched)}; its rate is "
                "the ERP's to set (a pricing rule can change a stated one), so check it in the ERP."
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


def _digest(person_id: int, doctype: str, fields: dict[str, Any], scope: str = "") -> str:
    if doctype == "Dunning":
        # One letter per invoice and type: asking again on a later day is the
        # same draft, whose new fingerprint replaces the waiting card.
        fields = {k: v for k, v in fields.items() if k != "posting_date"}
    # A watch job's cards are their own (scope "watch:<id>"): never the card
    # a person asked for in chat, nor replaced by one.
    keyed: list[Any] = [person_id, doctype, fields, scope] if scope else [person_id, doctype, fields]
    return hashlib.sha256(json.dumps(keyed, sort_keys=True).encode()).hexdigest()[:32]


def _tool_for(operation: str, doctype: str) -> str:
    """The oe_connector tool a card's approval calls: a new draft's own,
    or the update's or comment's."""
    if operation == "create":
        return DRAFT_TOOLS[doctype]
    from openexecutive.integrations.erpnext_changes import OPERATION_TOOLS

    return OPERATION_TOOLS[operation]


def fingerprint_of(checked: dict[str, Any]) -> str | None:
    """draft_check's fingerprint of the draft it read, if it gave a sound one."""
    value = checked.get("fingerprint")
    return value if isinstance(value, str) and _FINGERPRINT_RE.fullmatch(value) else None


def propose(
    person: Any, doctype: str, fields: dict[str, Any], view: dict[str, Any], why: str,
    *, asked_by: str, session_id: str | None, waiting: list[Any] | None = None, asked_in: str = "",
    from_alert: int | None = None, fingerprint: str | None = None,
    operation: str = "create", name: str | None = None, extra: dict[str, Any] | None = None,
    scope: str = "",
) -> tuple[int, bool, dict[str, Any]]:
    """Store a card for ``person`` (``fields`` already checked); returns its
    decision id, whether it is new and the view it shows. The same draft
    still waiting is that card, not a new one, with the view it was left
    with, unless ERPNext now reads it differently (another ``fingerprint``):
    then that card is closed and a new one left. Never changed in place,
    since a screen still showing the old card would approve the new draft
    (security review); the old card's screen gets "already handled".
    ``waiting``: ``open_cards(person.id)``, if read. ``extra``: more of the
    payload (a credit raise's identities, erpnext_credit; a watch job's keys,
    ``leave_job_card``). ``scope``: what else keys the card (a job's id)."""
    from openexecutive.memory.decision_ledger import close_externally, create_decision_instance

    digest = _digest(person.id, doctype, _keyed(operation, name, fields), scope)
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
    card_key = f"oe:{DECISION_CLASS}:{uuid.uuid4().hex}"
    payload = {
        # A card is its person's alone (approver_only hides the class); this
        # keeps every reader that checks the payload in step.
        "private": True,
        "person_id": person.id,
        "why": _one_line(why, WHY_MAX),
        "digest": digest,
        "doctype": doctype,
        "tool": _tool_for(operation, doctype),
        "fields": fields,
        **({"operation": operation, "name": name} if operation != "create" else {}),
        "asked_by": asked_by,
        **({"asked_in": asked_in} if asked_in else {}),
        # The ERPNext alert whose suggestion this is (erpnext_alert_drafts),
        # so that alert offers no second card.
        **({"from_alert": from_alert} if from_alert is not None else {}),
        "card_key": card_key,
        "view": view,
        # What ERPNext read, sent back on approval so only this draft is made.
        **({"fingerprint": fingerprint} if fingerprint is not None else {}),
        **(extra or {}),
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


# How many of a conversation's earlier cards a new card's result names.
EARLIER_CARDS_MAX = 10


def earlier_cards(person_id: int, session_id: Any, *, leaving: int) -> list[dict[str, Any]]:
    """``person_id``'s other draft cards left in this conversation, oldest
    first, each with what became of it (``action_chips.card_outcome``) and,
    once approved, the draft's name: so a reply speaks of each as it stands,
    never "still waiting" for one approved or closed (S14, Nov 7, 2026).
    Cards from other conversations are left out."""
    from openexecutive.memory.decision_ledger import list_instances
    from openexecutive.orchestrator.action_chips import card_outcome

    if not isinstance(session_id, str) or not session_id:
        return []
    found = [
        c for c in list_instances(DECISION_CLASS, approver_person_id=person_id, limit=1000)
        if c.originating_session_id == session_id and c.id != leaving
    ]
    out: list[dict[str, Any]] = []
    for card in sorted(found, key=lambda c: c.id)[-EARLIER_CARDS_MAX:]:
        entry: dict[str, Any] = {
            "decision_id": card.id,
            "doctype": str(_payload(card).get("doctype") or ""),
            "status": card_outcome(card),
        }
        if entry["status"] == "approved":
            try:
                final = json.loads(card.final_payload_json or "{}")
            except (TypeError, ValueError):
                final = {}
            made = final.get("created") if isinstance(final, dict) else None
            name = made.get("name") if isinstance(made, dict) else None
            if isinstance(name, str) and _DOC_NAME_RE.fullmatch(name):
                entry["name"] = name
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# What became of this conversation's cards, for its later turns
# ---------------------------------------------------------------------------

# Why a card closed on approval, as stored in its final payload. Our own codes,
# never the ERP's words; the note below reads them through _CLOSED_SAYS only.
CLOSED_CHANGED = "changed_since_proposed"
CLOSED_NO_ANSWER = "no_answer"
# How far back, and how many, the turn's note looks: a card's own lifetime.
DECIDED_WINDOW = CARD_TTL
DECIDED_MAX = 5
# A draft's name as the note shows it: tighter than _DOC_NAME_RE (no spaces,
# shorter), and still the ERP's text, so it is wrapped as untrusted.
_SHOWN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,59}")
_CLOSED_SAYS: dict[str, str] = {
    CLOSED_CHANGED: (
        "closed when they approved it: it changed in the ERP after the card was left "
        "(a price, a rate or a source document), so nothing was made"
    ),
    # Every other final failure: a refusal and a timeout can look alike
    # (both may come back as plain error text), so nothing here says that
    # nothing was made.
    CLOSED_NO_ANSWER: (
        "closed when they approved it: the approval didn't go through after several tries, "
        "and whether a draft was made isn't recorded: check in the ERP before asking again"
    ),
}
_REPLACED_SAYS = "closed: replaced by a newer card because the ERP now reads the draft differently"
_UNKNOWN_CLOSE_SAYS = "closed; the reason wasn't recorded; don't guess one"
# A dismissed or expired card someone had already tried to approve: that try
# may have made the draft, so it never says nothing was made.
_TRIED_SAYS = "an approval of it was tried; check in the ERP whether a draft was made"
_OUTCOME_SAYS: dict[str, str] = {
    "approved": "approved: an unsubmitted draft was made",
    "dismissed": "dismissed: nothing was made",
    "expired": "expired before it was approved: nothing was made",
}
DECIDED_CARDS_NOTE = (
    "Draft cards left in this conversation that are no longer waiting, oldest first, "
    "as the records stand now. Mention one only when they ask about it or it bears on "
    "their question. Speak of each as it stands here, and give no reason a line doesn't "
    "give. When a card closed because the records changed in the ERP after it was left, "
    "that is not your error: say what changed and what it means now, never \"Earlier I …\", "
    "and offer to ask again for a card with the draft as it is now."
)


@dataclass(frozen=True)
class DecidedCard:
    """A card that is no longer waiting, as the turn's note shows it."""

    doctype: str
    # action_chips.card_outcome's word: approved, dismissed, closed or expired.
    outcome: str
    decided_at: datetime
    # Matched against _CLOSED_SAYS only, never shown.
    closed_code: str = ""
    # Closed by a later proposal that read the draft differently.
    replaced: bool = False
    # The draft's name in the ERP once approved, as the ERP gave it.
    name: str = ""
    # Dismissed or expired after an approval of it was tried (or while one
    # was under way), so a draft may exist.
    tried: bool = False


# The cards an eval scenario's turn is told about, in place of the ledger
# (evals.scenarios.scenario_decided_cards). Never set outside the eval runners.
decided_cards_override: ContextVar[tuple[DecidedCard, ...] | None] = ContextVar(
    "decided_cards_override", default=None,
)


def _decided(card: Any) -> DecidedCard | None:
    """``card`` as the note shows it, or None while it still waits or when
    its time can't be read. An expired card counts from when it expired, not
    from when it was tidied, so tidying it late never makes it news."""
    from openexecutive.memory.decision_ledger import STATUS_CLOSED_EXTERNALLY, STATUS_EXECUTING
    from openexecutive.orchestrator.action_chips import card_outcome

    outcome = card_outcome(card)
    if outcome == "waiting":
        return None
    if outcome == "expired":
        created = _created_at(card)
        decided_at = created + CARD_TTL if created is not None else None
    else:
        try:
            decided_at = datetime.fromisoformat(str(card.resolved_at))
        except (TypeError, ValueError):
            decided_at = None
        if decided_at is not None and decided_at.tzinfo is None:
            decided_at = decided_at.replace(tzinfo=UTC)
    if decided_at is None:
        return None
    try:
        final = json.loads(card.final_payload_json or "{}")
    except (TypeError, ValueError):
        final = {}
    final = final if isinstance(final, dict) else {}
    code = final.get("closed_code")
    name = ""
    if outcome == "approved":
        made = final.get("created")
        given = made.get("name") if isinstance(made, dict) else None
        name = given if isinstance(given, str) and _SHOWN_NAME_RE.fullmatch(given) else ""
    attempts = _payload(card).get("attempts")
    tried = outcome in ("dismissed", "expired") and (
        card.status == STATUS_EXECUTING
        or _payload(card).get("tried") is True
        or (isinstance(attempts, int) and not isinstance(attempts, bool) and attempts > 0)
    )
    return DecidedCard(
        doctype=str(_payload(card).get("doctype") or ""),
        outcome=outcome,
        decided_at=decided_at,
        closed_code=code if isinstance(code, str) else "",
        replaced=(
            outcome == "closed" and card.status == STATUS_CLOSED_EXTERNALLY
            and card.reversal_reason == "changed"
        ),
        name=name,
        tried=tried,
    )


def decided_cards(person_id: int, session_id: str, *, now: datetime | None = None) -> list[DecidedCard]:
    """``person_id``'s cards left in conversation ``session_id`` that were
    decided within ``DECIDED_WINDOW``: the newest ``DECIDED_MAX``, oldest
    first. Reads the ledger only; it never tidies (``open_cards``)."""
    from openexecutive.memory.decision_ledger import list_session_instances

    at = now or datetime.now(UTC)
    found = list_session_instances(DECISION_CLASS, session_id, approver_person_id=person_id, limit=1000)
    decided = [
        d for d in (_decided(c) for c in found)
        if d is not None and at - d.decided_at <= DECIDED_WINDOW
    ]
    return sorted(decided, key=lambda d: d.decided_at)[-DECIDED_MAX:]


def conversation_has_cards(session_id: str) -> bool:
    """Whether conversation ``session_id`` left any draft card: its turns
    read the ERP to leave one, and a later turn may be told what became of
    it (``decided_cards_note``). Raises if the ledger can't be read."""
    from openexecutive.memory.decision_ledger import list_session_instances

    return bool(list_session_instances(DECISION_CLASS, session_id, limit=1))


def decided_says(card: DecidedCard) -> str:
    """What the note says became of ``card``: our fixed words only."""
    if card.outcome == "closed":
        if card.replaced:
            return _REPLACED_SAYS
        return _CLOSED_SAYS.get(card.closed_code, _UNKNOWN_CLOSE_SAYS)
    if card.tried and card.outcome in ("dismissed", "expired"):
        return f"{card.outcome}: {_TRIED_SAYS}"
    return _OUTCOME_SAYS.get(card.outcome, _UNKNOWN_CLOSE_SAYS)


def render_decided_cards(found: list[DecidedCard] | tuple[DecidedCard, ...]) -> str:
    """The note's body: the instruction line, then a line per card in our
    own words, each approved draft's name after it in an untrusted block.
    "" for no cards."""
    from openexecutive.memory.workspace_settings import get_user_timezone
    from openexecutive.orchestrator.content_trust import wrap_untrusted
    from openexecutive.prompts.connected_systems import in_product_reply

    if not found:
        return ""
    try:
        zone: Any = get_user_timezone()
    except Exception:
        zone = UTC
    lines = [in_product_reply(DECIDED_CARDS_NOTE)]
    for card in found:
        label = f"{card.doctype} draft" if card.doctype in DRAFT_TOOLS else "A draft"
        when = card.decided_at.astimezone(zone).strftime("%a %d %b %H:%M")
        says = decided_says(card)
        name = card.name if card.outcome == "approved" and _SHOWN_NAME_RE.fullmatch(card.name) else ""
        if name:
            says += "; its name in the ERP follows"
        lines.append(in_product_reply(f"- {label}, {when}: {says}."))
        if name:
            lines.append(wrap_untrusted(name, source="erpnext"))
    return "\n".join(lines)


def _note_person(session: Any, person_id: int | None) -> int | None:
    """Whose cards the turn's note may show, or None for no note. ERPNext's
    panel: the team member its signed email names (``erpnext_panel._person_for``),
    whose own conversation it is (never on a turn of the web chat). Anywhere
    else: the turn's ``person_id``, only in a conversation nobody else reads,
    and for a teammate only on a surface that verified them. Never an unattended run or an
    email turn."""
    from openexecutive.memory.history_chat import private_chat
    from openexecutive.orchestrator.schedule_tools import erpnext_panel_turn

    if session is None or getattr(session, "unattended", False) is True:
        return None
    if (
        str(getattr(session, "origin_channel", "") or "") == "email"
        or getattr(session, "email_from", None)
        or getattr(session, "private_to_principal", False) is True
    ):
        return None
    # A turn of this app's own web chat is never the panel's, whatever
    # email it carries.
    if erpnext_panel_turn(session) and getattr(session, "from_web_chat", False) is not True:
        from openexecutive.integrations.erpnext_panel import _person_for

        person = _person_for(str(session.erpnext_panel_email).strip().lower())
        return int(person.id) if person is not None else None
    from openexecutive.orchestrator.schedule_tools import host_turn

    if host_turn(session):
        # ERPNext's desk: whoever the ERP is asked as on such a turn
        # (erpnext_caller._host_caller: signed in, a team member, a private
        # conversation), and only them. The page names no one.
        from openexecutive.orchestrator import erpnext_caller

        try:
            erpnext_caller._host_caller(session)
        except erpnext_caller.CallerRefused:
            return None
        pid = getattr(session, "caller_person_id", None)
        return pid if isinstance(pid, int) and not isinstance(pid, bool) else None
    if getattr(session, "shared_chat", False) is not False or not private_chat(session):
        return None
    if not isinstance(person_id, int) or isinstance(person_id, bool):
        return None
    from openexecutive.people.store import get_person

    person = get_person(person_id)
    if person is None or not person.active:
        return None
    if person.is_principal:
        # The principal's cards only on a surface that proved it is them, as
        # leaving one needs (principal_speaking): a chat id alone isn't proof.
        from openexecutive.orchestrator.content_trust import principal_speaking

        return person_id if principal_speaking(session) else None
    # A teammate's cards hold what the ERP read as them: only when this
    # surface verified it is them, as GET /erpnext/drafts holds them.
    from openexecutive.orchestrator.people_tools import teammate_on_verified_surface

    teammate = teammate_on_verified_surface(session)
    if teammate is None or teammate.id != person_id:
        return None
    if getattr(session, "from_web_chat", False) is True:
        # The web chat counts its caller as verified even unsigned; the
        # drafts route shows a teammate's cards only under signed sign-ins.
        from openexecutive.api.caller import signing_on

        if not signing_on():
            return None
    return person_id


def decided_cards_note(session: Any, person_id: int | None) -> str:
    """The body of the turn's ``<decided_cards>`` block (user turn only):
    what became of the cards this conversation left for the turn's own
    person, so a reply can say why one closed rather than guess. "" when
    there are none, the turn may not see them, or anything fails: it never
    blocks a turn. A note marks the conversation as one that read the ERP."""
    try:
        override = decided_cards_override.get()
        if override is not None:
            found: list[DecidedCard] | tuple[DecidedCard, ...] = override
        else:
            session_id = getattr(session, "session_id", None)
            if not isinstance(session_id, str) or not session_id:
                return ""
            who = _note_person(session, person_id)
            if who is None:
                return ""
            found = decided_cards(who, session_id)
        note = render_decided_cards(found)
    except Exception:
        logger.warning("erpnext_drafts: couldn't read this conversation's decided cards", exc_info=True)
        return ""
    if note and session is not None:
        session.read_erpnext = True
    return note


def cards(person: Any) -> list[DraftCard]:
    """``person``'s waiting draft cards, for ``GET /erpnext/drafts``. What the
    card says in words (its reason, notes and last error) is in the product's
    words on an install with a name (``in_product_reply``); ERPNext's own
    values in its fields and lines are shown as they are."""
    from openexecutive.prompts.connected_systems import in_product_reply as words

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
            why=words(str(payload.get("why") or "")),
            doctype=str(payload.get("doctype") or ""),
            fields=[f for f in view.get("fields") or [] if isinstance(f, dict)],
            lines=[ln for ln in view.get("lines") or [] if isinstance(ln, dict)],
            notes=[words(str(n)) for n in view.get("notes") or []],
            last_error=words(str(payload.get("last_error") or "")),
            asked_in=str(payload.get("asked_in")) if payload.get("asked_in") in (ASKED_IN_PANEL, ASKED_IN_DESK) else "",
            operation=(
                str(payload.get("operation")) if payload.get("operation") in ("update", "comment", "credit") else "create"
            ),
            job_title=_job_title(payload),
            why_now=_one_line(payload.get("reason"), JOB_REASON_MAX) if payload.get("source") == JOB_SOURCE else "",
        ))
    return out


# ---------------------------------------------------------------------------
# A watch job's cards
# ---------------------------------------------------------------------------

JOB_SOURCE = "watch"
# A reason line's length, as the job's reason step caps it.
JOB_REASON_MAX = 200
# The job's own cap on its open cards, across days and reviewers.
JOB_OPEN_MAX = 10
# Headroom a job's cards leave in a reviewer's OPEN_CARDS_MAX for their own.
JOB_HEADROOM = 5
SOD_JOB_TEXT = "You set up this job, so someone else approves its cards. Nothing was made."


def _job_title(payload: dict[str, Any]) -> str:
    """The job's title, from the registry (code), for a watch card."""
    if payload.get("source") != JOB_SOURCE:
        return ""
    from openexecutive.watch import registry

    job = registry.get(payload.get("job"))
    return job.title if job is not None else ""


def job_cards(job_id: str, *, open_only: bool = True) -> list[Any]:
    """The draft cards a watch job left (waiting, or every one)."""
    from openexecutive.memory.decision_ledger import (
        STATUS_EXECUTING,
        STATUS_PROPOSED,
        list_instances,
    )

    statuses: tuple[str | None, ...] = (STATUS_PROPOSED, STATUS_EXECUTING) if open_only else (None,)
    out: list[Any] = []
    for status in statuses:
        for card in list_instances(DECISION_CLASS, status=status, limit=5000):
            payload = _payload(card)
            if payload.get("source") == JOB_SOURCE and payload.get("job") == job_id:
                out.append(card)
    return out


def job_card_waiting(dedup_key: str) -> bool:
    """Whether a job's card for this finding is still waiting."""
    from openexecutive.memory.decision_ledger import (
        STATUS_EXECUTING,
        STATUS_PROPOSED,
        list_instances,
    )

    for status in (STATUS_PROPOSED, STATUS_EXECUTING):
        for card in list_instances(DECISION_CLASS, status=status, limit=5000):
            payload = _payload(card)
            if payload.get("source") == JOB_SOURCE and payload.get("dedup_key") == dedup_key:
                return True
    return False


def job_room(job_id: str, reviewer_id: int) -> str | None:
    """Why a job may leave no more cards now, or None: at most
    ``JOB_OPEN_MAX`` of its own waiting, and never its reviewer past
    ``OPEN_CARDS_MAX - JOB_HEADROOM``, so their own asks still fit."""
    if len(job_cards(job_id)) >= JOB_OPEN_MAX:
        return "job_full"
    if len(open_cards(reviewer_id)) >= OPEN_CARDS_MAX - JOB_HEADROOM:
        return "reviewer_full"
    return None


def leave_job_card(
    job: Any, run_day: str, operation: str, doctype: str, fields: dict[str, Any], check: dict[str, Any],
    *, reviewer: Any, configured_by_person_id: int, dedup_key: str, why: str, reason: str = "",
) -> tuple[int, bool]:
    """Leave ``reviewer`` the card ``job``'s run found, through ``propose``
    (so a waiting card for the same finding is that card, a changed
    fingerprint replaces it, and none changes in place). ``fields`` come from
    the job's ``check_fields`` with what ``check`` (ERPNext's ``draft_check``
    as the reviewer) settled pinned; the view is code's reading of ``check``.
    ``why`` is the code-built title; ``reason`` the reason step's line (a
    model's), shown as "Why now" and never in the title, key or fields.
    Returns the decision id and whether it is new."""
    from openexecutive.delegation.gmail import normalize_email

    email = normalize_email(reviewer.email or "")
    fingerprint = fingerprint_of(check)
    if fingerprint is None or "@" not in email:
        raise ValueError("a job's card needs ERPNext's fingerprint and its reviewer's email")
    view = card_view(doctype, check.get("summary"), fields)
    decision_id, new, _view = propose(
        reviewer, doctype, fields, view, why, asked_by=email, session_id=None,
        fingerprint=fingerprint, operation=operation, scope=f"{JOB_SOURCE}:{job.id}",
        extra={
            "source": JOB_SOURCE,
            "job": job.id,
            "run_day": run_day,
            "asked_by_person_id": reviewer.id,
            "approver_person_id": reviewer.id,
            "configured_by_person_id": configured_by_person_id,
            "dedup_key": dedup_key,
            **({"reason": _one_line(reason, JOB_REASON_MAX)} if reason else {}),
        },
    )
    if new:
        audit("erpnext_draft_proposed", f"Watch job {job.id} left person {reviewer.id} an ERPNext {doctype} card", {
            "person_id": reviewer.id, "decision_id": decision_id, "doctype": doctype, "job": job.id,
            "configured_by_person_id": configured_by_person_id,
        })
    return decision_id, new


def _sod_refused(instance: Any, payload: dict[str, Any], person: Any, why: str) -> ApproveRefused:
    """Close a job's card its configurer tried to approve (or whose configurer
    is gone), audit it, and the refusal to raise."""
    from openexecutive.memory.decision_ledger import close_externally

    close_externally(instance.id, reason="sod_refused")
    audit("erpnext_draft_sod_refused", f"Person {person.id} may not approve watch job {payload.get('job')}'s card", {
        "person_id": person.id, "decision_id": instance.id, "doctype": payload.get("doctype"),
        "job": payload.get("job"), "configured_by_person_id": payload.get("configured_by_person_id"),
        "reason": why,
    })
    return ApproveRefused(409, "separation_of_duties", SOD_JOB_TEXT)


def _job_approval(instance: Any, payload: dict[str, Any], person: Any) -> dict[str, str]:
    """A watch job's card, before anything is claimed: raises
    ``ApproveRefused`` unless ``person`` may approve it now, and returns the
    wire values the write tool gets (``asked_by: "job:<id>"`` and the
    configurer's current email). Separation of duties first, whatever
    ERPNEXT_DRAFT_SOD says."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.people.store import get_person
    from openexecutive.watch import registry
    from openexecutive.watch.settings import get_row, master_on

    job = registry.get(payload.get("job"))
    if job is None or job.check_fields is None:
        raise ApproveRefused(409, "malformed", "This card can't be carried out. Dismiss it.")
    row = get_row(job.id)
    stored = payload.get("configured_by_person_id")
    configurer = get_person(stored) if isinstance(stored, int) and not isinstance(stored, bool) else None
    if configurer is None or configurer.id is None:
        raise _sod_refused(instance, payload, person, "configurer_missing")
    if person.id in (configurer.id, row.configured_by_person_id):
        raise _sod_refused(instance, payload, person, "configurer_approves")
    configured_by = normalize_email(configurer.email or "")
    if not configurer.active or "@" not in configured_by:
        raise _sod_refused(instance, payload, person, "configurer_unavailable")
    if not master_on() or not row.enabled:
        raise ApproveRefused(409, "job_off", f"The {job.title} job is turned off, so its cards can't be approved. Nothing was made.")
    if person.is_principal:
        raise ApproveRefused(403, "principal", "A job's cards are approved by its reviewer, never the account owner.")
    if person.demo:
        raise ApproveRefused(403, "demo", "A demo account can't approve a job's card.")
    for key in ("approver_person_id", "asked_by_person_id"):
        if payload.get(key) != person.id:
            raise ApproveRefused(403, "not_yours", "Only the person this card is for can approve it.")
    if row.reviewer_person_id != person.id:
        raise ApproveRefused(
            409, "not_reviewer", f"You're no longer the {job.title} job's reviewer, so you can't approve its cards. "
            "Nothing was made.",
        )
    fields = payload.get("fields")
    again = job.check_fields(str(payload.get("operation", "create")), str(payload.get("doctype")), fields) \
        if isinstance(fields, dict) else "malformed"
    if isinstance(again, str) or again != fields or fingerprint_of(payload) is None:
        raise ApproveRefused(409, "malformed", "This card can't be carried out. Dismiss it.")
    if payload.get("doctype") == "Dunning" and not fields.get("posting_date"):
        raise ApproveRefused(409, "malformed", "This card can't be carried out. Dismiss it.")
    return {"asked_by": f"job:{job.id}", "configured_by": configured_by}


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
        return _error("The ERP isn't connected right now.")
    return await asyncio.wait_for(
        gateway.call_tool({"name": tool, "arguments": arguments}), timeout=CALL_TIMEOUT_S
    )


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

# How each DocType's fields read in the tool's description.
_FIELD_HELP: dict[str, str] = {
    "Dunning": (
        "Dunning fields (a payment reminder for one overdue, submitted Sales Invoice): "
        "sales_invoice, optional dunning_type (the company's default otherwise). Give "
        "posting_date (YYYY-MM-DD) only when they name the letter's date; otherwise "
        "leave it out and ERPNext dates it today."
    ),
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


def propose_tool(
    doctypes: tuple[str, ...], *, updates: tuple[str, ...] = (), comments: tuple[str, ...] = (),
) -> dict[str, Any]:
    """``propose_erpnext_draft`` for the DocTypes this install drafts, and
    the ones it may change (``updates``) or comment on (``comments``).
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
            + _change_help(updates, comments)
            + "Tell them the card is waiting; never say the draft exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "why": {
                    "type": "string",
                    "description": "One line on what this is for, e.g. 'Restock ITEM-1, below its reorder level'.",
                },
                "doctype": {"type": "string", "enum": sorted({*doctypes, *updates, *comments})},
                "fields": {"type": "object", "description": "The draft's fields, as listed above for its doctype."},
                **(
                    {
                        "operation": {
                            "type": "string",
                            "enum": ["create", *(["update"] if updates else []), *(["comment"] if comments else [])],
                            "description": "create (a new draft, the default), update or comment.",
                        },
                        "name": {"type": "string", "description": "The document to update or comment on."},
                    }
                    if updates or comments else {}
                ),
            },
            "required": ["why", "doctype", "fields"],
        },
    }


def _change_help(updates: tuple[str, ...], comments: tuple[str, ...]) -> str:
    help_ = ""
    if updates:
        help_ += (
            f"To change an unsubmitted {', '.join(updates)} they own or were assigned, use operation "
            "`update` with its `name` and fields {set: {field: value}, lines: {items: {row name: "
            "{field: value}}}, remove_lines: {items: [row name]}}; read the document first for its row "
            "names. A Purchase Invoice's header fields: bill_no, bill_date, due_date, posting_date, "
            "set_posting_time, remarks, cost_center, project, on_hold, release_date, hold_comment (never "
            "its supplier); its line fields: qty, rate, uom, expense_account, cost_center, "
            "project, description. Only the values they asked for; nothing is submitted. "
        )
    if comments:
        help_ += (
            f"To add their comment to a {', '.join(comments)}, use operation `comment` with its `name` "
            "and fields {text}: their words as they gave them, plain text, at most 2,000 characters. "
        )
    return help_


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
    operation = tool_input.get("operation") or "create"
    target: str | None = None
    if operation == "create":
        if tool_input.get("name") not in (None, ""):
            return _error("A new draft has no `name`; use operation update or comment to change a document.")
        fields = check_fields(tool_input.get("doctype"), tool_input.get("fields"))
    else:
        from openexecutive.integrations import erpnext_changes

        checked_change = erpnext_changes.check_change(
            str(operation), tool_input.get("doctype"), tool_input.get("name"), tool_input.get("fields"),
        )
        fields = checked_change if isinstance(checked_change, str) else checked_change[1]
        if not isinstance(checked_change, str):
            target = checked_change[0]
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
    full = _cards_full(person.id, doctype, _keyed(str(operation), target, fields))
    if full is not None:
        return _error(full)
    # The turn's slot is taken before the check, which waits on ERPNext, so
    # calls made together in one round can't each pass CARDS_PER_TURN; it is
    # given back below unless a new card is left.
    pinned.erpnext_drafts += 1
    new = False
    try:
        result, new = await _check_and_leave(
            session, person, email, doctype, fields, str(tool_input.get("why") or ""),
            operation=str(operation), name=target,
        )
        return result
    finally:
        if not new:
            pinned.erpnext_drafts -= 1


def _noun(operation: str) -> str:
    return {"update": "change", "comment": "comment", "credit": "credit-limit raise"}.get(operation, "draft")


def _after_approval(operation: str) -> str:
    """What the tool's result tells the model approval will do."""
    where = (
        "The card is on Today in the OpenExecutive web app, and under this reply in its chat, "
        "for them to approve. "
    )
    if operation == "update":
        return ("Nothing is changed in ERPNext yet. " + where + "Approving makes exactly the change "
                "it shows, as them, and the document stays unsubmitted. ")
    if operation == "comment":
        return "Nothing is in ERPNext yet. " + where + "Approving adds exactly that comment, as them. "
    return ("Nothing is in ERPNext yet. " + where + "It then becomes an unsubmitted draft they review "
            "and submit in ERPNext. ")


def _keyed(operation: str, name: str | None, fields: dict[str, Any]) -> dict[str, Any]:
    """What a card's dedupe digest covers: a new draft's fields as before;
    an update's or comment's operation and document with them."""
    if operation == "create":
        return fields
    return {"__operation": operation, "__name": name, **fields}


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
    *, operation: str = "create", name: str | None = None,
) -> tuple[str, bool]:
    """``handle_propose`` once the turn's slot is taken: ERPNext's check as
    the turn's caller, then the card. The tool result, and whether a new
    card was left."""
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.orchestrator.content_trust import wrap_untrusted
    from openexecutive.orchestrator.schedule_tools import erpnext_panel_turn

    check_args: dict[str, Any] = {"doctype": doctype, "fields": fields}
    if operation != "create":
        check_args = {"doctype": doctype, "name": name, "operation": operation, "fields": fields}
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
    if operation != "create":
        from openexecutive.integrations import erpnext_changes

        refusal = (
            "ERPNext didn't fingerprint this change, so no card was left. Try again in a moment."
            if fingerprint_of(checked) is None
            else erpnext_changes.summary_refusal(operation, doctype, str(name), fields, checked)
        )
        if refusal is not None:
            return _error(refusal), False
    if operation == "create" and doctype == "Dunning" and fingerprint_of(checked) is None:
        # Its figures are the point: never a card that approves blind.
        return _error("ERPNext didn't fingerprint this letter, so no card was left. Try again in a moment."), False
    pinned = pin_from_check(doctype, fields, checked.get("summary")) if operation == "create" else fields
    if isinstance(pinned, str):
        return _error(pinned), False
    fields = pinned
    if operation == "create":
        view = card_view(doctype, checked.get("summary"), fields)
    else:
        from openexecutive.integrations import erpnext_changes

        view = erpnext_changes.card_view(operation, doctype, str(name), checked["summary"])
    from_panel = erpnext_panel_turn(session)
    from openexecutive.orchestrator.schedule_tools import host_turn

    # Where it was asked, said on the card, its payload and its audit row: a
    # panel's word or a desk page's turn, never this app's own chat.
    asked_in = ASKED_IN_PANEL if from_panel else ASKED_IN_DESK if host_turn(session) else ""
    if asked_in == ASKED_IN_PANEL:
        view["notes"] = [*view["notes"], PANEL_NOTE]
    elif asked_in == ASKED_IN_DESK:
        view["notes"] = [*view["notes"], DESK_NOTE]
    # Read again after the check, with no await before the card is stored,
    # so calls made together never pass OPEN_CARDS_MAX.
    full = _cards_full(person.id, doctype, _keyed(operation, name, fields))
    if full is not None:
        return _error(full), False
    try:
        decision_id, new, view = propose(
            person, doctype, fields, view, why,
            asked_by=email, session_id=getattr(session, "session_id", None),
            asked_in=asked_in, fingerprint=fingerprint_of(checked),
            operation=operation, name=name,
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
        audit("erpnext_draft_proposed", f"Left person {person.id} an ERPNext {doctype} {_noun(operation)} card", {
            "person_id": person.id, "decision_id": decision_id, "doctype": doctype,
            **({"operation": operation, "name": name} if operation != "create" else {}),
            **({"asked_in": asked_in} if asked_in else {}),
        })
    try:
        earlier = earlier_cards(person.id, getattr(session, "session_id", None), leaving=decision_id)
    except Exception:
        logger.warning("erpnext_drafts: couldn't read this conversation's earlier cards", exc_info=True)
        earlier = []
    return json.dumps({
        "status": "waiting_for_approval",
        "decision_id": decision_id,
        "doctype": doctype,
        **({"operation": operation} if operation != "create" else {}),
        "card": wrap_untrusted(json.dumps(view, ensure_ascii=False), source="erpnext", author="draft_check"),
        # Only what this conversation left, as each stands now.
        "earlier_cards": earlier,
        "note": (
            ("" if new else f"This is the card already waiting for the same {_noun(operation)}. ")
            + _after_approval(operation)
            + "Outside the web app, say "
            "where to find it. Speak of this conversation's earlier cards only as "
            "`earlier_cards` gives them (approved, dismissed, closed with nothing made, "
            "expired, or waiting), or not at all; never call one waiting that isn't, "
            "and never count cards from other conversations."
        ),
    }), new


def draft_tools() -> list[dict[str, Any]]:
    """The turn tools a turn ``offered`` drafts gets."""
    from openexecutive.integrations import erpnext_changes

    return [propose_tool(
        enabled_doctypes(),
        updates=erpnext_changes.update_doctypes(),
        comments=erpnext_changes.comment_doctypes(),
    )]


def chip(tool_input: dict[str, Any], parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    """The chat chip for a proposal: only a stored card earns one, and it says
    nothing is in ERPNext yet. The chat shows the card itself under its
    message, by ``decision_id``."""
    if (parsed or {}).get("status") != "waiting_for_approval":
        return None
    doctype = (parsed or {}).get("doctype")
    from openexecutive.prompts.connected_systems import in_product_reply

    operation = (parsed or {}).get("operation")
    if operation in ("update", "comment") and isinstance(doctype, str):
        noun = f"{'change to' if operation == 'update' else 'comment on'} a {_one_line(doctype, NAME_MAX)}"
    else:
        noun = f"{doctype} draft" if doctype in DRAFT_TOOLS else "ERPNext draft"
    return {
        "summary": in_product_reply(f"Waiting for your approval: {noun}").replace(": the ERP draft", ": a draft"),
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
    from openexecutive.memory.decision_ledger import (
        claim_for_execution,
        release_claim,
        update_open_payload,
    )
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
        raise ApproveRefused(409, "principal_only", "For now only the account owner can approve ERP drafts.")
    if not drafts_on():
        raise ApproveRefused(409, "drafts_off", "ERP drafts are turned off here, or the ERP isn't connected.")
    # A watch job's card: its job, its reviewer and separation of duties.
    wire = _job_approval(instance, payload, person) if payload.get("source") == JOB_SOURCE else None
    from openexecutive.integrations import erpnext_changes

    operation = payload.get("operation", "create")
    if operation not in erpnext_changes.OPERATIONS:
        raise ApproveRefused(409, "malformed", "This card can't be carried out. Dismiss it and ask again.")
    if operation == "credit":
        # A credit-limit raise (erpnext_credit): its own switches, authority
        # and both identities, checked before anything is claimed.
        from openexecutive.integrations import erpnext_credit

        erpnext_credit.approval_refusal(instance, payload, person)
    elif operation == "create" and payload.get("doctype") in DRAFT_TOOLS and payload.get("doctype") not in enabled_doctypes():
        raise ApproveRefused(409, "drafts_off", f"ERP {payload['doctype']} drafts are turned off here.")
    elif operation != "create" and payload.get("doctype") not in erpnext_changes.enabled(operation):
        what = "changes" if operation == "update" else "comments"
        raise ApproveRefused(409, "drafts_off", f"ERP {what} on {payload.get('doctype')} are turned off here.")
    if not _live(instance, datetime.now(UTC)):
        raise ApproveRefused(409, "expired", "This card is more than three days old. Ask again if it's still needed.")
    doctype, fields, key = payload.get("doctype"), payload.get("fields"), payload.get("card_key")
    target = payload.get("name")
    rechecked: Any
    if operation == "create":
        tool = DRAFT_TOOLS.get(doctype) if isinstance(doctype, str) else None
        rechecked = "a new draft has no name" if target is not None else check_fields(doctype, fields)
    elif operation == "credit":
        from openexecutive.integrations import erpnext_credit

        tool = erpnext_changes.OPERATION_TOOLS[operation]
        again_credit = erpnext_credit.credit_fields(fields)
        rechecked = (
            "malformed" if again_credit is None or doctype != erpnext_credit.DOCTYPE
            or erpnext_credit._doc_name(target) is None else again_credit
        )
        if not (isinstance(payload.get("fingerprint"), str) and _FINGERPRINT_RE.fullmatch(payload["fingerprint"])):
            rechecked = "no fingerprint"
    else:
        tool = erpnext_changes.OPERATION_TOOLS[operation]
        again = erpnext_changes.check_change(operation, doctype, target, fields)
        rechecked = again if isinstance(again, str) else (again[1] if again[0] == target else "name changed")
        if not isinstance(payload.get("fingerprint"), str):
            rechecked = "no fingerprint"
    if (
        tool is None or payload.get("tool") != tool or isinstance(rechecked, str) or rechecked != fields
        or not isinstance(key, str) or not _KEY_RE.fullmatch(key) or key != instance.idempotency_key
        or not isinstance(doctype, str)
    ):
        raise ApproveRefused(409, "malformed", "This card can't be carried out. Dismiss it and ask again.")
    if "@" not in email:
        raise ApproveRefused(409, "no_email", "Your People entry has no email, so the ERP can't tell who you are.")
    # The card was checked as the person who asked: if their People email
    # changed since, it may now name someone else, so it isn't theirs to make.
    asked_by = payload.get("asked_by")
    # A credit raise may be approved by someone other than who prepared it:
    # erpnext_credit.approval_refusal held both to the card instead.
    if operation != "credit" and (not isinstance(asked_by, str) or normalize_email(asked_by) != email):
        raise ApproveRefused(409, "changed", "Your email changed since this card was left. Dismiss it and ask again.")
    if not claim_for_execution(instance.id, resolver_person_id=resolver):
        raise ApproveRefused(409, "already_handled", "This card was already handled.")
    _APPROVING.add(instance.id)
    try:
        # Marked before the call, while the claim is held: if the call never
        # comes back (a restart releases the claim), the card still says an
        # approval was tried, so the note never says nothing was made. No
        # mark, no call: the card waits again and the person approves anew.
        stored = instance.proposed_payload_json
        if payload.get("tried") is not True:
            marked = {**payload, "tried": True}
            try:
                ok = update_open_payload(instance.id, marked, expected_json=stored, while_executing=True)
            except Exception:
                logger.warning("erpnext_drafts: marking card %d as tried failed", instance.id, exc_info=True)
                ok = False
            if not ok:
                release_claim(instance.id)
                raise ApproveRefused(409, "busy", "This card changed a moment ago. Approve it again.")
            payload, stored = marked, json.dumps(marked)
        return await _carry_out(instance, payload, person, email, tool, rechecked, key, stored, wire=wire)
    finally:
        _APPROVING.discard(instance.id)


async def _carry_out(
    instance: Any, payload: dict[str, Any], person: Any, email: str,
    tool: str, fields: dict[str, Any], key: str, stored: str, *, wire: dict[str, str] | None = None,
) -> dict[str, Any]:
    """``approve`` once it holds the claim: the one ERPNext call, then the
    card finished, or back to waiting with why. Raises ``ApproveRefused``.
    ``wire``: a watch job's card's ``asked_by`` and ``configured_by``
    (``_job_approval``), from its stored keys, never a model's."""
    from openexecutive.memory.decision_ledger import (
        STATUS_APPROVED_UNCHANGED,
        STATUS_FAILED,
        finish_execution,
        release_claim,
        update_open_payload,
    )
    from openexecutive.orchestrator import erpnext_caller

    doctype = payload["doctype"]
    operation = payload.get("operation", "create")
    asked_by = payload.get("asked_by") if isinstance(payload.get("asked_by"), str) else email
    arguments = {**fields, "idempotency_key": key, "asked_by": asked_by}
    if wire is not None:
        # Only the write tool gets these: draft_check takes neither.
        arguments = {**arguments, "asked_by": wire["asked_by"], "configured_by": wire["configured_by"]}
    if operation != "create":
        # The document it changes or comments on, as the card named it.
        arguments = {"doctype": doctype, "name": payload["name"], **arguments}
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
        raw = _error("The ERP didn't answer in time; it may or may not have made the draft.")
    except BaseException:
        # Cancelled mid-call (the request went away, the API is stopping):
        # the card waits again rather than staying claimed. Approving again
        # sends the same key, so ERPNext returns the draft if it was made.
        release_claim(instance.id)
        raise
    result = _result(raw)
    name = result.get("name")
    changed: dict[str, Any] | None = None
    if operation != "create":
        from openexecutive.integrations.erpnext_changes import made

        # A credit raise is draft_update's: its success is an update's.
        changed = made("update" if operation == "credit" else operation, doctype, payload["name"], result)
        if changed is not None and operation == "credit":
            from openexecutive.integrations.erpnext_credit import made_extra

            changed = {**changed, "operation": operation, **made_extra(payload)}
    if changed is not None or (
        operation == "create" and result.get("ok") is True and result.get("docstatus") == 0
        and isinstance(name, str) and _DOC_NAME_RE.fullmatch(name)
    ):
        created = changed if changed is not None else {
            "doctype": doctype, "name": name, "url": _desk_link(result.get("url")),
            "already_created": result.get("already_created") is True,
        }
        name = created["name"]
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
        audit("erpnext_draft_created", f"Person {person.id} approved an ERPNext {doctype} {_noun(operation)}", {
            "person_id": person.id, "decision_id": instance.id, "doctype": doctype, "name": name,
            **({"operation": operation} if operation != "create" else {}),
            **_identities(payload),
            "already_created": created["already_created"],
            **{k: created[k] for k in ("row_hash", "chain_index") if k in created},
        })
        return created
    reason = _refusal_text(raw)
    if operation == "credit":
        from openexecutive.integrations import erpnext_credit

        # Final either way: approving again would refuse again.
        final_text = (
            erpnext_credit.CHANGED_TEXT if CHANGED_SINCE_PROPOSED in reason
            else erpnext_credit.SOD_TEXT if erpnext_credit.SOD_REFUSED in reason else None
        )
        if final_text is not None:
            # Our own code for why it closed (decided_cards_note): a change
            # since the card was left is changed_since_proposed; a refusal
            # for separation of duties has no code of its own, so the note
            # gives no reason rather than a wrong one.
            closed = {"closed_code": CLOSED_CHANGED} if final_text == erpnext_credit.CHANGED_TEXT else {}
            finish_execution(instance.id, STATUS_FAILED, final_payload={**payload, "last_error": reason, **closed})
            audit("erpnext_draft_changed", f"Person {person.id}'s credit-limit raise wasn't made and its card closed", {
                "person_id": person.id, "decision_id": instance.id, "doctype": doctype, "operation": operation,
                **_identities(payload),
            })
            code = "changed_since_proposed" if final_text == erpnext_credit.CHANGED_TEXT else "separation_of_duties"
            raise ApproveRefused(409, code, final_text)
    if wire is not None and SOD_REFUSED in reason:
        # oe_connector's own separation of duties: final, as ours is.
        finish_execution(instance.id, STATUS_FAILED, final_payload={**payload, "last_error": reason})
        audit("erpnext_draft_sod_refused", f"ERPNext refused person {person.id} watch job {payload.get('job')}'s card", {
            "person_id": person.id, "decision_id": instance.id, "doctype": doctype, "job": payload.get("job"),
            "configured_by_person_id": payload.get("configured_by_person_id"), "reason": "erpnext",
        })
        raise ApproveRefused(409, "separation_of_duties", SOD_JOB_TEXT)
    # Anywhere in it: the MCP layer may put its own words before a tool's error.
    if CHANGED_SINCE_PROPOSED in reason or (doctype == "Dunning" and _DUNNING_STALE in reason):
        # Nothing was made, and approving again would refuse again: the card
        # is closed, and asking again leaves one with the draft as it is now.
        finish_execution(
            instance.id, STATUS_FAILED,
            final_payload={**payload, "last_error": reason, "closed_code": CLOSED_CHANGED},
        )
        audit("erpnext_draft_changed", f"Person {person.id}'s ERPNext {doctype} draft changed since its card", {
            "person_id": person.id, "decision_id": instance.id, "doctype": doctype,
        })
        raise ApproveRefused(
            409, "changed_since_proposed",
            "This draft changed in the ERP since the card was left (a source document, a price or a "
            "rate), so nothing was made and the card is closed. Ask for it again to see it as it is now.",
        )
    attempts = int(payload.get("attempts") or 0) + 1
    audit("erpnext_draft_failed", f"Person {person.id}'s ERPNext {doctype} draft wasn't made", {
        "person_id": person.id, "decision_id": instance.id, "doctype": doctype, "attempts": attempts,
        **_identities(payload),
    })
    if attempts >= ATTEMPTS_MAX:
        # Our own code for why it closed, for the conversation's later turns
        # (decided_cards_note). A refusal and a timeout can't be told apart
        # by the answer's shape, so every such close is no_answer.
        finish_execution(
            instance.id, STATUS_FAILED,
            final_payload={**payload, "attempts": attempts, "last_error": reason, "closed_code": CLOSED_NO_ANSWER},
        )
        raise ApproveRefused(502, "draft_failed", f"The ERP didn't make the draft: {reason} This card has been tried {attempts} times, so it is closed.")
    # Back to waiting, with the reason: approving again sends the same card
    # key, so ERPNext returns the draft if it was made after all. The reason
    # and count are written while the claim is still held, then released, so
    # a second tap can't claim it in between and the count is never lost.
    if not update_open_payload(
        instance.id, {**payload, "attempts": attempts, "last_error": reason},
        expected_json=stored, while_executing=True,
    ):
        logger.warning("erpnext_drafts: couldn't note card %d's failed attempt", instance.id)
    release_claim(instance.id)
    raise ApproveRefused(
        502, "draft_failed",
        f"The ERP didn't make the draft: {reason} You can approve it again; it won't make a second one.",
    )


def _identities(payload: dict[str, Any]) -> dict[str, Any]:
    """A credit raise's audit rows name who prepared it and who approves; a
    watch job's card's, the job and who set it up."""
    if payload.get("source") == JOB_SOURCE:
        return {k: payload.get(k) for k in ("job", "configured_by_person_id", "approver_person_id")}
    if payload.get("operation") != "credit":
        return {}
    return {k: payload.get(k) for k in ("asked_by_person_id", "approver_person_id")}


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

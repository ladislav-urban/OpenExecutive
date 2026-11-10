"""The credit-raise card: "Prepare the raise" on a credit-hold alert.

The card half of the credit hold (the alert half is ``erpnext_events``'
``credit_hold`` and its named readers). oe_connector raises ``credit_hold``
for a draft Sales Order over its customer's credit limit, and signs with it
the raise that would let it through (``suggest``). The alert's named people
and the principal see a "Prepare the raise" button; approving the card it
leaves raises one company's credit limit for that customer, as the
approver, through oe_connector's ``draft_update`` (doctype Customer,
``credit: {company, credit_limit, sales_order}``). The order is never
submitted: its owner submits it in ERPNext once it fits.

**Never the model's.** A raise is only ever built here, from the stored
suggestion, on a person's own verified tap. ``propose_erpnext_draft`` never
takes Customer and never takes operation ``credit``
(``erpnext_changes.enabled("credit")`` lists nothing).

**Off by default** (``ERPNEXT_CREDIT_RAISE``, with drafts on): the first card
whose approval changes a live master at once.

**Who prepares** (``prepare``): the people the alert names and the principal,
each known to the API as themselves (``verified.caller_refusal``); a teammate
only with ``ERPNEXT_DRAFTS_TEAMMATES`` and signed sign-ins. ERPNext checks the
raise as them (``draft_check`` inside ``erpnext_caller.draft_check_as``). A
check ERPNext refuses for want of credit authority leaves no card and says
who can prepare it.

**Who approves** (``choose_approver``): the tapper when they hold
``customer_credit`` in People (or are the principal), aren't a demo person,
and separation of duties (``ERPNEXT_DRAFT_SOD``) is off; otherwise the
principal. With it on and the principal preparing, no card is left. The card
stores both identities (``approver_person_id`` / ``approver_email``,
``asked_by_person_id`` / ``asked_by``), and approval refuses unless both are
still those people (``approval_refusal``). oe_connector checks the ERPNext
role, raise-only, its cap, the held draft order and the fingerprint again,
as the approver.

**Read again on open** (``recheck``): the card list (``GET /erpnext/drafts``,
never ``cards()``, which Home's count calls) reads up to ``RECHECK_PER_REQUEST``
of the viewer's own credit cards again as them, least recently read first,
each at most every ``RECHECK_TTL_S``. A different fingerprint or an order no
longer a draft marks the card changed, and approval refuses it.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from openexecutive.integrations import erpnext_drafts as drafts

logger = logging.getLogger(__name__)

KIND = "credit_hold"
DOCTYPE = "Customer"
OPERATION = "credit"
LABEL = "Prepare the raise"
# oe_connector's own bound on a credit limit (draft_changes.CREDIT_HIGH).
CREDIT_HIGH = 1_000_000_000_000
# How oe_connector's separation-of-duties refusal begins (drafts.SOD_REFUSED).
SOD_REFUSED = "Separation of duties"
# How oe_connector refuses a tapper who may not prepare a raise: before
# oe_connector#535, anyone without the credit role; after it, anyone who is
# neither the held order's owner nor holds that role.
NEEDS_AUTHORITY = (
    "raising a credit limit needs customer credit authority",
    "a credit-limit raise is prepared by the held order's owner or someone with customer credit authority",
)
# Cards a person may have waiting for someone else's approval.
PER_ASKER_MAX = 3
RECHECK_PER_REQUEST = 3
RECHECK_TTL_S = 60.0
# A fresh read is kept this long at most, then forgotten.
RECHECK_KEEP_S = 3 * 24 * 3600.0
# How many people a "who can prepare it" reply names.
NAMES_MAX = 5
_SUGGEST_REQUIRED = frozenset({"doctype", "operation", "name", "company", "credit_limit", "sales_order"})
_SUGGEST_DISPLAY = frozenset({"current_limit", "currency"})
_OWNER_MAX = 254


def credit_on() -> bool:
    """``ERPNEXT_CREDIT_RAISE`` with drafts on and ERPNext connected."""
    from openexecutive.config import get_settings

    return bool(get_settings().erpnext_credit_raise) and drafts.drafts_on()


def _sod() -> bool:
    from openexecutive.config import get_settings

    return get_settings().erpnext_draft_sod == "agent"


def _number(value: Any) -> float | None:
    """``value`` as a finite number, never a bool; else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _limit(value: Any) -> float | None:
    """A credit limit: a finite number with 0 < x < ``CREDIT_HIGH``."""
    number = _number(value)
    return number if number is not None and 0 < number < CREDIT_HIGH else None


def _doc_name(value: Any) -> str | None:
    from openexecutive.integrations.erpnext_changes import _DOC_RE

    return value if isinstance(value, str) and _DOC_RE.fullmatch(value) else None


def event_key(company: str, sales_order: str) -> str:
    """The credit-hold event's key, as oe_connector cuts it."""
    return f"{company}:{sales_order}"[:140]


# ---------------------------------------------------------------------------
# The suggestion
# ---------------------------------------------------------------------------

def parse_suggestion(key: str, raw: Any) -> dict[str, Any] | None:
    """What approval needs of a credit-hold event's ``suggest``, or None.
    Shape: ``{doctype: "Customer", operation: "update", name, company,
    sales_order, credit_limit}``, with ``current_limit`` and ``currency``
    accepted (shape-checked) for display only and not kept. The event's key
    must be exactly ``f"{company}:{sales_order}"`` after oe_connector's cut,
    compared whole."""
    from openexecutive.integrations.erpnext_drafts import _CURRENCY_RE

    if not isinstance(raw, dict) or not set(raw) >= _SUGGEST_REQUIRED or set(raw) - _SUGGEST_REQUIRED - _SUGGEST_DISPLAY:
        return None
    if raw.get("doctype") != DOCTYPE or raw.get("operation") != "update":
        return None
    name, company, order = _doc_name(raw.get("name")), _doc_name(raw.get("company")), _doc_name(raw.get("sales_order"))
    limit = _limit(raw.get("credit_limit"))
    if name is None or company is None or order is None or limit is None:
        return None
    if "current_limit" in raw:
        current = _number(raw["current_limit"])
        if current is None or not 0 <= current < CREDIT_HIGH:
            return None
    if "currency" in raw and not (isinstance(raw["currency"], str) and _CURRENCY_RE.fullmatch(raw["currency"])):
        return None
    if not isinstance(key, str) or key != event_key(company, order):
        return None
    return {
        "doctype": DOCTYPE, "operation": "update", "name": name,
        "company": company, "sales_order": order, "credit_limit": limit,
    }


def credit_fields(raw: Any) -> dict[str, Any] | None:
    """A card's stored ``fields`` checked again: exactly ``{credit: {company,
    credit_limit, sales_order}}``, each of its kind; else None."""
    if not isinstance(raw, dict) or set(raw) != {"credit"}:
        return None
    credit = raw["credit"]
    if not isinstance(credit, dict) or set(credit) != {"company", "credit_limit", "sales_order"}:
        return None
    company, order, limit = _doc_name(credit["company"]), _doc_name(credit["sales_order"]), _limit(credit["credit_limit"])
    if company is None or order is None or limit is None:
        return None
    return {"credit": {"company": company, "credit_limit": credit["credit_limit"], "sales_order": order}}


def check_args(name: str, fields: dict[str, Any]) -> dict[str, Any]:
    """``draft_check``'s arguments for a raise, built here only."""
    return {"doctype": DOCTYPE, "operation": "update", "name": name, "fields": fields}


# ---------------------------------------------------------------------------
# ERPNext's reading
# ---------------------------------------------------------------------------

def summary_refusal(name: str, fields: dict[str, Any], checked: dict[str, Any]) -> str | None:
    """Why ``draft_check``'s answer can't become a card, or None. It must
    read back an update of this Customer, the same company and new limit,
    finite figures, ``order_fits`` a bool and true, the order still a draft,
    and a sound fingerprint."""
    from openexecutive.integrations.erpnext_drafts import _CURRENCY_RE

    if drafts.fingerprint_of(checked) is None:
        return "ERPNext didn't fingerprint this raise, so no card was left. Try again in a moment."
    summary = checked.get("summary")
    if not isinstance(summary, dict):
        return "ERPNext didn't say what it would change, so no card was left."
    if (summary.get("operation"), summary.get("doctype"), summary.get("name")) != ("update", DOCTYPE, name):
        return "ERPNext read a different customer or change, so no card was left."
    credit, order = summary.get("credit"), summary.get("sales_order")
    if not isinstance(credit, dict) or not isinstance(order, dict):
        return "ERPNext's reading of the raise was incomplete, so no card was left."
    asked = fields["credit"]
    figures = [credit.get(k) for k in ("limit_before", "limit_after", "outstanding", "with_order", "increase")]
    if (
        any(_number(v) is None for v in figures)
        or _number(order.get("base_grand_total")) is None
        or not isinstance(credit.get("order_fits"), bool)
        or not (isinstance(credit.get("currency"), str) and _CURRENCY_RE.fullmatch(credit["currency"]))
        or credit.get("company") != asked["company"]
        or order.get("name") != asked["sales_order"]
        or ("owner" in order and not (isinstance(order["owner"], str) and 0 < len(order["owner"]) <= _OWNER_MAX))
    ):
        return "ERPNext's reading of the raise was incomplete, so no card was left."
    docstatus = order.get("docstatus")
    if isinstance(docstatus, bool) or docstatus != 0:
        return "The order is no longer a draft in ERPNext, so there is nothing to raise. No card was left."
    if abs(float(credit["limit_after"]) - float(asked["credit_limit"])) >= 0.005:
        return "ERPNext read a different new limit, so no card was left."
    if credit["order_fits"] is not True:
        return "ERPNext says the order still wouldn't fit under that limit, so no card was left."
    return None


def _money(value: Any) -> str:
    return f"{float(value):,.2f}"


def card_view(name: str, fields: dict[str, Any], summary: dict[str, Any], prepared_by: str | None) -> dict[str, Any]:
    """The card: our own words. Y is exactly the stored new limit; X, V, W and
    the currency are ERPNext's check's."""
    one = drafts._one_line
    credit, order = summary["credit"], summary["sales_order"]
    asked = fields["credit"]
    customer, company = one(name, drafts.NAME_MAX), one(asked["company"], drafts.NAME_MAX)
    before, after = float(credit["limit_before"]), float(asked["credit_limit"])
    currency = credit["currency"]
    shown = [{
        "label": "Raise",
        "value": (
            f"Raise {customer}'s credit limit at {company} from {_money(before)} to {_money(after)} {currency} "
            f"(+{_money(after - before)}), so order {one(asked['sales_order'], drafts.NAME_MAX)} "
            f"({_money(order['base_grand_total'])}) fits; it owes {_money(credit['outstanding'])}."
        ),
    }]
    if prepared_by:
        shown.append({"label": "Prepared by", "value": one(prepared_by, drafts.NAME_MAX)})
    notes = []
    if credit.get("row") == "added":
        notes.append(
            f"{customer} has no credit limit of its own at {company}: approving adds one, which applies from "
            "then on instead of its group's or the company's."
        )
    notes.append("You'd still submit the order in ERPNext.")
    return {"fields": shown, "lines": [], "notes": notes}


# ---------------------------------------------------------------------------
# Who prepares, who approves
# ---------------------------------------------------------------------------

def has_authority(person: Any) -> bool:
    """The principal, or someone given ``customer_credit`` in People."""
    from openexecutive.people.models import AuthorityScope

    return bool(getattr(person, "is_principal", False)) or AuthorityScope.CUSTOMER_CREDIT in (
        getattr(person, "authority_scope", None) or []
    )


def choose_approver(tapper: Any, principal: Any) -> Any | str:
    """Who approves the raise ``tapper`` prepares, or why nobody can."""
    if tapper.is_principal:
        if _sod():
            return (
                "Separation of duties is on, so a raise you prepare is approved by someone else, and here only "
                "you approve raises. Give someone customer credit authority in People, and the credit "
                "controller role in ERPNext, so they can prepare it for you to approve. No card was left."
            )
        return tapper
    if not _sod() and not tapper.demo and has_authority(tapper) and drafts.teammates_on():
        return tapper
    return principal


def who_can_prepare(tapper_id: int | None) -> str:
    """The people with customer credit authority by name, else "your credit
    controller"."""
    from openexecutive.people.models import AuthorityScope
    from openexecutive.people.store import list_people

    try:
        names = [
            drafts._one_line(p.full_name, drafts.NAME_MAX) for p in list_people()
            if p.active and p.kind == "team" and not p.demo and p.id != tapper_id
            and AuthorityScope.CUSTOMER_CREDIT in (p.authority_scope or [])
        ][:NAMES_MAX]
    except Exception:
        logger.warning("erpnext_credit: couldn't read who holds customer credit authority", exc_info=True)
        names = []
    names = [n for n in names if n]
    if not names:
        return "your credit controller"
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def _asked_by_count(asker_id: int) -> int:
    """Raise cards ``asker_id`` left waiting for someone else's approval."""
    from datetime import UTC, datetime

    from openexecutive.memory.decision_ledger import STATUS_PROPOSED, list_instances

    now = datetime.now(UTC)
    count = 0
    for card in list_instances(drafts.DECISION_CLASS, status=STATUS_PROPOSED, limit=1000):
        payload = drafts._payload(card)
        if (
            payload.get("operation") == OPERATION and payload.get("asked_by_person_id") == asker_id
            and card.approver_person_id != asker_id and drafts._live(card, now)
        ):
            count += 1
    return count


def _room(tapper: Any, approver: Any, fields: dict[str, Any], name: str) -> list[Any]:
    """The approver's waiting cards; raises when they are full, or when the
    tapper already has ``PER_ASKER_MAX`` raises waiting on someone else."""
    from openexecutive.integrations.erpnext_alert_drafts import _room_for

    if approver.id != tapper.id and _asked_by_count(tapper.id) >= PER_ASKER_MAX:
        raise drafts.ApproveRefused(
            409, "too_many",
            f"You already have {PER_ASKER_MAX} raises waiting for someone else's approval. Wait for those first.",
        )
    return _room_for(approver.id, DOCTYPE, drafts._keyed(OPERATION, name, fields))


@dataclass
class Prepared:
    decision_id: int | None
    new: bool
    message: str


async def prepare(alert: Any, *, caller: Any, tapper: Any) -> Prepared:
    """Leave the raise ``alert`` suggests on a card, as ``tapper`` asked.
    Raises ``erpnext_drafts.ApproveRefused``."""
    from openexecutive.alerts.models import named_for
    from openexecutive.api.caller import signing_on
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.verified import NOT_YOURS, caller_refusal
    from openexecutive.integrations import erpnext_alert_drafts as alert_drafts
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.people.store import find_principal_person

    refused = drafts.ApproveRefused
    if not credit_on():
        raise refused(409, "drafts_off", "Raising credit limits from an alert is turned off here.")
    if tapper is None or tapper.id is None or not tapper.active or tapper.kind != "team":
        raise refused(404, "gone", "That alert isn't there.")
    if not tapper.is_principal and not named_for(alert, tapper.id):
        raise refused(404, "gone", "That alert isn't there.")
    if not tapper.is_principal and not drafts.teammates_on():
        raise refused(403, "principal_only", "For now only the account owner can prepare ERPNext drafts.")
    if not tapper.is_principal and not signing_on():
        raise refused(
            409, "caller_signing_required",
            "Preparing a raise needs signed sign-ins on this server, so that nobody else can do it for you.",
        )
    email = normalize_email(tapper.email or "")
    if "@" not in email:
        raise refused(409, "no_email", "Your People entry has no email, so ERPNext can't tell who you are.")
    why_not = caller_refusal(caller, email)
    if why_not == NOT_YOURS:
        raise refused(403, "not_yours", "Only the people this alert is for can prepare its raise.")
    if why_not is not None:
        raise refused(
            409, "caller_signing_required",
            "Preparing a raise needs signed sign-ins on this server, so that nobody else can do it for you.",
        )
    principal = find_principal_person()
    principal_email = normalize_email(getattr(principal, "email", None) or "")
    if principal is None or principal.id is None or "@" not in principal_email:
        raise refused(409, "no_email", "The account owner's People entry has no email, so no card can be left.")
    suggestion = alert_drafts.suggestion_for(alert)
    if suggestion is None or suggestion.get("doctype") != DOCTYPE:
        raise refused(404, "no_suggestion", "This alert has no raise to prepare.")
    try:
        alert_drafts._already(tapper.id, alert)
    except alert_drafts._Waiting as waiting_card:
        return Prepared(waiting_card.decision_id, False, "The raise is already on a card waiting for approval.")
    approver = choose_approver(tapper, principal)
    if isinstance(approver, str):
        raise refused(409, "no_approver", approver)
    name = suggestion["name"]
    fields = {"credit": {
        "company": suggestion["company"], "credit_limit": suggestion["credit_limit"],
        "sales_order": suggestion["sales_order"],
    }}
    _room(tapper, approver, fields, name)
    args = check_args(name, fields)
    try:
        with erpnext_caller.draft_check_as(args, email):
            raw = await drafts._call(erpnext_caller.DRAFT_CHECK_TOOL, args)
    except Exception:
        logger.warning("erpnext_credit: draft_check failed", exc_info=True)
        raise refused(502, "check_failed", "ERPNext didn't answer the check. Try again in a moment.") from None
    checked = drafts._result(raw)
    if checked.get("ok") is not True:
        said = drafts._refusal_text(raw)
        if any(text in said for text in NEEDS_AUTHORITY):
            # Not a failure: the tapper may look, but someone with the
            # authority prepares it.
            return Prepared(None, False, (
                "Only someone with customer credit authority can prepare this raise, so no card was left. "
                f"Ask {who_can_prepare(tapper.id)} to prepare it."
            ))
        raise refused(502, "not_accepted", f"ERPNext did not accept this raise: {said}")
    why_not_card = summary_refusal(name, fields, checked)
    if why_not_card is not None:
        raise refused(409, "no_card", why_not_card)
    prepared_by = tapper.full_name if approver.id != tapper.id else None
    view = card_view(name, fields, checked["summary"], prepared_by)
    why = f"Raise {drafts._one_line(name, drafts.NAME_MAX)}'s credit limit for order {suggestion['sales_order']} (ERPNext alert)"
    # Read again after the check, with no await before the card is stored,
    # so two taps at once leave one card and pass no cap.
    try:
        alert_drafts._already(tapper.id, alert)
    except alert_drafts._Waiting as waiting_card:
        return Prepared(waiting_card.decision_id, False, "The raise is already on a card waiting for approval.")
    waiting = _room(tapper, approver, fields, name)
    approver_email = normalize_email(approver.email or "")
    decision_id, new, _ = drafts.propose(
        approver, DOCTYPE, fields, view, why, asked_by=email, session_id=None, waiting=waiting,
        from_alert=getattr(alert, "id", None), fingerprint=drafts.fingerprint_of(checked),
        operation=OPERATION, name=name,
        extra={
            "approver_person_id": approver.id, "approver_email": approver_email,
            "asked_by_person_id": tapper.id,
            # The card's currency (ERPNext's), for the line a made raise shows.
            "currency": checked["summary"]["credit"]["currency"],
        },
    )
    if new:
        drafts.audit(
            "erpnext_draft_proposed",
            f"Left person {approver.id} a credit-limit raise card from an alert, prepared by person {tapper.id}",
            {
                "person_id": approver.id, "decision_id": decision_id, "doctype": DOCTYPE, "operation": OPERATION,
                "name": name, "alert_id": getattr(alert, "id", None),
                "asked_by_person_id": tapper.id, "approver_person_id": approver.id,
            },
        )
    if approver.id == tapper.id:
        message = "Its card is in Your decision. Nothing changes in ERPNext until you approve it."
    else:
        message = (
            f"Its card is waiting for {drafts._one_line(approver.full_name, drafts.NAME_MAX)} to approve. "
            "Nothing changes in ERPNext until they do."
        )
    return Prepared(decision_id, new, message)


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

def approval_refusal(instance: Any, payload: dict[str, Any], approver: Any) -> None:
    """Raise ``ApproveRefused`` unless ``approver`` may carry out this raise
    now: the switches still on, not a demo person, still holding the
    authority, the card's own approver under the same email, and whoever
    prepared it still that person."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.memory.decision_ledger import close_externally
    from openexecutive.people.store import get_person

    refused = drafts.ApproveRefused
    if not credit_on():
        raise refused(409, "drafts_off", "Raising credit limits is turned off here.")
    if approver.demo:
        raise refused(403, "demo", "A demo account can't approve a credit-limit raise.")
    if not has_authority(approver):
        raise refused(403, "no_authority", "Approving a credit-limit raise needs customer credit authority in People.")
    stored_id = payload.get("approver_person_id")
    if (
        isinstance(stored_id, bool) or not isinstance(stored_id, int)
        or stored_id != approver.id or stored_id != instance.approver_person_id
    ):
        raise refused(403, "not_yours", "Only the person this card is for can approve it.")
    stored_email = payload.get("approver_email")
    if not isinstance(stored_email, str) or normalize_email(approver.email or "") != stored_email:
        raise refused(409, "changed", "Your email changed since this card was left. Dismiss it and prepare it again.")
    asker_id, asked_by = payload.get("asked_by_person_id"), payload.get("asked_by")
    asker = get_person(asker_id) if isinstance(asker_id, int) and not isinstance(asker_id, bool) else None
    if (
        asker is None or not asker.active or not isinstance(asked_by, str)
        or normalize_email(asker.email or "") != normalize_email(asked_by)
    ):
        raise refused(
            409, "changed",
            "Whoever prepared this card has left the team or changed their email since, so it can't be "
            "carried out. Dismiss it and prepare it again.",
        )
    if _sod() and asker.id == approver.id:
        raise refused(409, "separation_of_duties", "Separation of duties is on, so you can't approve a raise you prepared.")
    if drifted(instance.id, approver.id):
        close_externally(instance.id, reason="changed")
        drafts.audit("erpnext_draft_changed", f"Person {approver.id}'s credit-limit raise changed since its card", {
            "person_id": approver.id, "decision_id": instance.id, "doctype": DOCTYPE, "operation": OPERATION,
            "asked_by_person_id": asker.id, "approver_person_id": approver.id,
        })
        raise refused(409, "changed_since_proposed", CHANGED_TEXT)


CHANGED_TEXT = (
    "This raise changed in ERPNext since the card was left (what the customer owes, the order or its credit "
    "limit), so nothing was changed and the card is closed. Prepare it again from the alert to see it as it is now."
)
SOD_TEXT = (
    "Separation of duties is on in ERPNext, so whoever prepared this raise can't approve it. Nothing was "
    "changed and the card is closed."
)


def made_extra(payload: dict[str, Any]) -> dict[str, Any]:
    """What a made raise's line says beyond the customer: the company and the
    new limit, as the card showed them."""
    from openexecutive.integrations.erpnext_drafts import _CURRENCY_RE

    fields = payload.get("fields")
    credit = fields.get("credit") if isinstance(fields, dict) else None
    credit = credit if isinstance(credit, dict) else {}
    currency = payload.get("currency")
    code = currency if isinstance(currency, str) and _CURRENCY_RE.fullmatch(currency) else ""
    limit = _number(credit.get("credit_limit"))
    shown = f"{_money(limit)} {code}".strip() if limit is not None else ""
    return {"credit": {"company": drafts._one_line(credit.get("company"), drafts.NAME_MAX), "limit": shown}}


# ---------------------------------------------------------------------------
# Read again on open
# ---------------------------------------------------------------------------

@dataclass
class _Read:
    at: float
    approver_id: int
    changed: bool


# By decision id; served only to that card's approver.
_RECHECKED: dict[int, _Read] = {}


def drifted(decision_id: int, approver_id: int | None) -> bool:
    """Whether the last fresh read of this card, made as ``approver_id``,
    found it changed."""
    read = _RECHECKED.get(decision_id)
    return read is not None and read.approver_id == approver_id and read.changed


def _forget_old(now: float) -> None:
    for decision_id in [d for d, r in _RECHECKED.items() if now - r.at > RECHECK_KEEP_S]:
        _RECHECKED.pop(decision_id, None)


async def recheck(viewer: Any, cards: list[Any], caller: Any) -> None:
    """Mark ``viewer``'s waiting credit-raise cards (``DraftCard.changed``)
    that ERPNext now reads differently. Only the viewer's own cards, only
    when the API knows the caller is them, at most ``RECHECK_PER_REQUEST``
    reads, least recently read first. Reads and annotates; never changes a
    card. Errors show nothing new."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.verified import caller_refusal
    from openexecutive.memory.decision_ledger import get_decision_instance
    from openexecutive.orchestrator import erpnext_caller

    own = [c for c in cards if c.operation == OPERATION]
    if not own or viewer is None or viewer.id is None:
        return
    email = normalize_email(viewer.email or "")
    if "@" not in email or caller_refusal(caller, email) is not None or not credit_on():
        return
    now = time.monotonic()
    _forget_old(now)
    due: list[Any] = []
    for card in own:
        read = _RECHECKED.get(card.decision_id)
        if read is not None and read.approver_id == viewer.id:
            card.changed = read.changed
            if now - read.at < RECHECK_TTL_S:
                continue
        due.append(card)

    def last_read(card: Any) -> float:
        read = _RECHECKED.get(card.decision_id)
        return read.at if read is not None and read.approver_id == viewer.id else -math.inf

    for card in sorted(due, key=last_read)[:RECHECK_PER_REQUEST]:
        instance = get_decision_instance(card.decision_id)
        payload = drafts._payload(instance) if instance is not None else {}
        fields, name, expected = credit_fields(payload.get("fields")), payload.get("name"), payload.get("fingerprint")
        if (
            instance is None or instance.approver_person_id != viewer.id
            or payload.get("approver_email") != email or fields is None
            or _doc_name(name) is None or not isinstance(expected, str)
        ):
            continue
        args = check_args(str(name), fields)
        try:
            with erpnext_caller.draft_recheck_as(args, email):
                raw = await drafts._call(erpnext_caller.DRAFT_CHECK_TOOL, args)
        except Exception:
            logger.warning("erpnext_credit: the fresh check of card %d failed", card.decision_id, exc_info=True)
            continue
        checked = drafts._result(raw)
        if checked.get("ok") is not True:
            continue
        summary = checked.get("summary")
        order = summary.get("sales_order") if isinstance(summary, dict) else None
        docstatus = order.get("docstatus") if isinstance(order, dict) else None
        changed = drafts.fingerprint_of(checked) != expected or isinstance(docstatus, bool) or docstatus != 0
        _RECHECKED[card.decision_id] = _Read(at=time.monotonic(), approver_id=int(viewer.id), changed=changed)
        card.changed = changed

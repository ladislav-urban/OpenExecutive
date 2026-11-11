"""The invoice-capture job's fill card: a supplier's mailed draft Purchase
Invoice filled from the submitted Purchase Order or Purchase Receipt it
bills, through ERPNext's own mapper, as the approver (oe_connector's
``draft_fill_purchase_invoice``, ladislav-urban/oe_connector#545).

ERPNext's Email Account makes an empty draft Purchase Invoice from each
mail a supplier sends, and files the mail. The draft stays a draft: approving
the card fills its supplier, rows, taxes, bill number and date, and nothing
is submitted, paid or posted.

**Only from the invoice-capture job.** The job's code picks the source
(``watch.jobs.invoice_capture``) from oe_connector's deterministic reading of
the PDF, never from a model; ERPNext's ``draft_check`` (operation ``fill``)
builds the fill as the job's reviewer, and the card is left by code
(``erpnext_drafts.leave_job_card``). No chat turn or ERPNext panel can
propose one: ``erpnext_changes.enabled("fill")`` lists nothing.

**The sender gate** (``fill_allowed``, the owner's choice, Nov 11, 2026): a
card only when the mail's own From address was confirmed as it arrived
(``sender_from``), the account's server is stated to enforce DMARC
(``sender_checked``), and the From domain asks it to reject failing mail,
or to quarantine it with the account's own authserv-id check passed for
this mail. oe_connector refuses the same cases (``draft_fill.fills``).

**Off two ways besides the job,** each checked again at approval
(``approval_refusal``): ``ERPNEXT_INVOICE_FILL`` on this server, and the pin
(``draft_fill_purchase_invoice`` pinned and found with that definition).

**What the card must read back** (``echo_refusal``): the draft, source, bill
number and date the job built; ERPNext's summary complete (a missing key is
"can't tell", so no card); the gate; no other invoice of the supplier with
that bill number; the draft in the company's currency, its grand total the
PDF's; the reviewer the draft's assignee (only its assignee approves a fill)
and no creator of the order or receipt it bills. ``slice_of`` keeps a
cleaned, capped copy of what approval re-checks, stored on the card.

**At approval** (``approval_refusal``, before the claim), from that stored
slice: the switches, the gate, the bill number, the approver the assignee
and not a creator. Then, holding the claim, the job's day cap on the
amount filled (``take_approved``), as for bank matches.

**What approval sends**: ``{name, source, lower, bill_no, bill_date,
idempotency_key, asked_by: "job:invoice-capture", configured_by,
expected_fingerprint}``, a fixed set with no ``doctype``. oe_connector
rebuilds the fill under lock and refuses any difference from its
fingerprint, then applies its own gate, separation of duties and caps.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

TOOL = "erpnext__draft_fill_purchase_invoice"
PIN_NAME = "draft_fill_purchase_invoice"
OPERATION = "fill"
DOCTYPE = "Purchase Invoice"
JOB_ID = "invoice-capture"
SOURCES = ("Purchase Order", "Purchase Receipt")
# oe_connector's draft_fill._BILL_NO_RE: what a bill number may be.
BILL_NO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ./_\-]{0,39}")
DMARC_CHECKED = "dmarc-enforced"
POLICIES = ("reject", "quarantine", "none", "missing", "lookup_failed")
MAX_ROWS = 100
MAX_PEOPLE = 20
# The job's amount_cap_day: its default and the most it may be set to.
AMOUNT_CAP_DEFAULT = 50_000
AMOUNT_CAP_MAX = 1_000_000
_EMAIL_MAX = 320
_TEXT_MAX = 140
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_FIELDS = frozenset({"source", "lower", "bill_no", "bill_date"})
_SOURCE_KEYS = frozenset({"doctype", "name"})

# How oe_connector's final refusals of a fill read (draft_fill.py), each with
# the code and the words the person is told. Approving again would refuse
# again, so each closes the card.
FINAL: tuple[tuple[str, str, str], ...] = (
    (
        "already carries bill number", "bill_no_in_use",
        "Another of the supplier's invoices already carries this bill number in ERPNext, so nothing was filled "
        "and the card is closed.",
    ),
    (
        "only its assignee approves its fill", "not_assignee",
        "Only the person the draft is assigned to approves its fill, so nothing was filled and the card is "
        "closed. The next run leaves it for the assignee.",
    ),
    (
        "The mail's sender can't be trusted to fill an invoice", "sender_untrusted",
        "ERPNext can't trust the mail's sender to fill an invoice now, so nothing was filled and the card is "
        "closed. Fill the draft by hand if the invoice is genuine.",
    ),
    (
        "a fill comes only from the invoice-capture job's card", "not_from_job",
        "ERPNext takes a fill only from the invoice-capture job's card. Nothing was filled and the card is "
        "closed.",
    ),
    (
        "only an empty mailed draft is filled", "already_filled",
        "The draft isn't empty any more in ERPNext, so nothing was filled and the card is closed.",
    ),
    (
        "isn't a draft", "not_a_draft",
        "The invoice isn't a draft any more in ERPNext, so nothing was filled and the card is closed.",
    ),
)
CHANGED_TEXT = (
    "The draft, its mail, the supplier or the order or receipt changed in ERPNext since the card was left, so "
    "nothing was filled and the card is closed. The next run reads it as it is now."
)
LIMIT_TEXT = (
    "Today's limit on invoices filled from here is reached, so nothing was filled. The card waits: approve it "
    "again tomorrow, or fill the draft in ERPNext."
)


def switch_on() -> bool:
    """``ERPNEXT_INVOICE_FILL``: this server's switch for fill cards."""
    from openexecutive.config import get_settings

    return bool(get_settings().erpnext_invoice_fill)


async def pinned() -> bool:
    """Whether the gateway serves ``draft_fill_purchase_invoice`` with exactly
    the pinned definition."""
    from openexecutive.orchestrator.erpnext_tool_pin import TOOL_DIGESTS
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_SERVER

    if PIN_NAME not in TOOL_DIGESTS:
        return False
    gateway = get_active_gateway()
    if gateway is None or ERPNEXT_SERVER not in gateway_server_names(gateway):
        return False
    try:
        return bool(await gateway.verified_erpnext_tools([TOOL]))
    except Exception:
        return False


async def unavailable() -> str | None:
    """Why a fill card can't be left or approved now, or None: this server's
    switch and the pin, in that order."""
    if not switch_on():
        return "fill_switch_off"
    if not await pinned():
        return "fill_not_pinned"
    return None


def company_today() -> date:
    from openexecutive.memory.workspace_settings import get_user_timezone

    return datetime.now(get_user_timezone()).date()


# --------------------------------------------------------------------------- #
# The owner's sender gate
# --------------------------------------------------------------------------- #


def fill_allowed(facts: Mapping[str, Any]) -> bool:
    """The owner's gate (Nov 11, 2026), the one rule both the card and its
    approval use: the account's server enforces DMARC and the mail's own
    From was confirmed, and the From domain asks it to reject failing mail,
    or to quarantine it with the account's authserv-id check passed. A
    missing or malformed key is never enough."""
    if not isinstance(facts, Mapping):
        return False
    sender_from, authserv = facts.get("sender_from"), facts.get("authserv_id")
    if facts.get("sender_checked") != DMARC_CHECKED or not isinstance(sender_from, str) or not sender_from.strip():
        return False
    policy = facts.get("sender_policy")
    return policy == "reject" or (policy == "quarantine" and isinstance(authserv, str) and bool(authserv.strip()))


def gate_reason(facts: Mapping[str, Any]) -> str:
    """Why the gate refuses, in code's words around oe_connector's reasons
    (shown inside a notice's untrusted block)."""
    got: Mapping[str, Any] = facts if isinstance(facts, Mapping) else {}
    if not got.get("sender_from"):
        why = got.get("sender_from_reason")
        return f"The mail's own From address wasn't confirmed: {_text(why) or 'no reason given'}"
    if got.get("sender_checked") != DMARC_CHECKED:
        why = got.get("sender_checked_reason")
        return f"The mail's sender wasn't checked by DMARC: {_text(why) or 'no reason given'}"
    policy = got.get("sender_policy")
    domain = _text(got.get("sender_policy_domain")) or "its domain"
    if policy == "quarantine":
        return (
            f"{domain} asks only to quarantine mail that fails DMARC, and this account names no authserv-id "
            "check for its own server's verdict"
        )
    if policy in ("none", "missing"):
        return f"{domain} doesn't ask for mail that fails DMARC to be refused (its policy: {policy})"
    return f"{domain}'s DMARC policy couldn't be read"


# --------------------------------------------------------------------------- #
# The card's fields
# --------------------------------------------------------------------------- #


def _doc_name(value: Any) -> str | None:
    from openexecutive.integrations.erpnext_changes import _DOC_RE

    return value if isinstance(value, str) and _DOC_RE.fullmatch(value) else None


def _day(value: Any) -> str | None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value[:10]):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def fill_fields(fields: Any, name: Any) -> dict[str, Any] | str:
    """A fill card's fields as they are checked, stored and sent:
    ``{source: {doctype, name}, lower: [], bill_no, bill_date}`` for the
    draft ``name``, or why not. In v1 no row is lowered: a total that
    differs from the PDF's leaves no card."""
    if _doc_name(name) is None:
        return "A fill card names the draft it fills."
    if not isinstance(fields, dict) or set(fields) != _FIELDS:
        return "A fill card's fields are {source, lower, bill_no, bill_date}."
    source = fields.get("source")
    if not isinstance(source, dict) or set(source) != _SOURCE_KEYS:
        return "source is {doctype, name}."
    doctype, source_name = source.get("doctype"), _doc_name(source.get("name"))
    if doctype not in SOURCES or source_name is None:
        return "source is a Purchase Order or Purchase Receipt, by its name."
    if fields.get("lower") != []:
        return "A fill lowers no row (lower is [])."
    bill_no, bill_date = fields.get("bill_no"), fields.get("bill_date")
    if not isinstance(bill_no, str) or not BILL_NO_RE.fullmatch(bill_no):
        return "bill_no is the supplier's invoice number."
    if not isinstance(bill_date, str) or _day(bill_date) != bill_date:
        return "bill_date is YYYY-MM-DD."
    return {"source": {"doctype": str(doctype), "name": source_name}, "lower": [], "bill_no": bill_no,
            "bill_date": bill_date}


# --------------------------------------------------------------------------- #
# ERPNext's check: what it must read back, and the slice the card keeps
# --------------------------------------------------------------------------- #


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _email(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > _EMAIL_MAX:
        return None
    text = value.strip().lower()
    return text if text.isprintable() and " " not in text else None


def _text(value: Any, limit: int = _TEXT_MAX) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _emails(value: Any) -> list[str] | None:
    if not isinstance(value, list) or len(value) > MAX_PEOPLE:
        return None
    kept = [_email(v) for v in value]
    if any(v is None for v in kept):
        return None
    return sorted({v for v in kept if v is not None})


_SUMMARY_KEYS = (
    "company", "source", "supplier", "sender_from", "sender_from_reason", "authserv_id", "sender_checked",
    "sender_policy", "sender_policy_domain", "currency", "credit_to", "rows", "taxes", "totals", "bill_no",
    "bill_date", "bill_no_collides", "creators", "assigned_to", "can_write",
)


def slice_of(summary: Any) -> dict[str, Any] | None:
    """The part of a fill check's summary the card keeps for approval,
    cleaned and capped, or None when the summary can't be read whole: a key
    the rules rely on missing ("can't tell"), a value of the wrong kind, more
    than a hundred rows or twenty people."""
    if not isinstance(summary, dict) or any(k not in summary for k in _SUMMARY_KEYS):
        return None
    company, source, supplier = _doc_name(summary.get("company")), summary.get("source"), summary.get("supplier")
    if company is None or not isinstance(source, dict) or not isinstance(supplier, dict):
        return None
    source_doctype, source_name = source.get("doctype"), _doc_name(source.get("name"))
    supplier_name = _doc_name(supplier.get("name"))
    if source_doctype not in SOURCES or source_name is None or supplier_name is None:
        return None
    currency, rows, totals = summary.get("currency"), summary.get("rows"), summary.get("totals")
    if (
        not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency) or not isinstance(rows, list)
        or not 1 <= len(rows) <= MAX_ROWS or not isinstance(totals, dict) or not isinstance(summary.get("taxes"), list)
    ):
        return None
    net, grand = _amount(totals.get("net_total")), _amount(totals.get("grand_total"))
    bill_no, bill_date = summary.get("bill_no"), _day(summary.get("bill_date"))
    if net is None or grand is None or not isinstance(bill_no, str) or not BILL_NO_RE.fullmatch(bill_no):
        return None
    if bill_date is None:
        return None
    collides = summary.get("bill_no_collides")
    if collides is not None and _doc_name(collides) is None:
        return None
    creators, assigned = _emails(summary.get("creators")), _emails(summary.get("assigned_to"))
    if creators is None or assigned is None or not isinstance(summary.get("can_write"), bool):
        return None
    policy = summary.get("sender_policy")
    if policy not in POLICIES:
        return None
    sender_from = summary.get("sender_from")
    authserv = summary.get("authserv_id")
    checked = summary.get("sender_checked")
    if (
        (sender_from is not None and _email(sender_from) is None)
        or (authserv is not None and (not isinstance(authserv, str) or not _text(authserv)))
        or (checked is not None and checked != DMARC_CHECKED)
    ):
        return None
    for row in rows:
        if not isinstance(row, dict) or _amount(row.get("qty")) is None or _amount(row.get("rate")) is None:
            return None
    return {
        "company": company,
        "source": {"doctype": str(source_doctype), "name": source_name},
        "supplier": supplier_name,
        "sender_from": _email(sender_from) if sender_from is not None else None,
        "authserv_id": _text(authserv) if authserv is not None else None,
        "sender_checked": checked,
        "sender_policy": policy,
        "sender_policy_domain": _text(summary.get("sender_policy_domain"), 253),
        "currency": currency,
        "rows": len(rows),
        "net_total": net,
        "grand_total": grand,
        "bill_no": bill_no,
        "bill_date": bill_date,
        "bill_no_collides": collides,
        "creators": creators,
        "assigned_to": assigned,
        "can_write": summary["can_write"],
    }


def _cents(value: float) -> int:
    return round(value * 100)


def slice_refusal(kept: dict[str, Any], fields: Any) -> str | None:
    """Why a kept slice can't be a card (or approved), as a reason code, or
    None: the source, bill number and date the card's own; the owner's
    sender gate; no other invoice with that bill number; a whole fill the
    approver may save."""
    if not isinstance(fields, dict) or (
        kept["source"] != fields.get("source") or kept["bill_no"] != fields.get("bill_no")
        or kept["bill_date"] != fields.get("bill_date")
    ):
        return "check_mismatch"
    if not fill_allowed(kept):
        return "check_fill_sender"
    if kept["bill_no_collides"] is not None:
        return "check_fill_collides"
    if _cents(kept["grand_total"]) <= 0:
        return "check_fill_total"
    if not kept["can_write"]:
        return "check_permission"
    return None


def is_assignee(kept: dict[str, Any], email: str) -> bool:
    me = _email(email)
    return me is not None and "@" in me and me in kept["assigned_to"]


def names_person(kept: dict[str, Any], email: str) -> bool:
    """Whether ERPNext names this person (by normalized email) as a creator
    of the order or receipt the fill bills."""
    me = _email(email)
    return me is not None and "@" in me and me in kept["creators"]


def echo_refusal(
    name: str, fields: dict[str, Any], checked: dict[str, Any], expect: dict[str, Any], reviewer_email: str,
) -> str | None:
    """Why ERPNext's fill check can't become a card, as a reason code, or
    None. ``expect``: what the job read itself, as the job's user: the
    company, supplier and confirmed From, the company's currency and the
    PDF's total."""
    summary = checked.get("summary")
    if (
        checked.get("operation") != OPERATION or checked.get("doctype") != DOCTYPE or checked.get("name") != name
        or not isinstance(summary, dict) or summary.get("operation") != OPERATION
        or summary.get("doctype") != DOCTYPE or summary.get("name") != name
    ):
        return "check_mismatch"
    kept = slice_of(summary)
    if kept is None or any(
        kept.get(k) != expect.get(k) for k in ("company", "supplier", "sender_from")
    ):
        return "check_mismatch"
    why = slice_refusal(kept, fields)
    if why is not None:
        return why
    if kept["currency"] != expect.get("company_currency"):
        return "check_fill_currency"
    total = _amount(expect.get("pdf_total"))
    if total is None or _cents(total) != _cents(kept["grand_total"]):
        return "check_fill_total"
    if not is_assignee(kept, reviewer_email):
        return "check_fill_assignee"
    if names_person(kept, reviewer_email):
        return "check_fill_sod"
    return None


def card_amount(checked: dict[str, Any]) -> float | None:
    """What a checked fill counts against the day's cap: its grand total, in
    the company's currency (a fill in another currency leaves no card)."""
    kept = slice_of(checked.get("summary"))
    return kept["grand_total"] if kept is not None else None


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #


def card_view(summary: Any) -> dict[str, Any]:
    """What the card shows, from ERPNext's check: the draft, the sender's
    checks, the source, each row the fill bills, the taxes and totals."""
    from openexecutive.integrations.erpnext_drafts import _one_line, _shown

    got: dict[str, Any] = summary if isinstance(summary, dict) else {}
    currency = _shown(got.get("currency"))

    def money(value: Any) -> str:
        number = _amount(value)
        return f"{number:,.2f} {currency}".strip() if number is not None else ""

    source = got.get("source") if isinstance(got.get("source"), dict) else {}
    supplier = got.get("supplier") if isinstance(got.get("supplier"), dict) else {}
    totals = got.get("totals") if isinstance(got.get("totals"), dict) else {}
    assert isinstance(source, dict) and isinstance(supplier, dict) and isinstance(totals, dict)
    authserv = got.get("authserv_id")
    shown: list[dict[str, str]] = [
        {"label": "Draft invoice", "value": f"{DOCTYPE} {_shown(got.get('name'))}"},
        {"label": "Company", "value": _shown(got.get("company"))},
        {"label": "Supplier", "value": f"{_shown(supplier.get('supplier_name'))} ({_shown(supplier.get('name'))})"},
        {"label": "Sent from (the mail's own From)", "value": _shown(got.get("sender_from"))},
        {"label": "Sender's domain policy",
         "value": f"{_shown(got.get('sender_policy'))} ({_shown(got.get('sender_policy_domain'))})"},
        {"label": "Checked by", "value": _shown(authserv) if authserv else "the account's mail server (stated)"},
        {"label": "Bills", "value": f"{_shown(source.get('doctype'))} {_shown(source.get('name'))}"},
        {"label": "Supplier's invoice no.", "value": _one_line(got.get("bill_no"), 60)},
        {"label": "Supplier's invoice date", "value": _shown(got.get("bill_date"))},
        {"label": "Net total", "value": money(totals.get("net_total"))},
        {"label": "Grand total", "value": money(totals.get("grand_total"))},
        {"label": "Payable account", "value": _shown(got.get("credit_to"))},
    ]
    taxes = got.get("taxes")
    for tax in taxes if isinstance(taxes, list) else []:
        if isinstance(tax, list) and len(tax) == 3:
            shown.append({"label": f"Tax ({_shown(tax[0])})", "value": f"{_shown(tax[1])}: {money(tax[2])}"})
    rows = got.get("rows")
    lines: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        lines.append({
            "row": _shown(row.get("row")),
            "item": _shown(row.get("item_code")),
            "qty": _shown(row.get("qty")),
            "rate": money(row.get("rate")),
            "amount": money(row.get("amount")),
        })
    notes = [
        "Approving fills this draft invoice in ERPNext from the order or receipt shown, through ERPNext's own "
        "mapper, as you. It stays a draft: nothing is submitted, paid or posted.",
        "The supplier's invoice is outsiders' text. Check the PDF against the rows and totals before approving.",
    ]
    return {"fields": shown, "lines": lines, "notes": notes}


def made(name: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """What approval did, from oe_connector's result, or None when it isn't a
    success: ``ok`` with the fill of this draft, fresh or a repeat of the
    card's key (``already_done``)."""
    if result.get("ok") is not True:
        return None
    if (result.get("operation"), result.get("doctype"), result.get("name")) != (OPERATION, DOCTYPE, name):
        return None
    return {"doctype": DOCTYPE, "name": name, "operation": OPERATION, "already_created": result.get("already_done") is True}


def final_refusal(reason: str) -> tuple[str, str] | None:
    """The code and words of a final refusal of a fill, from oe_connector's
    reason (anywhere in it), or None."""
    for marker, code, text in FINAL:
        if marker in reason:
            return code, text
    return None


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


def _malformed() -> Exception:
    from openexecutive.integrations.erpnext_drafts import ApproveRefused

    return ApproveRefused(409, "malformed", "This card can't be carried out. Dismiss it.")


async def approval_refusal(instance: Any, payload: dict[str, Any], person: Any) -> None:
    """A fill card at approval, before anything is claimed: raises
    ``ApproveRefused`` unless it is the invoice-capture job's card,
    ``ERPNEXT_INVOICE_FILL`` and the pin hold now, and the slice stored with
    it still passes the owner's gate, with the approver the draft's assignee.
    A slice naming the approver as a creator of the source closes the card
    (separation of duties)."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.integrations.erpnext_drafts import JOB_SOURCE, ApproveRefused, _sod_refused
    from openexecutive.watch import registry

    job = registry.get(payload.get("job"))
    if payload.get("source") != JOB_SOURCE or job is None or job.id != JOB_ID or payload.get("doctype") != DOCTYPE:
        raise _malformed()
    if await unavailable() is not None:
        raise ApproveRefused(
            409, "fill_off",
            "Filling mailed invoices from here is turned off now, so nothing was filled. Fill the draft in "
            "ERPNext if it should still be filled.",
        )
    kept = slice_of_stored(payload.get("slice"))
    if kept is None or slice_refusal(kept, payload.get("fields")) is not None:
        raise _malformed()
    email = normalize_email(person.email or "")
    if not is_assignee(kept, email):
        raise ApproveRefused(
            409, "not_assignee",
            "Only the person the draft is assigned to approves its fill. Assign it to yourself in ERPNext and "
            "wait for the next run's card.",
        )
    if names_person(kept, email):
        raise _sod_refused(instance, payload, person, "erp_data")


def slice_of_stored(stored: Any) -> dict[str, Any] | None:
    """A stored slice read back for approval, or None when it isn't exactly
    what ``slice_of`` keeps."""
    if not isinstance(stored, dict):
        return None
    keys = {
        "company", "source", "supplier", "sender_from", "authserv_id", "sender_checked", "sender_policy",
        "sender_policy_domain", "currency", "rows", "net_total", "grand_total", "bill_no", "bill_date",
        "bill_no_collides", "creators", "assigned_to", "can_write",
    }
    if set(stored) != keys or not isinstance(stored.get("rows"), int) or isinstance(stored.get("rows"), bool):
        return None
    supplier = stored.get("supplier")
    summary = {
        "company": stored.get("company"),
        "source": stored.get("source"),
        "supplier": {"name": supplier},
        "sender_from": stored.get("sender_from"),
        "sender_from_reason": "",
        "authserv_id": stored.get("authserv_id"),
        "sender_checked": stored.get("sender_checked"),
        "sender_policy": stored.get("sender_policy"),
        "sender_policy_domain": stored.get("sender_policy_domain"),
        "currency": stored.get("currency"),
        "credit_to": "",
        "rows": [{"qty": 0, "rate": 0}] * max(0, min(int(stored["rows"]), MAX_ROWS + 1)),
        "taxes": [],
        "totals": {"net_total": stored.get("net_total"), "grand_total": stored.get("grand_total")},
        "bill_no": stored.get("bill_no"),
        "bill_date": stored.get("bill_date"),
        "bill_no_collides": stored.get("bill_no_collides"),
        "creators": stored.get("creators"),
        "assigned_to": stored.get("assigned_to"),
        "can_write": stored.get("can_write"),
    }
    again = slice_of(summary)
    return again if again == stored else None


def take_approved(payload: dict[str, Any], decision_id: int, person_id: int) -> bool:
    """Count this card against what was approved of the job's cards today
    (the company's day), by anyone, within the job's ``amount_cap_day``, in
    one transaction. Whether it fits. Fails closed."""
    from openexecutive.watch import registry
    from openexecutive.watch.settings import effective_settings, get_row, take_approved_amount

    try:
        job = registry.get(JOB_ID)
        kept = slice_of_stored(payload.get("slice"))
        if job is None or kept is None:
            return False
        cap = effective_settings(job, get_row(job.id)).get("amount_cap_day")
        if not isinstance(cap, int) or isinstance(cap, bool):
            return False
        return take_approved_amount(
            job.id, company_today().isoformat(), person_id, decision_id, float(kept["grand_total"]),
            float(min(cap, AMOUNT_CAP_MAX)),
        )
    except Exception:
        return False


def amount_cap(job: Any) -> float | None:
    """The job's ``amount_cap_day`` as it is now, or None."""
    from openexecutive.watch.settings import effective_settings, get_row

    try:
        cap = effective_settings(job, get_row(job.id)).get("amount_cap_day")
    except Exception:
        return None
    if not isinstance(cap, int) or isinstance(cap, bool):
        return None
    return float(min(cap, AMOUNT_CAP_MAX))

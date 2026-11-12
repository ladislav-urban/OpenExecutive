"""The payment-checks job's block card: ERPNext's own Block Invoice on a
submitted supplier invoice that likely repeats another (oe_connector's
``draft_block_invoice``, ladislav-urban/oe_connector#530).

The one card whose approval changes a submitted document, and only by
ERPNext's own reversible hold: the invoice can't be paid until a person
releases it with Unblock Invoice, or its release date passes. Nothing here
unblocks, pays, submits or cancels.

**Only from the payment-checks job.** The job's code finds the duplicate
(watch.jobs.payment_checks), ERPNext's ``draft_check`` (operation ``block``)
reads it as the job's reviewer, and the card is left by code
(``erpnext_drafts.leave_job_card``). No chat turn or ERPNext panel can
propose one: ``erpnext_changes.enabled("block")`` lists nothing, so
``check_change`` refuses it, and the propose tool never offers it.

**Off three ways,** each checked again when the card is approved
(``approval_refusal``), not only when it is left:

1. the job's own ``block_submitted`` setting (off by default), as it is now;
2. ``ERPNEXT_BLOCK_INVOICE`` on this server (off by default);
3. the pin: oe_connector's ``draft_block_invoice`` among the pinned tools
   (``erpnext_tool_pin.TOOL_DIGESTS``) and found by the gateway with that
   exact definition.

Until all three hold, a submitted duplicate leaves a notice saying to use
Block Invoice in ERPNext.

**What approval sends** (``erpnext_drafts._carry_out``, only for a card whose
source is the watch and whose job is payment-checks): ``{name, duplicate_of,
release_date, idempotency_key, asked_by: "job:payment-checks", configured_by,
expected_fingerprint}``. The release date is the card's own, never "14 days
from approval". oe_connector writes the hold comment from its own template.

**Final refusals** (``FINAL``): the invoice changed since the card
(``CHANGED_SINCE_PROPOSED``), separation of duties, a blocking limit, an
invoice a person unblocked before, and a block not from the job's card. Each
closes the card: approving again would refuse again.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

TOOL = "erpnext__draft_block_invoice"
PIN_NAME = "draft_block_invoice"
OPERATION = "block"
DOCTYPE = "Purchase Invoice"
JOB_ID = "payment-checks"
# The card's release date: this many days after the run's day, at most
# MAX_RELEASE_DAYS ahead when approved (oe_connector's own bounds).
RELEASE_DAYS = 14
MAX_RELEASE_DAYS = 90
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FIELDS = frozenset({"duplicate_of", "release_date"})

# How oe_connector's final refusals of a block begin or read (block_invoice.py),
# each with the code and the words the person is told. Approving again would
# refuse again, so each closes the card.
FINAL: tuple[tuple[str, str, str], ...] = (
    (
        "Blocking limit", "blocking_limit",
        "ERPNext's limit on holds from this app is reached (for the day, the supplier or the site), so "
        "nothing was held and the card is closed. A person can still use Block Invoice in ERPNext.",
    ),
    (
        "unblocked by a person", "unblocked_before",
        "A person unblocked this invoice after an earlier hold, so it isn't held again from here. Nothing "
        "was held and the card is closed.",
    ),
    (
        "a block comes only from the payment-checks job's card", "not_from_job",
        "ERPNext takes a block only from the payment-checks job's card. Nothing was held and the card is "
        "closed.",
    ),
)
CHANGED_TEXT = (
    "The invoice, the one it repeats or a payment against it changed in ERPNext since the card was left, so "
    "nothing was held and the card is closed. The next payment check reads it as it is now."
)


def switch_on() -> bool:
    """``ERPNEXT_BLOCK_INVOICE``: this server's switch for block cards."""
    from openexecutive.config import get_settings

    return bool(get_settings().erpnext_block_invoice)


def setting_on(job: Any, row: Any) -> bool:
    """The job's ``block_submitted`` setting as it is now (its effective
    settings: stored ones that still fit, else its defaults)."""
    from openexecutive.watch.settings import effective_settings

    return effective_settings(job, row).get("block_submitted") is True


async def pinned() -> bool:
    """Whether the gateway serves ``draft_block_invoice`` with exactly the
    pinned definition: listed in the pin, and found by the gateway's search
    matching it (which also lets the gateway call it)."""
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


async def unavailable(job: Any, row: Any) -> str | None:
    """Why a block card can't be left or approved now, or None: the job's
    setting, this server's switch and the pin, in that order."""
    if not setting_on(job, row):
        return "block_setting_off"
    if not switch_on():
        return "block_switch_off"
    if not await pinned():
        return "block_not_pinned"
    return None


def _company_today() -> date:
    from datetime import datetime

    from openexecutive.memory.workspace_settings import get_user_timezone

    return datetime.now(get_user_timezone()).date()


def release_ok(value: Any, today: date | None = None) -> bool:
    """A release date: YYYY-MM-DD, after today (the company's), and at most
    ``MAX_RELEASE_DAYS`` ahead."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return False
    now = today or _company_today()
    return now < day <= now + timedelta(days=MAX_RELEASE_DAYS)


def block_fields(fields: Any, name: Any, *, today: date | None = None) -> dict[str, Any] | str:
    """A block card's fields as they are checked, stored and sent:
    ``{duplicate_of, release_date}`` for the invoice ``name``, or why not.
    Both names as ERPNext gives them, never the same invoice; the release
    date after today and at most 90 days ahead."""
    from openexecutive.integrations.erpnext_changes import _DOC_RE

    if not isinstance(name, str) or not _DOC_RE.fullmatch(name):
        return "A block card names the invoice it holds."
    if not isinstance(fields, dict) or set(fields) != _FIELDS:
        return "A block card's fields are duplicate_of and release_date."
    other = fields.get("duplicate_of")
    if not isinstance(other, str) or not _DOC_RE.fullmatch(other) or other == name:
        return "duplicate_of is the other invoice, by its name."
    if not release_ok(fields.get("release_date"), today):
        return f"release_date is after today and at most {MAX_RELEASE_DAYS} days ahead."
    return {"duplicate_of": other, "release_date": fields["release_date"]}


def echo_refusal(name: str, fields: dict[str, Any], checked: dict[str, Any], expect: dict[str, Any]) -> str | None:
    """Why ERPNext's block check can't become a card, or None: it must read
    back the block of this invoice, as a duplicate of the other one, with the
    card's release date, and what the job expects of it (its supplier and
    company)."""
    summary = checked.get("summary")
    if not isinstance(summary, dict):
        return "check_mismatch"
    other = summary.get("duplicate_of")
    if (
        checked.get("operation") != OPERATION or checked.get("doctype") != DOCTYPE or checked.get("name") != name
        or summary.get("operation") != OPERATION or summary.get("name") != name
        or not isinstance(other, dict) or other.get("name") != fields.get("duplicate_of")
        or summary.get("release_date") != fields.get("release_date")
        or any(summary.get(k) != v for k, v in expect.items())
    ):
        return "check_mismatch"
    return None


def card_view(summary: Any) -> dict[str, Any]:
    """What the card shows, from ERPNext's check: the invoice and the one it
    repeats, their figures, why it repeats it, the payments under way, and
    how long the hold lasts."""
    from openexecutive.integrations.erpnext_drafts import _shown

    got: dict[str, Any] = summary if isinstance(summary, dict) else {}
    found = got.get("duplicate_of")
    other: dict[str, Any] = found if isinstance(found, dict) else {}
    currency = _shown(got.get("currency"))

    def money(value: Any) -> str:
        return f"{_shown(value)} {currency}".strip()

    shown: list[dict[str, str]] = [
        {"label": "Document", "value": f"{DOCTYPE} {_shown(got.get('name'))}"},
        {"label": "Supplier", "value": _shown(got.get("supplier"))},
        {"label": "Company", "value": _shown(got.get("company"))},
        {"label": "Supplier invoice no.", "value": _shown(got.get("bill_no"))},
        {"label": "Supplier invoice date", "value": _shown(got.get("bill_date"))},
        {"label": "Grand total", "value": money(got.get("grand_total"))},
        {"label": "Outstanding", "value": money(got.get("outstanding_amount"))},
        {"label": "Paid so far", "value": money(got.get("paid_amount"))},
        {"label": "Due date", "value": _shown(got.get("due_date"))},
        {"label": "Likely repeats", "value": f"{DOCTYPE} {_shown(other.get('name'))}"},
        {"label": "Its supplier invoice no.", "value": _shown(other.get("bill_no"))},
        {"label": "Its supplier invoice date", "value": _shown(other.get("bill_date"))},
        {"label": "Its grand total", "value": money(other.get("grand_total"))},
        {"label": "Its outstanding", "value": money(other.get("outstanding_amount"))},
        {"label": "Why", "value": _shown(got.get("reason"))},
        {"label": "Hold comment", "value": _shown(got.get("hold_comment"))},
        {"label": "Release date", "value": _shown(got.get("release_date"))},
    ]
    pending = got.get("payments_under_way")
    under_way: dict[str, Any] = pending if isinstance(pending, dict) else {}
    for key, label in (
        ("draft_payment_entries", "Draft payments"),
        ("open_payment_requests", "Open payment requests"),
        ("payment_orders", "Payment orders"),
    ):
        names = under_way.get(key)
        listed = [_shown(n) for n in names if isinstance(n, str)] if isinstance(names, list) else []
        shown.append({"label": label, "value": ", ".join(listed[:10]) if listed else "none"})
    notes = [
        "Approving holds this invoice in ERPNext with its own Block Invoice, as you: it can't be paid, and is "
        f"held until someone unblocks it, at the latest {_shown(got.get('release_date'))}. Nothing is paid, "
        "submitted or cancelled.",
    ]
    if under_way.get("payment_orders"):
        notes.append("A payment order names this invoice: the money may already be with the bank, which a hold can't stop.")
    return {"fields": shown, "lines": [], "notes": notes}


def made(name: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """What approval did, from oe_connector's result, or None when it isn't a
    success: ``ok`` with the block of this invoice, fresh or a repeat of the
    card's key (``already_done``)."""
    if result.get("ok") is not True:
        return None
    if (result.get("operation"), result.get("doctype"), result.get("name")) != (OPERATION, DOCTYPE, name):
        return None
    return {"doctype": DOCTYPE, "name": name, "operation": OPERATION, "already_created": result.get("already_done") is True}


def final_refusal(reason: str) -> tuple[str, str] | None:
    """The code and words of a final refusal of a block, from oe_connector's
    reason (anywhere in it: the MCP layer may put its own words first), or
    None. ``CHANGED_SINCE_PROPOSED`` and separation of duties are the
    executor's own, shared with every job card."""
    for marker, code, text in FINAL:
        if marker in reason:
            return code, text
    return None


async def approval_refusal(payload: dict[str, Any]) -> None:
    """A block card at approval, before anything is claimed: raises
    ``ApproveRefused`` unless it is the payment-checks job's card and the
    job's ``block_submitted``, ``ERPNEXT_BLOCK_INVOICE`` and the pin all hold
    now."""
    from openexecutive.integrations.erpnext_drafts import JOB_SOURCE, ApproveRefused
    from openexecutive.watch import registry
    from openexecutive.watch.settings import get_row

    job = registry.get(payload.get("job"))
    if payload.get("source") != JOB_SOURCE or job is None or job.id != JOB_ID or payload.get("doctype") != DOCTYPE:
        raise ApproveRefused(409, "malformed", "This card can't be carried out. Dismiss it.")
    why = await unavailable(job, get_row(job.id))
    if why is not None:
        raise ApproveRefused(
            409, "block_off",
            "Holding a submitted invoice from here is turned off now, so nothing was held. Use Block Invoice in "
            "ERPNext if it should still be held.",
        )

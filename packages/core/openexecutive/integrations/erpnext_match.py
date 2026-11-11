"""The bank-matching job's match card: ERPNext's own reconciliation of one
bank line with the submitted Payment or Journal Entries the card shows
(oe_connector's ``draft_match_bank_line``, ladislav-urban/oe_connector#540).

A card whose approval changes financial state: the bank line is reconciled,
and each voucher is allocated in full and cleared, through ERPNext's own
``reconcile_vouchers``, as the approver. Nothing here creates, submits or
cancels a voucher, and a partial match is never proposed.

**Only from the bank-matching job.** The job's code picks the voucher
(``watch.jobs.bank_matching``), ERPNext's ``draft_check`` (operation
``match``) reads it as the job's reviewer, computing the allocation in a
rolled-back savepoint, and the card is left by code
(``erpnext_drafts.leave_job_card``). No chat turn or ERPNext panel can
propose one: ``erpnext_changes.enabled("match")`` lists nothing.

**Off two ways besides the job,** each checked again when the card is
approved (``approval_refusal``), not only when it is left:

1. ``ERPNEXT_BANK_MATCH`` on this server (off by default);
2. the pin: ``draft_match_bank_line`` among the pinned tools and found by
   the gateway with that exact definition.

The job itself must be on (``_job_approval``).

**What the card must read back** (``echo_refusal``, at the check): the line
and the vouchers the job built, ERPNext's summary complete (a key the rules
need missing means "can't tell", so no card), the line in the company's
currency, the vouchers on the line's side of the account, dated on or
before it, each allocated and cleared in full, the line Reconciled after
with its whole unallocated amount taken, and the reviewer neither the
statement's importer nor a voucher's creator (separation of duties from
ERPNext's own data). ``slice_of`` keeps a cleaned, capped copy of what
approval re-checks, stored on the card.

**At approval** (``approval_refusal``, before the claim), from that stored
slice: the switches, the line still Reconciled after and fully taken, the
vouchers the card's own, the approver not its importer or a voucher's
creator by their current email. Then, holding the claim, the day's cap:
what was approved of the job's cards today, by anyone, plus this one stays
within the job's ``amount_cap_day`` in the company's currency
(``take_approved``, one transaction, failing closed). Over it, the claim
is released and the card waits: "today's matching limit is reached".

**What approval sends** (``erpnext_drafts._carry_out``, only for the
bank-matching job's card): ``{name, vouchers, idempotency_key, asked_by:
"job:bank-matching", configured_by, expected_fingerprint}``, a fixed set
with no ``doctype``. oe_connector rebuilds the card under lock and refuses
any difference from its fingerprint, then checks its own separation of
duties and caps.

**Final refusals** (``FINAL``, plus the executor's shared
``CHANGED_SINCE_PROPOSED`` and separation of duties): ERPNext's own
matching limit, a line already reconciled, and a match not from the job's
card. Each closes the card; the next day's run proposes again if the line
is still open.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any

TOOL = "erpnext__draft_match_bank_line"
PIN_NAME = "draft_match_bank_line"
OPERATION = "match"
DOCTYPE = "Bank Transaction"
JOB_ID = "bank-matching"
VOUCHER_TYPES = ("Payment Entry", "Journal Entry")
MAX_VOUCHERS = 5
# A summary naming more creators than this leaves no card.
MAX_CREATORS = 20
# The job's amount_cap_day: its default and the most it may be set to.
AMOUNT_CAP_DEFAULT = 50_000
AMOUNT_CAP_MAX = 1_000_000
_EMAIL_MAX = 320
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_DIRECTIONS = ("in", "out")
_FIELDS = frozenset({"vouchers"})
_VOUCHER_KEYS = frozenset({"doctype", "name"})

# How oe_connector's final refusals of a match read (bank_match.py), each
# with the code and the words the person is told. Approving again would
# refuse again, so each closes the card.
FINAL: tuple[tuple[str, str, str], ...] = (
    (
        "Matching limit", "erpnext_match_limit",
        "ERPNext's limit on bank matches from this app is reached for today, so nothing was matched and the "
        "card is closed. The next run proposes it again if the line is still open.",
    ),
    (
        "is no longer unreconciled", "already_matched",
        "This bank line is already reconciled in ERPNext, so nothing was matched and the card is closed.",
    ),
    (
        "a match comes only from the bank-matching job's card", "not_from_job",
        "ERPNext takes a match only from the bank-matching job's card. Nothing was matched and the card is "
        "closed.",
    ),
)
CHANGED_TEXT = (
    "The bank line or a voucher changed in ERPNext since the card was left, so nothing was matched and the "
    "card is closed. The next run reads it as it is now."
)
LIMIT_TEXT = (
    "Today's matching limit is reached, so nothing was matched. The card waits: approve it again tomorrow, or "
    "match the line in ERPNext's Banking app."
)


def switch_on() -> bool:
    """``ERPNEXT_BANK_MATCH``: this server's switch for match cards."""
    from openexecutive.config import get_settings

    return bool(get_settings().erpnext_bank_match)


async def pinned() -> bool:
    """Whether the gateway serves ``draft_match_bank_line`` with exactly the
    pinned definition: listed in the pin, and found by the gateway's search
    matching it."""
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
    """Why a match card can't be left or approved now, or None: this
    server's switch and the pin, in that order."""
    if not switch_on():
        return "match_switch_off"
    if not await pinned():
        return "match_not_pinned"
    return None


def company_today() -> date:
    from openexecutive.memory.workspace_settings import get_user_timezone

    return datetime.now(get_user_timezone()).date()


# --------------------------------------------------------------------------- #
# The card's fields
# --------------------------------------------------------------------------- #


def _doc_name(value: Any) -> str | None:
    from openexecutive.integrations.erpnext_changes import _DOC_RE

    return value if isinstance(value, str) and _DOC_RE.fullmatch(value) else None


def match_fields(fields: Any, name: Any) -> dict[str, Any] | str:
    """A match card's fields as they are checked, stored and sent:
    ``{vouchers: [{doctype, name}]}`` for the bank line ``name``, or why not.
    One to five vouchers, each a Payment Entry or Journal Entry by its name,
    each named once, in order (doctype, then name), as oe_connector sorts
    them."""
    if _doc_name(name) is None:
        return "A match card names the bank line it matches."
    if not isinstance(fields, dict) or set(fields) != _FIELDS:
        return "A match card's fields are {vouchers}."
    vouchers = fields.get("vouchers")
    if not isinstance(vouchers, list) or not 1 <= len(vouchers) <= MAX_VOUCHERS:
        return f"vouchers is a list of 1 to {MAX_VOUCHERS} {{doctype, name}}."
    out: list[tuple[str, str]] = []
    for voucher in vouchers:
        if not isinstance(voucher, dict) or set(voucher) != _VOUCHER_KEYS:
            return "Each voucher is {doctype, name}."
        doctype, voucher_name = voucher.get("doctype"), _doc_name(voucher.get("name"))
        if doctype not in VOUCHER_TYPES or voucher_name is None:
            return "A voucher is a Payment Entry or a Journal Entry, by its name."
        out.append((str(doctype), voucher_name))
    if len(set(out)) != len(out):
        return "A voucher is named once."
    return {"vouchers": [{"doctype": d, "name": n} for d, n in sorted(out)]}


def _pairs(fields: Any) -> list[tuple[str, str]]:
    vouchers = fields.get("vouchers") if isinstance(fields, dict) else None
    if not isinstance(vouchers, list):
        return []
    return sorted(
        (str(v.get("doctype")), str(v.get("name"))) for v in vouchers if isinstance(v, dict)
    )


# --------------------------------------------------------------------------- #
# ERPNext's check: what it must read back, and the slice the card keeps
# --------------------------------------------------------------------------- #


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _email(value: Any) -> str | None:
    """A person as ERPNext names them, normalized for comparing: an email
    (or "Administrator", which never matches a person's email)."""
    if not isinstance(value, str) or not value.strip() or len(value) > _EMAIL_MAX:
        return None
    text = value.strip().lower()
    return text if text.isprintable() and " " not in text else None


def _day(value: Any) -> str | None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value[:10]):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _party(value: Any) -> tuple[str, str] | None:
    if (
        isinstance(value, list | tuple) and len(value) == 2
        and value[0] in ("Customer", "Supplier") and _doc_name(value[1]) is not None
    ):
        return str(value[0]), str(value[1])
    return None


def slice_of(summary: Any) -> dict[str, Any] | None:
    """The part of a match check's summary the card keeps for approval,
    cleaned and capped, or None when the summary can't be read whole: a key
    the rules rely on missing ("can't tell", never "unset"), a value of the
    wrong kind, more than five vouchers or twenty creators."""
    if not isinstance(summary, dict):
        return None
    line, vouchers, after = summary.get("line"), summary.get("vouchers"), summary.get("after")
    creators = summary.get("creators")
    if (
        not isinstance(line, dict) or not isinstance(vouchers, list) or not isinstance(after, dict)
        or not isinstance(creators, list) or not 1 <= len(vouchers) <= MAX_VOUCHERS
        or len(creators) > MAX_CREATORS or "importer" not in summary
    ):
        return None
    for key in ("name", "date", "direction", "currency", "company_currency", "unallocated_amount", "party_type", "party"):
        if key not in line:
            return None
    name, day = _doc_name(line.get("name")), _day(line.get("date"))
    currency, company_currency = line.get("currency"), line.get("company_currency")
    unallocated = _amount(line.get("unallocated_amount"))
    if (
        name is None or day is None or line.get("direction") not in _DIRECTIONS or unallocated is None
        or not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency)
        or not isinstance(company_currency, str) or not _CURRENCY_RE.fullmatch(company_currency)
    ):
        return None
    party_type, party = line.get("party_type"), line.get("party")
    if party is None and party_type is None:
        line_party: list[str] | None = None
    else:
        found = _party([party_type, party])
        if found is None:
            return None
        line_party = list(found)
    kept_vouchers: list[dict[str, Any]] = []
    for voucher in vouchers:
        if not isinstance(voucher, dict):
            return None
        for key in ("doctype", "name", "direction", "posting_date", "parties", "allocated", "clears_on", "creator"):
            if key not in voucher:
                return None
        doctype, voucher_name = voucher.get("doctype"), _doc_name(voucher.get("name"))
        allocated, posted = _amount(voucher.get("allocated")), _day(voucher.get("posting_date"))
        parties = voucher.get("parties")
        creator = _email(voucher.get("creator"))
        clears = voucher.get("clears_on")
        if (
            doctype not in VOUCHER_TYPES or voucher_name is None or allocated is None or posted is None
            or voucher.get("direction") not in _DIRECTIONS or not isinstance(parties, list) or creator is None
            or (clears is not None and _day(clears) is None)
        ):
            return None
        kept_parties = [_party(p) for p in parties[:MAX_CREATORS]]
        if any(p is None for p in kept_parties) or len(parties) > MAX_CREATORS:
            return None
        kept_vouchers.append({
            "doctype": doctype, "name": voucher_name, "direction": voucher["direction"], "posting_date": posted,
            "parties": [list(p) for p in kept_parties if p is not None], "allocated": allocated,
            "clears_on": _day(clears) if clears is not None else None, "creator": creator,
        })
    status, left = after.get("status"), _amount(after.get("unallocated_amount"))
    total, total_company = _amount(summary.get("total")), _amount(summary.get("total_company_currency"))
    importer = _email(summary.get("importer"))
    kept_creators = [_email(c) for c in creators]
    if (
        not isinstance(status, str) or left is None or total is None or total_company is None or importer is None
        or any(c is None for c in kept_creators)
    ):
        return None
    company, account = _doc_name(summary.get("company")), _doc_name(summary.get("bank_account"))
    if company is None or account is None:
        return None
    return {
        "company": company,
        "bank_account": account,
        "line": {
            "name": name, "date": day, "direction": line["direction"], "currency": currency,
            "company_currency": company_currency, "unallocated_amount": unallocated, "party": line_party,
        },
        "vouchers": kept_vouchers,
        "after": {"status": status[:40], "unallocated_amount": left},
        "total": total,
        "total_company_currency": total_company,
        "importer": importer,
        "creators": sorted({c for c in kept_creators if c is not None}),
    }


def _cents(value: float) -> int:
    return round(value * 100)


def slice_refusal(kept: dict[str, Any], fields: Any) -> str | None:
    """Why a kept slice can't be a card (or approved), as a reason code, or
    None: the vouchers the card's own, each on the line's side of the
    account, dated on or before it, allocated and cleared in full, for the
    line's party when it has one; the line Reconciled after with its whole
    unallocated amount taken; the line in the company's currency."""
    line = kept["line"]
    if sorted((v["doctype"], v["name"]) for v in kept["vouchers"]) != _pairs(fields):
        return "check_mismatch"
    for voucher in kept["vouchers"]:
        if (
            voucher["direction"] != line["direction"] or voucher["posting_date"] > line["date"]
            or voucher["allocated"] <= 0 or not voucher["clears_on"]
            or (line["party"] is not None and line["party"] not in voucher["parties"])
        ):
            return "check_mismatch"
    if line["currency"] != line["company_currency"]:
        return "check_currency"
    total = kept["total"]
    if (
        kept["after"]["status"] != "Reconciled" or _cents(kept["after"]["unallocated_amount"]) != 0
        or _cents(total) != _cents(line["unallocated_amount"]) or _cents(total) <= 0
        or _cents(sum(v["allocated"] for v in kept["vouchers"])) != _cents(total)
        or _cents(kept["total_company_currency"]) != _cents(total)
    ):
        return "check_not_whole"
    return None


def names_person(kept: dict[str, Any], email: str) -> bool:
    """Whether ERPNext's data on the card names this person (by normalized
    email) as the statement's importer or a voucher's creator."""
    me = _email(email)
    if me is None or "@" not in me:
        return False
    return me == kept["importer"] or me in kept["creators"] or any(v["creator"] == me for v in kept["vouchers"])


def echo_refusal(
    name: str, fields: dict[str, Any], checked: dict[str, Any], expect: dict[str, Any], reviewer_email: str,
) -> str | None:
    """Why ERPNext's match check can't become a card, as a reason code, or
    None. ``check_reviewer_sod``: the reviewer imported the statement or made
    a voucher, so someone else must approve it."""
    summary = checked.get("summary")
    if (
        checked.get("operation") != OPERATION or checked.get("doctype") != DOCTYPE or checked.get("name") != name
        or not isinstance(summary, dict) or summary.get("operation") != OPERATION
        or summary.get("doctype") != DOCTYPE or summary.get("name") != name
    ):
        return "check_mismatch"
    kept = slice_of(summary)
    if kept is None or kept["line"]["name"] != name or any(kept.get(k) != v for k, v in expect.items()):
        return "check_mismatch"
    why = slice_refusal(kept, fields)
    if why is not None:
        return why
    if names_person(kept, reviewer_email):
        return "check_reviewer_sod"
    return None


def card_amount(checked: dict[str, Any]) -> float | None:
    """What a checked match card counts against the day's cap, in the
    company's currency."""
    kept = slice_of(checked.get("summary"))
    return kept["total_company_currency"] if kept is not None else None


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #


def card_view(summary: Any) -> dict[str, Any]:
    """What the card shows, from ERPNext's check: the bank line, each
    voucher with what ERPNext would allocate and the date it clears, and the
    line after."""
    from openexecutive.integrations.erpnext_drafts import _one_line, _shown

    got: dict[str, Any] = summary if isinstance(summary, dict) else {}
    line_found = got.get("line")
    line: dict[str, Any] = line_found if isinstance(line_found, dict) else {}
    currency = _shown(line.get("currency"))

    def money(value: Any) -> str:
        number = _amount(value)
        return f"{number:,.2f} {currency}".strip() if number is not None else ""

    party = f"{_shown(line.get('party_type'))} {_shown(line.get('party'))}".strip() if line.get("party") else "none"
    shown: list[dict[str, str]] = [
        {"label": "Bank line", "value": f"{DOCTYPE} {_shown(got.get('name'))}"},
        {"label": "Bank account", "value": _shown(got.get("bank_account"))},
        {"label": "Company", "value": _shown(got.get("company"))},
        {"label": "Date", "value": _shown(line.get("date"))},
        {"label": "Money", "value": "received" if line.get("direction") == "in" else "paid out"},
        {"label": "Amount", "value": money(line.get("amount"))},
        {"label": "Not yet matched", "value": money(line.get("unallocated_amount"))},
        {"label": "Party on the line", "value": party},
        {"label": "Reference (from the bank)", "value": _one_line(line.get("reference"), 140)},
        {"label": "Description (from the bank)", "value": _one_line(line.get("description"), 300)},
    ]
    vouchers = got.get("vouchers")
    lines: list[dict[str, str]] = []
    for voucher in vouchers if isinstance(vouchers, list) else []:
        if not isinstance(voucher, dict):
            continue
        parties = voucher.get("parties")
        named = ", ".join(
            f"{_shown(p[1])}" for p in (parties if isinstance(parties, list) else [])
            if isinstance(p, list) and len(p) == 2
        )
        earlier = voucher.get("existing_bank_allocations")
        lines.append({
            "voucher": f"{_shown(voucher.get('doctype'))} {_shown(voucher.get('name'))}",
            "dated": _shown(voucher.get("posting_date")),
            "party": named or "none",
            "allocated": money(voucher.get("allocated")),
            "clears_on": _shown(voucher.get("clears_on")),
            "matched_elsewhere": str(len(earlier)) if isinstance(earlier, list) else "",
            "created_by": _shown(voucher.get("creator")),
        })
    after = got.get("after") if isinstance(got.get("after"), dict) else {}
    shown.extend([
        {"label": "Matched in all", "value": money(got.get("total"))},
        {"label": "The line after", "value": _shown(after.get("status")) if isinstance(after, dict) else ""},
        {"label": "Statement imported by", "value": _shown(got.get("importer"))},
    ])
    notes = [
        "Approving matches this bank line in ERPNext with the vouchers listed, through ERPNext's own bank "
        "reconciliation, as you: each is allocated in full and cleared on the date shown. Nothing is created, "
        "submitted or cancelled.",
        "The reference and description are the bank's text, shown as data. Check the match against the bank "
        "statement before approving.",
    ]
    return {"fields": shown, "lines": lines, "notes": notes}


def made(name: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """What approval did, from oe_connector's result, or None when it isn't a
    success: ``ok`` with the match of this line, fresh or a repeat of the
    card's key (``already_done``)."""
    if result.get("ok") is not True:
        return None
    if (result.get("operation"), result.get("doctype"), result.get("name")) != (OPERATION, DOCTYPE, name):
        return None
    return {"doctype": DOCTYPE, "name": name, "operation": OPERATION, "already_created": result.get("already_done") is True}


def final_refusal(reason: str) -> tuple[str, str] | None:
    """The code and words of a final refusal of a match, from oe_connector's
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
    """A match card at approval, before anything is claimed: raises
    ``ApproveRefused`` unless it is the bank-matching job's card,
    ``ERPNEXT_BANK_MATCH`` and the pin hold now, and the slice stored with it
    still reads as a whole match its approver may make. A slice naming the
    approver's current email as the statement's importer or a voucher's
    creator closes the card (separation of duties)."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.integrations.erpnext_drafts import JOB_SOURCE, ApproveRefused, _sod_refused
    from openexecutive.watch import registry

    job = registry.get(payload.get("job"))
    if payload.get("source") != JOB_SOURCE or job is None or job.id != JOB_ID or payload.get("doctype") != DOCTYPE:
        raise _malformed()
    if await unavailable() is not None:
        raise ApproveRefused(
            409, "match_off",
            "Matching bank lines from here is turned off now, so nothing was matched. Match the line in "
            "ERPNext's Banking app if it should still be matched.",
        )
    stored = payload.get("slice")
    kept = slice_of_stored(stored)
    if kept is None or kept["line"]["name"] != payload.get("name") or slice_refusal(kept, payload.get("fields")):
        raise _malformed()
    if names_person(kept, normalize_email(person.email or "")):
        raise _sod_refused(instance, payload, person, "erp_data")


def slice_of_stored(stored: Any) -> dict[str, Any] | None:
    """A stored slice read back for approval: the same shape ``slice_of``
    keeps, or None."""
    if not isinstance(stored, dict):
        return None
    line = stored.get("line")
    if not isinstance(line, dict):
        return None
    party = line.get("party")
    summary = {
        **stored,
        "operation": OPERATION,
        "line": {**line, "party_type": party[0] if isinstance(party, list) and len(party) == 2 else None,
                 "party": party[1] if isinstance(party, list) and len(party) == 2 else None},
    }
    again = slice_of(summary)
    return again if again == stored else None


def take_approved(payload: dict[str, Any], decision_id: int, person_id: int) -> bool:
    """Count this card against what was approved of the job's cards today
    (the company's day), by anyone, within the job's ``amount_cap_day``, in
    one transaction. Whether it fits. Fails closed: anything unreadable is
    over the cap."""
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
            job.id, company_today().isoformat(), person_id, decision_id, float(kept["total_company_currency"]),
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

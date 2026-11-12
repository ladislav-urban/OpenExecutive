"""The payment-checks job (phase 1): a likely duplicate supplier invoice held
before it's paid, and a supplier whose bank details changed flagged for a
phone call.

Every hour from 08:00 to 18:00 on weekdays, company time (``SLOTS``), it
reads:

1. **the unpaid Purchase Invoices due** within ``due_within_days`` for the
   company (``list_records``), drafts and submitted ones both;
2. **the same suppliers' submitted Purchase Invoices of the last 120 days**
   (``list_records``, ``supplier in`` the suppliers due, the 50 soonest due),
   to compare against;
3. **whether those suppliers' bank details changed** since their last
   payment (``supplier_bank_changed``, withheld from every other caller).

Reads 2 and 3 are derived from read 1 and skipped when nothing is due. When
ERPNext's ``oe_connector_bank_facts`` is off, read 3 is refused and the run
stops with one notice saying so (ladislav-urban/oe_connector#534).

Its rules are code, unit-tested on fixed data:

- **Duplicates** (``match``), oe_connector's own rules for a block
  (``block_invoice._match``), so the card and ERPNext agree: the same
  supplier and currency, both with a supplier invoice date, and
  - the same supplier invoice number once normalized (case, spaces, dashes,
    leading zeros: "INV-0042" is "inv 42"), a real one (at least 3
    characters and 2 digits) with the same amount or dates within 60 days:
    **high**;
  - the same amount and the same supplier invoice date: **medium**;
  - the same amount with dates within 3 days: **low**, shown as a notice but
    never proposed.
- **Which is the copy:** a draft against a submitted invoice (the draft); of
  two submitted ones, the later posted. Only an unpaid copy not already on
  hold, among the invoices due, is raised.
- **Bank changes** (``bank_changed``): changed after the last payment; or
  never paid, with details created in the last ``NEW_BANK_DAYS`` days.

What it leaves:

- **A duplicate draft:** a ``draft_update`` card for the job's reviewer
  putting it on hold (``on_hold: 1`` and a ``hold_comment`` from a fixed
  template, cleaned and at most 280 characters), only when ERPNext's check,
  as the reviewer, says the draft is theirs or assigned to them. Otherwise a
  notice.
- **A duplicate submitted invoice:** a block card (``erpnext_block``) when
  the job's ``block_submitted`` setting, ``ERPNEXT_BLOCK_INVOICE`` and the pin
  all hold; otherwise a notice saying to use Block Invoice in ERPNext.
- **A bank change:** a notice with code's own line: confirm the change with
  the supplier by phone, on a number already on file, before paying.

Cards go only to the job's reviewer (``watch.cards.route``), at most
``card_cap_day`` a day across the day's runs; a finding with no card is told
as its notice. A finding is raised at most once a day.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

from openexecutive.integrations.erpnext_block import RELEASE_DAYS
from openexecutive.watch.registry import (
    MAX_LIST,
    CardSpec,
    Derived,
    DraftSpec,
    Prior,
    ReadArguments,
    Settings,
    WatchJob,
    WatchRead,
)

# Joins a finding key's parts: a character no document name holds, so two
# different pairs never make one key.
_SEP = ""

JOB_ID = "payment-checks"
DOCTYPE = "Purchase Invoice"
FIELDS = [
    "name", "supplier", "bill_no", "bill_date", "posting_date", "grand_total", "currency",
    "outstanding_amount", "docstatus", "on_hold", "due_date",
]
# Its reads' entries in oe_connector_watch_jobs, as oe_connector lists them
# (tests/unit/watch_job_vectors.json pins them; both list filters are in
# oe_connector's tests/watch_shape_vectors.json).
DUE_SHAPE: Mapping[str, Any] = {
    "doctype": DOCTYPE,
    "fields": FIELDS,
    "filters": {
        "company": "$company",
        "docstatus": ["<", 2],
        "outstanding_amount": [">", 0],
        "due_date": ["<=", "$date"],
    },
    "limit": "$int",
}
HISTORY_SHAPE: Mapping[str, Any] = {
    "doctype": DOCTYPE,
    "fields": FIELDS,
    "filters": {
        "company": "$company",
        "docstatus": 1,
        "supplier": ["in", "$parties"],
        "posting_date": [">=", "$date"],
    },
    "limit": "$int",
}
BANK_SHAPE: Mapping[str, Any] = {"suppliers": "$parties"}
READ_LIMIT = 500
HISTORY_DAYS = 120
# Every hour from 08:00 to 18:00, Monday to Friday, company time.
SLOTS: tuple[time, ...] = tuple(time(hour, 0) for hour in range(8, 19))
WEEKDAYS = frozenset(range(5))
# oe_connector's block_invoice: a bill number counts only when real, and with
# the same amount or supplier invoice dates this close.
MIN_BILL_LENGTH = 3
NUMBER_DATE_WINDOW_DAYS = 60
# "Same amount within 3 days": low, never proposed.
LOW_WINDOW_DAYS = 3
# A never-paid supplier's bank details this new raise the notice.
NEW_BANK_DAYS = 30
HOLD_COMMENT_MAX = 280
HIGH, MEDIUM, LOW = "high", "medium", "low"
_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}
# What a hold comment says why, by confidence (code's words).
WHY = {HIGH: "same bill number", MEDIUM: "same amount and date"}
_COMMENT_RE = re.compile(r"Possible duplicate of (.{1,140}): (same bill number|same amount and date)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# What a bank-change notice says, in code's own words, never the model's.
BANK_LINE = "Confirm the change with the supplier by phone, on a number you already have, before paying."
BLOCK_LINE = (
    "It's submitted, so put it on hold with Block Invoice in ERPNext if it is a duplicate, before anyone pays it."
)
HOLD_LINE = "It's a draft: put it on hold in ERPNext, or delete it, if it is a duplicate."
LOW_LINE = "A weak match (the same amount, close dates): shown for a look, never proposed as a hold."


# --------------------------------------------------------------------------- #
# Reading the results
# --------------------------------------------------------------------------- #


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _day(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _name(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= 140 else ""


@dataclass(frozen=True)
class Invoice:
    name: str
    supplier: str
    bill_no: str
    bill_date: date | None
    posting_date: date | None
    grand_total: float
    currency: str
    outstanding: float
    docstatus: int
    on_hold: bool
    due_date: date | None


def invoices(result: Mapping[str, Any], *, required: bool) -> list[Invoice]:
    """The Purchase Invoices in a ``list_records`` result. A skipped read is
    {} and none."""
    if not result and not required:
        return []
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ValueError("no rows")
    out: list[Invoice] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name, supplier = _name(row.get("name")), _name(row.get("supplier"))
        total, owed = _amount(row.get("grand_total")), _amount(row.get("outstanding_amount"))
        status = row.get("docstatus")
        if not name or not supplier or total is None or owed is None or status not in (0, 1) or isinstance(status, bool):
            continue
        bill = row.get("bill_no")
        currency = row.get("currency")
        out.append(Invoice(
            name=name, supplier=supplier, bill_no=bill if isinstance(bill, str) else "",
            bill_date=_day(row.get("bill_date")), posting_date=_day(row.get("posting_date")),
            grand_total=round(total, 2), currency=currency if isinstance(currency, str) else "",
            outstanding=round(owed, 2), docstatus=int(status), on_hold=row.get("on_hold") in (1, True),
            due_date=_day(row.get("due_date")),
        ))
    return out


def normalized_bill(bill_no: object) -> str:
    """A supplier invoice number as anyone might key it (oe_connector's
    ``normalized_bill``): letters and numbers only, lower case, each number
    without leading zeros ("INV-0042" and "inv 42" are the same)."""
    if not isinstance(bill_no, str):
        return ""
    parts = re.findall(r"[a-z]+|\d+", bill_no.lower())
    return " ".join((p.lstrip("0") or "0") if p.isdigit() else p for p in parts)


def _real_number(normalized: str) -> bool:
    return len(normalized.replace(" ", "")) >= MIN_BILL_LENGTH and sum(c.isdigit() for c in normalized) >= 2


def match(doc: Invoice, other: Invoice) -> str | None:
    """How confident it is that ``doc`` repeats ``other`` (high, medium,
    low), or None. Both need a supplier invoice date and the same currency."""
    if doc.bill_date is None or other.bill_date is None or doc.currency != other.currency:
        return None
    same_amount = doc.grand_total == other.grand_total
    days = abs((doc.bill_date - other.bill_date).days)
    mine = normalized_bill(doc.bill_no)
    same_number = mine and mine == normalized_bill(other.bill_no) and _real_number(mine)
    if same_number and (same_amount or days <= NUMBER_DATE_WINDOW_DAYS):
        return HIGH
    if same_amount and days == 0:
        return MEDIUM
    if same_amount and days <= LOW_WINDOW_DAYS:
        return LOW
    return None


def _later(doc: Invoice, other: Invoice) -> bool:
    """Whether ``doc`` was posted after ``other`` (then by name)."""
    return (doc.posting_date or date.min, doc.name) > (other.posting_date or date.min, other.name)


@dataclass(frozen=True)
class Duplicate:
    copy: Invoice
    original: Invoice
    confidence: str


def duplicates(due: Sequence[Invoice], history: Sequence[Invoice]) -> list[Duplicate]:
    """Each unpaid invoice due, not on hold, that likely repeats a submitted
    one of the same supplier: a draft against any submitted invoice, a
    submitted one against an earlier submitted one. The strongest match
    each, then the earliest original."""
    known: dict[str, Invoice] = {}
    for inv in (*due, *history):
        known.setdefault(inv.name, inv)
    out: list[Duplicate] = []
    for doc in due:
        if doc.outstanding <= 0 or doc.on_hold or doc.grand_total <= 0:
            continue
        best: tuple[int, tuple[date, str], Invoice, str] | None = None
        for other in known.values():
            if (
                other.name == doc.name or other.supplier != doc.supplier or other.docstatus != 1
                or other.grand_total <= 0 or (doc.docstatus == 1 and not _later(doc, other))
            ):
                continue
            confidence = match(doc, other)
            if confidence is None:
                continue
            rank = (_RANK[confidence], (other.posting_date or date.min, other.name))
            if best is None or rank < best[:2]:
                best = (*rank, other, confidence)
        if best is not None:
            out.append(Duplicate(doc, best[2], best[3]))
    return sorted(out, key=lambda d: (_RANK[d.confidence], d.copy.due_date or date.max, d.copy.name))


@dataclass(frozen=True)
class BankChange:
    supplier: str
    changed_on: str
    last_paid_on: str


def bank_changes(result: Mapping[str, Any], today: date) -> list[BankChange]:
    """The suppliers whose bank details changed after their last payment, or
    who were never paid and have details created in the last
    ``NEW_BANK_DAYS`` days. A skipped read is {} and none. A supplier whose
    payments the job's user can't read (``None``) isn't raised."""
    if not result:
        return []
    rows = result.get("suppliers")
    if not isinstance(rows, list):
        raise ValueError("no suppliers")
    out: list[BankChange] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        supplier = _name(row.get("supplier"))
        changed = _day(row.get("bank_details_changed_on"))
        paid = _day(row.get("last_paid_on"))
        after = row.get("bank_details_changed_after_last_payment")
        if not supplier or changed is None or row.get("has_bank_account") is not True or after is not True:
            continue
        if paid is None and (today - changed).days > NEW_BANK_DAYS:
            continue
        out.append(BankChange(supplier, changed.isoformat(), paid.isoformat() if paid else ""))
    return out


def hold_comment(original: str, confidence: str) -> str:
    """The hold comment a duplicate draft's card sets: a fixed template,
    cleaned (no control characters, one line), at most 280 characters."""
    return _clean_comment(f"Possible duplicate of {original}: {WHY[confidence]}")


def _clean_comment(text: str) -> str:
    """One line, made plain (as the card shows it and approval checks it:
    erpnext_changes._as_shown), at most HOLD_COMMENT_MAX."""
    from openexecutive.utils.prompt_blocks import plain

    return " ".join(_CONTROL.sub(" ", plain(text)).split())[:HOLD_COMMENT_MAX].strip()


# --------------------------------------------------------------------------- #
# The reads' arguments
# --------------------------------------------------------------------------- #


def _due(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    until = today + timedelta(days=int(settings["due_within_days"]))
    return {
        "doctype": DOCTYPE,
        "fields": list(FIELDS),
        "filters": {
            "company": settings["company"],
            "docstatus": ["<", 2],
            "outstanding_amount": [">", 0],
            "due_date": ["<=", until.isoformat()],
        },
        "limit": READ_LIMIT,
    }


def suppliers_due(prior: Prior) -> tuple[list[str], bool]:
    """The suppliers of the invoices due (read 1), soonest due first, at most
    ``MAX_LIST``, and whether more were cut."""
    due = invoices(prior[0], required=True) if prior else []
    order: list[str] = []
    for inv in sorted(due, key=lambda i: (i.due_date or date.max, i.name)):
        if inv.supplier not in order:
            order.append(inv.supplier)
    return order[:MAX_LIST], len(order) > MAX_LIST


def _history(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    parties, cut = suppliers_due(prior)
    if not parties:
        return None
    return Derived({
        "doctype": DOCTYPE,
        "fields": list(FIELDS),
        "filters": {
            "company": settings["company"],
            "docstatus": 1,
            "supplier": ["in", parties],
            "posting_date": [">=", (today - timedelta(days=HISTORY_DAYS)).isoformat()],
        },
        "limit": READ_LIMIT,
    }, truncated=cut)


def _bank(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    parties, cut = suppliers_due(prior)
    if not parties:
        return None
    return Derived({"suppliers": parties}, truncated=cut)


# --------------------------------------------------------------------------- #
# The findings
# --------------------------------------------------------------------------- #


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f}{f' {currency}' if currency else ''}"


def _values(dup: Duplicate) -> tuple[tuple[str, str], ...]:
    copy, original = dup.copy, dup.original
    return (
        ("Supplier", copy.supplier),
        ("Invoice", f"{copy.name} ({'draft' if copy.docstatus == 0 else 'submitted'})"),
        ("Supplier invoice no.", copy.bill_no or "none"),
        ("Supplier invoice date", copy.bill_date.isoformat() if copy.bill_date else ""),
        ("Grand total", _money(copy.grand_total, copy.currency)),
        ("Due", copy.due_date.isoformat() if copy.due_date else ""),
        ("Likely repeats", original.name),
        ("Its supplier invoice no.", original.bill_no or "none"),
        ("Its supplier invoice date", original.bill_date.isoformat() if original.bill_date else ""),
        ("Its grand total", _money(original.grand_total, original.currency)),
        ("Confidence", dup.confidence),
    )


def _notice(dup: Duplicate, line: str) -> CardSpec:
    what = "draft invoice" if dup.copy.docstatus == 0 else "submitted invoice"
    return CardSpec(
        key=f"duplicate{_SEP}{dup.copy.name}{_SEP}{dup.original.name}",
        summary=f"Possible duplicate {what}: {dup.copy.name}",
        values=_values(dup),
        notes=(line,),
    )


def build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[CardSpec | DraftSpec]:
    """The run's findings: duplicate holds and blocks (cards, each with its
    notice for when no card can be left), weak matches and bank changes
    (notices). Figures from the reads only."""
    due_result, history_result, bank_result = reads
    due = invoices(due_result, required=True)
    history = invoices(history_result, required=False)
    company = str(settings["company"])
    out: list[CardSpec | DraftSpec] = []
    for dup in duplicates(due, history):
        copy, original = dup.copy, dup.original
        if dup.confidence == LOW:
            out.append(_notice(dup, LOW_LINE))
        elif copy.docstatus == 0:
            out.append(DraftSpec(
                key=f"duplicate{_SEP}{copy.name}{_SEP}{original.name}",
                operation="update",
                doctype=DOCTYPE,
                name=copy.name,
                fields={"set": {"on_hold": 1, "hold_comment": hold_comment(original.name, dup.confidence)}},
                expect={},
                title=f"Hold draft {copy.name}: possible duplicate of {original.name}",
                group=copy.supplier,
                notice=_notice(dup, HOLD_LINE),
            ))
        elif settings["block_submitted"] is True:
            out.append(DraftSpec(
                key=f"duplicate{_SEP}{copy.name}{_SEP}{original.name}",
                operation="block",
                doctype=DOCTYPE,
                name=copy.name,
                fields={"duplicate_of": original.name, "release_date": (today + timedelta(days=RELEASE_DAYS)).isoformat()},
                expect={"supplier": copy.supplier, "company": company},
                title=f"Hold submitted {copy.name}: possible duplicate of {original.name}",
                group=copy.supplier,
                notice=_notice(dup, BLOCK_LINE),
            ))
        else:
            out.append(_notice(dup, BLOCK_LINE))
    owing: dict[str, list[str]] = {}
    for inv in sorted(due, key=lambda i: (i.due_date or date.max, i.name)):
        owing.setdefault(inv.supplier, []).append(inv.name)
    for change in bank_changes(bank_result, today):
        names = owing.get(change.supplier, [])
        out.append(CardSpec(
            key=f"bank{_SEP}{change.supplier}{_SEP}{change.changed_on}",
            summary=f"Bank details changed: {change.supplier}",
            values=(
                ("Supplier", change.supplier),
                ("Bank details changed on", change.changed_on),
                ("Last paid on", change.last_paid_on or "never paid"),
                ("Invoices due", ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")),
            ),
            notes=(BANK_LINE,),
        ))
    if due_result.get("truncated") is True or history_result.get("truncated") is True:
        notes = "Not every invoice could be read this run, so some duplicates may not be shown."
        out = [
            CardSpec(s.key, s.summary, s.values, s.facts, s.items, (*s.notes, notes), s.count)
            if isinstance(s, CardSpec) and n == 0 else s
            for n, s in enumerate(out)
        ]
    return out


def check_fields(operation: str, doctype: str, fields: Mapping[str, Any], name: str = "") -> dict[str, Any] | str:
    """A payment-checks card's fields, as they are checked, stored and sent:
    a draft's hold (``{"set": {"on_hold": 1, "hold_comment"}}``, the comment
    exactly the job's template) or a block (``erpnext_block.block_fields``:
    ``{duplicate_of, release_date}``). Run before each check and again at
    approval."""
    from openexecutive.integrations.erpnext_block import block_fields
    from openexecutive.integrations.erpnext_changes import _DOC_RE

    if doctype != DOCTYPE:
        return "The payment-checks job holds Purchase Invoices only."
    if operation == "block":
        return block_fields(dict(fields) if isinstance(fields, Mapping) else fields, name)
    if operation != "update":
        return "The payment-checks job leaves holds only."
    if not isinstance(name, str) or not _DOC_RE.fullmatch(name):
        return "A hold card names the draft invoice it holds."
    held = fields.get("set") if isinstance(fields, Mapping) and set(fields) == {"set"} else None
    if not isinstance(held, Mapping) or set(held) != {"on_hold", "hold_comment"}:
        return "A hold card sets on_hold and hold_comment only."
    on_hold, comment = held.get("on_hold"), held.get("hold_comment")
    if on_hold != 1 or isinstance(on_hold, bool) or not isinstance(comment, str):
        return "A hold card sets on_hold to 1, with its comment."
    found = _COMMENT_RE.fullmatch(comment)
    if (
        found is None or comment != _clean_comment(comment) or len(comment) > HOLD_COMMENT_MAX
        or found.group(1) == name or not _DOC_RE.fullmatch(found.group(1))
    ):
        return "A hold card's comment is the job's own: Possible duplicate of <invoice>: <why>."
    return {"set": {"on_hold": 1, "hold_comment": comment}}


JOB: WatchJob = WatchJob(
    id=JOB_ID,
    title="Payment checks",
    reads=(
        WatchRead("list_records", DUE_SHAPE, _due, "the supplier invoices due"),
        WatchRead("list_records", HISTORY_SHAPE, _history, "the suppliers' recent invoices"),
        WatchRead("supplier_bank_changed", BANK_SHAPE, _bank, "the suppliers' bank changes"),
    ),
    build=build,
    schedule=SLOTS[0],
    slots=SLOTS,
    weekdays=WEEKDAYS,
    card_cap_day=10,
    default_settings={"company": "", "due_within_days": 14, "block_submitted": False},
    check_fields=check_fields,
    setting_bounds={"due_within_days": (1, 400)},
    card_noun="hold cards",
)

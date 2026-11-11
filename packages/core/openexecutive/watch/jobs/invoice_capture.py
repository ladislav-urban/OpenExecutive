"""The invoice-capture job (phase 2): a supplier's mailed invoice, which
ERPNext's Email Account made an empty draft Purchase Invoice from, filled
from the submitted Purchase Order or Purchase Receipt it bills, on a card
the draft's assignee approves.

Working days at 09:00, 11:00, 13:00 and 15:00, company time, it reads:

1. **the drafts a supplier's mail made** (``mail_invoices_to_fill``, the
   last ``days_back`` days): each with the mail's own From as it arrived,
   the account's DMARC statement, the From domain's published policy, the
   supplier its Contacts name and the mail's one attachment;
2. **each fillable draft's PDF** (``read_attachment``, once per draft, at
   most 20 a run), of which only oe_connector's deterministic fields are
   used: the invoice number and date, its totals, currencies and the
   Purchase Order references it names. An unreadable file is that draft's
   "needs a person", never the run's end;
3. **the suppliers' open Purchase Orders and Purchase Receipts**
   (``list_records``, submitted, not fully billed);
4. **the company's currency** (``list_records`` on Company).

Every read is withheld from chat (``connected_systems``) and opens only
under this job's own read marker; oe_connector refuses the first two to
anyone but this job, running as its own user (``as_job_user``, as
``listing()`` says).

**Setting it up.** When configuring the job, an ERPNext operator limits its
ERP user (``oe-watch-invoice-capture@<site>``) to exactly one Company by
User Permission, applying to all DocTypes; without that, oe_connector
refuses every mailed-draft read. The read is listed in
``oe_connector_watch_jobs`` exactly as ``listing()`` gives it:
``{"tool": "read_attachment", "doctype": "Purchase Invoice", "name":
"$mail_draft", "file": "$mail_file"}``; the two slots are sound only there
(``registry.mail_problem``, ``registry.mail_tool_problem``), never on
another tool, nested, in a filter or in a list.

Its rules are code, unit-tested on fixed data. **The PDF's text never
reaches a model**, and the job has no reason step.

- **The sender gate** (``erpnext_fill.fill_allowed``, the owner's): a
  draft that fails it gets a "can't fill" line saying why.
- **What it bills:** the one Purchase Order the PDF names, when the job
  can open it and it is the supplier's and still open; otherwise, when the
  PDF names none, the one open Purchase Receipt of the supplier whose grand
  total is the PDF's to the cent, in the company's currency. Anything else
  (two orders named, a closed or someone else's order, two or no receipts
  at the total, no total, no text, an ambiguous date) is "a person fills
  these", with the reason.
- **No row is lowered** in v1: ERPNext's check must bill exactly the PDF's
  total, or there is no card.

What it leaves:

- **A fill card** (``integrations.erpnext_fill``, operation ``fill``):
  ERPNext's ``draft_check`` as the job's reviewer, only when the reviewer
  is the person the mail pull assigned the draft to (oe_connector counts
  only that assignment, never a self-assignment; otherwise a line says an
  ERPNext operator points the Purchase Invoice assignment rule at the
  reviewer, or the draft is filled by hand), only while ``ERPNEXT_INVOICE_FILL`` and the pin hold, within
  ``card_cap_day`` cards and ``amount_cap_day`` of their total a day.
- **"Can't be filled from here"** and **"A person fills these"**: one
  notice each a run, listing the drafts and why.
- **Each month**, to the principal: the fills approved the month before,
  and any whose result ERPNext never confirmed, to check.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

from openexecutive.integrations import erpnext_fill
from openexecutive.watch.registry import (
    CardSpec,
    Derived,
    DraftSpec,
    Prior,
    ReadArguments,
    Repeated,
    Settings,
    WatchJob,
    WatchRead,
)

# Joins a finding key's parts: a character no document name holds.
_SEP = "\x1f"

JOB_ID = erpnext_fill.JOB_ID
DOCTYPE = erpnext_fill.DOCTYPE
MAIL_SHAPE: Mapping[str, Any] = {"company": "$company", "from_date": "$date"}
ATTACHMENT_SHAPE: Mapping[str, Any] = {"doctype": "Purchase Invoice", "name": "$mail_draft", "file": "$mail_file"}
PO_FIELDS = ["name", "supplier", "currency", "grand_total", "transaction_date", "per_received", "per_billed", "status"]
PR_FIELDS = ["name", "supplier", "currency", "grand_total", "posting_date", "per_billed", "status"]
PO_SHAPE: Mapping[str, Any] = {
    "doctype": "Purchase Order",
    "fields": PO_FIELDS,
    "filters": {"company": "$company", "docstatus": 1, "per_billed": ["<", 100], "supplier": ["in", "$parties"]},
    "limit": "$int",
}
PR_SHAPE: Mapping[str, Any] = {
    "doctype": "Purchase Receipt",
    "fields": PR_FIELDS,
    "filters": {"company": "$company", "docstatus": 1, "per_billed": ["<", 100], "supplier": ["in", "$parties"]},
    "limit": "$int",
}
COMPANY_SHAPE: Mapping[str, Any] = {
    "doctype": "Company",
    "fields": ["name", "default_currency"],
    "filters": {"name": "$company"},
    "limit": "$int",
}
READ_LIMIT = 500
# The PDFs read a run, at most.
MAX_PDFS = 20
DAYS_BACK_DEFAULT = 14
DAYS_BACK_MAX = 60
SLOTS: tuple[time, ...] = (time(9, 0), time(11, 0), time(13, 0), time(15, 0))
WEEKDAYS = frozenset(range(5))
# Order statuses that bill nothing more.
CLOSED = frozenset({"Closed", "Cancelled", "Completed", "On Hold"})
# The total a PDF bills, by the label oe_connector read it under, first
# match wins.
TOTAL_LABELS: tuple[str, ...] = (
    "grand total", "invoice total", "total due", "amount due", "balance due", "total amount", "total",
)
MONTH_ITEMS_MAX = 100
# What a notice says, in code's own words.
GATE_LINE = (
    "These mailed invoices can't be filled from here: the sender isn't confirmed enough to trust the invoice. "
    "Check each with the supplier before entering it in ERPNext."
)
PERSON_LINE = (
    "A person fills these mailed invoices in ERPNext: the job fills one only from the single order or receipt "
    "the supplier's PDF clearly bills, for its whole total."
)
FILL_LINE = "A fill no card was left for. Fill the draft in ERPNext from the order or receipt shown if it's right."
MONTH_LINE = (
    "Check each of these fills against the supplier's invoice before it is submitted. Each is still a draft."
)
UNKNOWN_OUTCOME = "Not confirmed: check in ERPNext whether it was filled"
NO_COMPANY_LINE = "Name the company in Settings → Watch jobs; an ERPNext operator lists the job for it."
CUT_LINE = "Not every mailed invoice could be read this run, so some aren't shown."
# Why a draft needs a person, by code: code's words.
WHY: dict[str, str] = {
    "no_mail": "The draft wasn't made by a mail the job can read",
    "no_supplier": "The sender's address names no single supplier",
    "no_attachment": "The mail has no PDF the job may read",
    "unread": "The PDF couldn't be read",
    "no_text": "The PDF has no text layer (a scan): a person reads it",
    "no_number": "The PDF shows no invoice number the job can use",
    "no_date": "The PDF shows no invoice date the job can read",
    "ambiguous_date": "The PDF's invoice date could be read two ways",
    "no_total": "The PDF shows no single total",
    "other_currency": "The PDF names another currency than the company's",
    "several_orders": "The PDF names more than one order",
    "order_not_open": "The order the PDF names isn't an open order of this supplier",
    "no_receipt": "No open receipt of this supplier is for the PDF's total",
    "receipts_cut": "Not every open receipt could be read, so which one it bills can't be told",
    "several_currencies": "The PDF names more than one currency",
    "unread_order_ref": "The PDF names an order the job can't read",
    "date_out_of_range": "The PDF's date is more than a year back or more than a month ahead",
    "several_receipts": "More than one open receipt of this supplier is for the PDF's total",
}


# --------------------------------------------------------------------------- #
# Reading the results
# --------------------------------------------------------------------------- #


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _cents(value: float) -> int:
    return round(value * 100)


def _name(value: object) -> str:
    from openexecutive.integrations.erpnext_changes import _DOC_RE

    return value if isinstance(value, str) and _DOC_RE.fullmatch(value) else ""


def _settings_company(settings: Settings) -> str:
    return str(settings["company"])


@dataclass(frozen=True)
class Draft:
    """One mailed draft as ``mail_invoices_to_fill`` returned it."""

    name: str
    facts: Mapping[str, Any]
    supplier: str
    supplier_name: str
    attachment: str
    empty: bool
    reason: str


def drafts(result: Mapping[str, Any], company: str) -> tuple[list[Draft], bool]:
    """The drafts read 1 returned, the company the job's, and whether the
    list was cut."""
    if not result:
        return [], False
    if result.get("company") != company or not isinstance(result.get("drafts"), list):
        raise ValueError("another company's drafts")
    out: list[Draft] = []
    for row in result["drafts"]:
        if not isinstance(row, Mapping) or row.get("company") != company:
            continue
        name = _name(row.get("name"))
        if not name:
            continue
        supplier = row.get("supplier") if isinstance(row.get("supplier"), Mapping) else None
        attachment = row.get("attachment") if isinstance(row.get("attachment"), Mapping) else None
        reason = ""
        if not row.get("mail"):
            reason = "no_mail"
        elif supplier is None or not _name(supplier.get("name")):
            reason = "no_supplier"
        elif attachment is None or not _name(attachment.get("file")):
            reason = "no_attachment"
        out.append(Draft(
            name=name, facts=row,
            supplier=_name(supplier.get("name")) if supplier is not None else "",
            supplier_name=str(supplier.get("supplier_name") or "")[:140] if supplier is not None else "",
            attachment=_name(attachment.get("file")) if attachment is not None else "",
            empty=row.get("empty") is True, reason=reason,
        ))
    return out, result.get("truncated") is True


def fillable(found: Sequence[Draft]) -> list[Draft]:
    """The drafts whose PDF is worth reading: still empty, the gate passed,
    a supplier and a file. The others need no read."""
    return [d for d in found if d.empty and not d.reason and erpnext_fill.fill_allowed(d.facts)]


@dataclass(frozen=True)
class Pdf:
    """What oe_connector read deterministically from one draft's PDF."""

    number: str
    day: str
    day_reason: str
    total: float | None
    currencies: tuple[str, ...]
    orders: tuple[tuple[str, bool], ...]
    reason: str


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def normalize_date(text: object) -> tuple[str, str]:
    """An invoice date as the PDF wrote it, as YYYY-MM-DD, or "" with the
    reason ("no_date", "ambiguous_date"). ISO; D.M.Y, D/M/Y or M/D/Y only
    when one part is above 12; "12 Nov 2026" and "Nov 12, 2026"."""
    if not isinstance(text, str):
        return "", "no_date"
    value = " ".join(text.split()).strip(" .,")[:40]
    found: tuple[int, int, int] | None = None
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value):
        found = (int(m[1]), int(m[2]), int(m[3]))
    elif m := re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", value):
        first, second, year = int(m[1]), int(m[2]), int(m[3])
        if first > 12 >= second:
            found = (year, second, first)
        elif second > 12 >= first or first == second:
            found = (year, first, second)
        else:
            return "", "ambiguous_date"
    elif m := re.fullmatch(r"(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})", value):
        month = _MONTHS.get(m[2].lower())
        found = (int(m[3]), month, int(m[1])) if month else None
    elif m := re.fullmatch(r"([A-Za-z]{3})[a-z]* (\d{1,2}),? (\d{4})", value):
        month = _MONTHS.get(m[1].lower())
        found = (int(m[3]), month, int(m[2])) if month else None
    if found is None:
        return "", "no_date"
    try:
        return date(*found).isoformat(), ""
    except ValueError:
        return "", "no_date"


# How far an invoice date may be from the run's day: a year back, a month on.
DATE_BACK_DAYS = 366
DATE_AHEAD_DAYS = 31


def _date_in_range(day: str, today: date) -> bool:
    try:
        found = date.fromisoformat(day)
    except ValueError:
        return False
    return today - timedelta(days=DATE_BACK_DAYS) <= found <= today + timedelta(days=DATE_AHEAD_DAYS)


def parse_amount(text: object) -> float | None:
    """A total as the PDF wrote it ("1,234.56", "1.234,56", "1234.5",
    "$ 1,200"), or None when it can't be told."""
    if isinstance(text, int | float) and not isinstance(text, bool):
        return _amount(text)
    if not isinstance(text, str):
        return None
    value = re.sub(r"[^\d.,\-]", "", text)
    if not value or value.count("-") > 1 or ("-" in value and not value.startswith("-")):
        return None
    if "," in value and "." in value:
        decimal = "," if value.rfind(",") > value.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        value = value.replace(thousands, "").replace(decimal, ".")
    elif "," in value:
        head, _, tail = value.rpartition(",")
        if len(tail) == 2 and value.count(",") == 1:
            value = f"{head}.{tail}"
        elif all(len(part) == 3 for part in value.split(",")[1:]):
            value = value.replace(",", "")
        else:
            return None
    elif value.count(".") > 1:
        if not all(len(part) == 3 for part in value.split(".")[1:]):
            return None
        value = value.replace(".", "")
    try:
        number = float(value)
    except ValueError:
        return None
    return round(number, 2) if math.isfinite(number) and number > 0 else None


def pdf_total(totals: object) -> tuple[float | None, str]:
    """The PDF's total: the first label in ``TOTAL_LABELS`` it shows, when
    every value under that label agrees; or None with "no_total"."""
    if not isinstance(totals, list):
        return None, "no_total"
    for label in TOTAL_LABELS:
        values = {
            parse_amount(t.get("value")) for t in totals
            if isinstance(t, Mapping) and isinstance(t.get("label"), str) and t["label"].strip().lower() == label
        }
        if not values:
            continue
        if len(values) != 1 or None in values:
            return None, "no_total"
        (value,) = values
        return value, ""
    return None, "no_total"


def read_pdf(result: object) -> Pdf:
    """One draft's PDF, from ``read_attachment``'s result: its fields only."""
    if not isinstance(result, Mapping) or result.get("unread") is True:
        return Pdf("", "", "", None, (), (), "unread")
    if result.get("no_text") is True:
        return Pdf("", "", "", None, (), (), "no_text")
    fields = result.get("fields")
    if not isinstance(fields, Mapping):
        return Pdf("", "", "", None, (), (), "unread")
    number_field = fields.get("invoice_number")
    number = number_field.get("value") if isinstance(number_field, Mapping) else None
    number = " ".join(number.split()) if isinstance(number, str) else ""
    if not erpnext_fill.BILL_NO_RE.fullmatch(number):
        number = ""
    day_field = fields.get("invoice_date")
    day, day_reason = normalize_date(day_field.get("value") if isinstance(day_field, Mapping) else None)
    total, _why = pdf_total(fields.get("totals"))
    currencies = tuple(
        c for c in (fields.get("currencies") or []) if isinstance(c, str) and re.fullmatch(r"[A-Z]{3}", c)
    )[:10]
    orders: list[tuple[str, bool]] = []
    refs = fields.get("purchase_order_references")
    # A reference the job can't read counts against the receipt path: the
    # PDF named an order, so no receipt is ever chosen for it.
    unread_ref = refs is not None and not isinstance(refs, list)
    for ref in refs if isinstance(refs, list) else []:
        if isinstance(ref, Mapping) and _name(ref.get("value")):
            entry = (str(ref["value"]), ref.get("purchase_order_you_can_open") is True)
            if entry[0] not in {o[0] for o in orders}:
                orders.append(entry)
        else:
            unread_ref = True
    reason = ""
    if unread_ref:
        reason = "unread_order_ref"
    elif not number:
        reason = "no_number"
    elif not day:
        reason = day_reason or "no_date"
    elif total is None:
        reason = "no_total"
    return Pdf(number, day, day_reason, total, currencies, tuple(orders[:20]), reason)


@dataclass(frozen=True)
class Open:
    """An open Purchase Order or Purchase Receipt of a supplier."""

    doctype: str
    name: str
    supplier: str
    currency: str
    total: float


def documents_cut(result: Mapping[str, Any]) -> bool:
    """Whether a documents read was cut (its rows aren't all there)."""
    return isinstance(result, Mapping) and result.get("truncated") is True


def open_documents(result: Mapping[str, Any], doctype: str) -> list[Open]:
    if not result:
        return []
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ValueError("no rows")
    out: list[Open] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name, supplier, total = _name(row.get("name")), _name(row.get("supplier")), _amount(row.get("grand_total"))
        currency = row.get("currency")
        if not name or not supplier or total is None or not isinstance(currency, str):
            continue
        if row.get("status") in CLOSED:
            continue
        out.append(Open(doctype, name, supplier, currency, round(total, 2)))
    return out


def company_currency(result: Mapping[str, Any], company: str) -> str:
    rows = result.get("rows") if isinstance(result, Mapping) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and row.get("name") == company and isinstance(row.get("default_currency"), str):
            return str(row["default_currency"])
    return ""


def choose_source(
    draft: Draft, pdf: Pdf, orders: Sequence[Open], receipts: Sequence[Open], currency: str,
    receipts_cut: bool = False,
) -> tuple[Open | None, str]:
    """What the draft bills, by the plan's rules, or None with why."""
    # The same currency: one named, the company's (none named: the total's
    # own check bounds it). Several named: a person tells which it is in.
    if len(set(pdf.currencies)) > 1:
        return None, "several_currencies"
    if pdf.currencies and pdf.currencies[0] != currency:
        return None, "other_currency"
    if len(pdf.orders) > 1:
        return None, "several_orders"
    if len(pdf.orders) == 1:
        ref, can_open = pdf.orders[0]
        order = next((o for o in orders if o.name == ref), None)
        if not can_open or order is None or order.supplier != draft.supplier:
            return None, "order_not_open"
        return order, ""
    assert pdf.total is not None
    fit = [
        r for r in receipts
        if r.supplier == draft.supplier and r.currency == currency and _cents(r.total) == _cents(pdf.total)
    ]
    if receipts_cut:
        # "Exactly one" can't be told from a cut list.
        return None, "receipts_cut"
    if not fit:
        return None, "no_receipt"
    if len(fit) > 1:
        return None, "several_receipts"
    return fit[0], ""


# --------------------------------------------------------------------------- #
# The reads' arguments
# --------------------------------------------------------------------------- #


def _mail_args(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    if not settings["company"]:
        return None
    days = settings["days_back"] if isinstance(settings["days_back"], int) else DAYS_BACK_DEFAULT
    return {"company": settings["company"], "from_date": (today - timedelta(days=days)).isoformat()}


def _pdf_args(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    found, _cut = drafts(prior[0], _settings_company(settings)) if prior else ([], False)
    due = fillable(found)
    if not due:
        return None
    return Repeated(
        tuple({"doctype": DOCTYPE, "name": d.name, "file": d.attachment} for d in due[:MAX_PDFS]),
        truncated=len(due) > MAX_PDFS,
    )


def _suppliers(settings: Settings, prior: Prior) -> tuple[list[str], bool]:
    found, _cut = drafts(prior[0], _settings_company(settings)) if prior else ([], False)
    names = sorted({d.supplier for d in fillable(found)[:MAX_PDFS] if d.supplier})
    return names[:50], len(names) > 50


def _documents_args(doctype: str, fields: list[str]) -> Any:
    def build(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
        parties, cut = _suppliers(settings, prior)
        if not parties:
            return None
        return Derived({
            "doctype": doctype,
            "fields": list(fields),
            "filters": {"company": settings["company"], "docstatus": 1, "per_billed": ["<", 100],
                        "supplier": ["in", parties]},
            "limit": READ_LIMIT,
        }, truncated=cut)

    return build


def _company_args(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    if not settings["company"]:
        return None
    return {"doctype": "Company", "fields": ["name", "default_currency"], "filters": {"name": settings["company"]},
            "limit": 1}


# --------------------------------------------------------------------------- #
# The findings
# --------------------------------------------------------------------------- #


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f}{f' {currency}' if currency else ''}"


def _draft_values(draft: Draft) -> tuple[tuple[str, str], ...]:
    """A draft as a notice shows it."""
    facts = draft.facts
    return (
        ("Draft invoice", draft.name),
        ("Supplier", draft.supplier_name or draft.supplier or "not named"),
        # The mail's own From only when its headers confirmed it; otherwise
        # the sender ERPNext recorded, said to be unconfirmed.
        ("Sent from", str(facts["sender_from"])[:140])
        if isinstance(facts.get("sender_from"), str) and facts["sender_from"]
        else ("Sender (not confirmed)", str(facts.get("sender") or "")[:140]),
    )


def build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[CardSpec | DraftSpec]:
    """The run's findings: a fill card for each draft one source fits (with
    its notice for when no card can be left), one notice of the drafts the
    gate keeps from being filled, and one of the drafts a person fills.
    Figures from the reads only."""
    mail_result, pdf_result, po_result, pr_result, company_result = reads
    company = _settings_company(settings)
    if not company:
        return [CardSpec(key=f"no-company{_SEP}{today.isoformat()}", summary="No company to fill invoices for",
                         notes=(NO_COMPANY_LINE,))]
    found, cut = drafts(mail_result, company)
    currency = company_currency(company_result, company)
    if not currency:
        raise ValueError("no company currency")
    pdfs: dict[str, Pdf] = {}
    each = pdf_result.get("each") if isinstance(pdf_result, Mapping) else None
    due = fillable(found)
    if isinstance(each, list):
        for draft, result in zip(due[:MAX_PDFS], each, strict=False):
            # One result per draft asked, in order; one naming another draft
            # counts as unread.
            ok = isinstance(result, Mapping) and (result.get("unread") is True or result.get("name") == draft.name)
            pdfs[draft.name] = read_pdf(result if ok else None)
    cut = cut or (isinstance(pdf_result, Mapping) and pdf_result.get("truncated") is True)
    orders = open_documents(po_result, "Purchase Order")
    receipts = open_documents(pr_result, "Purchase Receipt")
    out: list[CardSpec | DraftSpec] = []
    gated: list[CardSpec] = []
    person: list[CardSpec] = []
    for draft in found:
        if not draft.empty:
            continue
        # The sender's gate first: a mail it keeps is never told as one a
        # person fills, whatever else is wrong with it.
        if not erpnext_fill.fill_allowed(draft.facts):
            gated.append(CardSpec(
                key=f"gate{_SEP}{draft.name}",
                summary=f"{draft.name}: {erpnext_fill.gate_reason(draft.facts)[:300]}",
                values=_draft_values(draft),
            ))
            continue
        if draft.reason:
            person.append(CardSpec(key=f"person{_SEP}{draft.name}", summary=f"{draft.name}: {WHY[draft.reason]}",
                                   values=_draft_values(draft)))
            continue
        pdf = pdfs.get(draft.name)
        if pdf is None:
            # Not read this run (past the run's PDFs): said by the cut line.
            continue
        why = pdf.reason
        source: Open | None = None
        if not why and not _date_in_range(pdf.day, today):
            why = "date_out_of_range"
        if not why:
            source, why = choose_source(draft, pdf, orders, receipts, currency, receipts_cut=documents_cut(pr_result))
        if source is None or why:
            person.append(CardSpec(key=f"person{_SEP}{draft.name}", summary=f"{draft.name}: {WHY[why]}",
                                   values=_draft_values(draft)))
            continue
        assert pdf.total is not None
        key = f"fill{_SEP}{draft.name}{_SEP}{source.doctype}{_SEP}{source.name}{_SEP}{pdf.number}"
        out.append(DraftSpec(
            key=key,
            operation=erpnext_fill.OPERATION,
            doctype=DOCTYPE,
            name=draft.name,
            fields={"source": {"doctype": source.doctype, "name": source.name}, "lower": [],
                    "bill_no": pdf.number, "bill_date": pdf.day},
            expect={"company": company, "supplier": draft.supplier,
                    "sender_from": str(draft.facts.get("sender_from") or "").lower(),
                    "company_currency": currency, "pdf_total": pdf.total},
            title=f"Fill {draft.name} from {source.doctype} {source.name}",
            group=draft.supplier,
            notice=CardSpec(
                key=key,
                summary=f"Likely fill for {draft.name}: {source.doctype} {source.name}",
                values=(*_draft_values(draft), ("Bills", f"{source.doctype} {source.name}"),
                        ("Invoice no. on the PDF", pdf.number), ("Its date", pdf.day),
                        ("Its total", _money(pdf.total, currency))),
                notes=(FILL_LINE,),
            ),
            shown=(
                ("On the supplier's PDF: invoice no.", pdf.number),
                ("On the supplier's PDF: date", pdf.day),
                ("On the supplier's PDF: total", _money(pdf.total, currency)),
            ),
        ))
    notes_cut = (CUT_LINE,) if cut else ()
    if gated:
        out.append(CardSpec(key=f"gate{_SEP}{today.isoformat()}", summary="Mailed invoices that can't be filled from here",
                            items=tuple(gated), notes=(GATE_LINE, *notes_cut)))
    if person:
        out.append(CardSpec(key=f"person{_SEP}{today.isoformat()}", summary="Mailed invoices a person fills",
                            items=tuple(person), notes=(PERSON_LINE, *notes_cut)))
    return out


def check_fields(operation: str, doctype: str, fields: Mapping[str, Any], name: str = "") -> dict[str, Any] | str:
    """An invoice-capture card's fields, as they are checked, stored and
    sent: a fill only (``erpnext_fill.fill_fields``). Run before each check
    and again at approval."""
    if operation != erpnext_fill.OPERATION or doctype != DOCTYPE:
        return "The invoice-capture job leaves invoice fills only."
    return erpnext_fill.fill_fields(dict(fields) if isinstance(fields, Mapping) else fields, name)


# --------------------------------------------------------------------------- #
# The month's fills (this install's own records)
# --------------------------------------------------------------------------- #


def _previous_month(today: date) -> str:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def month_notices(settings: Settings, today: date) -> list[CardSpec]:
    """Once a month, to the principal: the fills approved the month before,
    and any tried but never confirmed, from the job's cards."""
    from openexecutive.integrations import erpnext_drafts as drafts_mod
    from openexecutive.memory.decision_ledger import STATUS_APPROVED_UNCHANGED
    from openexecutive.people.store import get_person
    from openexecutive.watch.runner import dedup_key, raised_ever

    month = _previous_month(today)
    key = f"month{_SEP}{month}"
    if raised_ever(dedup_key(JOB_ID, key)):
        return []
    items: list[CardSpec] = []
    for card in sorted(drafts_mod.job_cards(JOB_ID, open_only=False), key=lambda c: (c.resolved_at or "", c.id)):
        payload = drafts_mod._payload(card)
        if payload.get("operation") != erpnext_fill.OPERATION:
            continue
        made = card.status == STATUS_APPROVED_UNCHANGED
        tried = not made and int(payload.get("attempts") or 0) > 0
        when = card.resolved_at or ""
        if not (made or tried) or (made and not when.startswith(month)) or (tried and when and not when.startswith(month)):
            continue
        kept = erpnext_fill.slice_of_stored(payload.get("slice"))
        if kept is None:
            continue
        approver = get_person(card.resolver_person_id) if isinstance(card.resolver_person_id, int) else None
        items.append(CardSpec(
            key=f"month{_SEP}{card.id}",
            summary=f"Invoice {payload.get('name')}",
            values=(
                ("Supplier", kept["supplier"]),
                ("Invoice no.", kept["bill_no"]),
                ("Total", _money(kept["grand_total"], kept["currency"])),
                ("Bills", f"{kept['source']['doctype']} {kept['source']['name']}"),
                ("Approved by", approver.full_name if approver is not None else "unknown"),
                ("Approved on", when[:10]) if made else ("Outcome", UNKNOWN_OUTCOME),
            ),
        ))
    if not items:
        return []
    more = len(items) - MONTH_ITEMS_MAX
    notes = (MONTH_LINE,) + ((f"And {more} more: see the job's cards.",) if more > 0 else ())
    return [CardSpec(
        key=key, summary=f"Invoices filled in {month}", items=tuple(items[:MONTH_ITEMS_MAX]), notes=notes,
        count=len(items),
        # The check on the reviewer's approvals goes to the principal.
        to_principal=True,
    )]


def _month_notices(settings: Settings, today: date) -> Sequence[CardSpec]:
    return month_notices(settings, today)


JOB: WatchJob = WatchJob(
    id=JOB_ID,
    title="Invoice capture",
    reads=(
        WatchRead("mail_invoices_to_fill", MAIL_SHAPE, _mail_args, "the mailed invoices to fill"),
        WatchRead("read_attachment", ATTACHMENT_SHAPE, _pdf_args, "the mailed invoices' PDFs", repeat=MAX_PDFS,
                  may_fail=True),
        WatchRead("list_records", PO_SHAPE, _documents_args("Purchase Order", PO_FIELDS), "the suppliers' open orders"),
        WatchRead("list_records", PR_SHAPE, _documents_args("Purchase Receipt", PR_FIELDS),
                  "the suppliers' open receipts"),
        WatchRead("list_records", COMPANY_SHAPE, _company_args, "the company's currency"),
    ),
    build=build,
    schedule=SLOTS[0],
    slots=SLOTS,
    weekdays=WEEKDAYS,
    card_cap_day=30,
    default_settings={"company": "", "days_back": DAYS_BACK_DEFAULT, "amount_cap_day": erpnext_fill.AMOUNT_CAP_DEFAULT},
    check_fields=check_fields,
    setting_bounds={"days_back": (1, DAYS_BACK_MAX), "amount_cap_day": (1, erpnext_fill.AMOUNT_CAP_MAX)},
    card_noun="fill cards",
    extra_notices=_month_notices,
)


def listing() -> dict[str, Any]:
    """The job's entry in ``oe_connector_watch_jobs`` as an operator writes
    it: run as its own user (oe_connector refuses the capture tools
    otherwise)."""
    reads: list[dict[str, Any]] = [
        {"tool": "mail_invoices_to_fill", **MAIL_SHAPE},
        {"tool": "read_attachment", **ATTACHMENT_SHAPE},
        {"tool": "list_records", **json.loads(json.dumps(PO_SHAPE))},
        {"tool": "list_records", **json.loads(json.dumps(PR_SHAPE))},
        {"tool": "list_records", **json.loads(json.dumps(COMPANY_SHAPE))},
    ]
    return {"as_job_user": True, "reads": reads}

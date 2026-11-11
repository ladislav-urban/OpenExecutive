"""The bank-matching job (phase 2): a bank line ERPNext's own rules left
unreconciled, matched with the one submitted Payment or Journal Entry it
most likely is, on a card the job's reviewer approves.

Daily at 10:00, company time, after the morning's statement import and
ERPNext's own hourly matching rules, it reads:

1. **each listed account's unreconciled lines** (``bank_lines_to_match``,
   one read per name in the job's ``bank_accounts``, at most 10; each
   account is also listed by an ERPNext operator in
   ``oe_connector_bank_matching``). oe_connector redacts account-like
   numbers in the bank's text and leaves out Employee and payroll lines;
2. **ERPNext's candidate vouchers for each line** (``bank_line_candidates``,
   one read per line still to match, oldest first, at most 50 a run);
3. **the unpaid Sales and Purchase Invoices of the lines' parties**
   (``list_records``, ``customer`` / ``supplier`` in the parties of the
   lines no voucher fits), to tell a payment for several invoices or a
   partial one. Skipped when there are none.

Every read is withheld from chat (``connected_systems``) and opens only
under this job's own read marker.

Its rules are code, unit-tested on fixed data:

- **Which lines:** still unallocated, at least a day old, and not one
  ERPNext's own rules take (a candidate with exactly the line's reference
  is left to them).
- **Confidence**, among candidates in the line's currency, dated on or
  before it, and never for another party than the line's:
  - **high:** the same party and the same amount;
  - **medium:** the same amount, the line with no party (explicitly none:
    a missing key means "can't tell", never "none"), and the voucher's
    reference (6 characters or more, on word boundaries) in the bank's
    reference or description;
  - **low:** the same amount or the same party only. Shown on a card's
    "also possible" lines, never proposed, never named in a notice.
- **One voucher a card,** and only when exactly one fits at the best
  confidence. Never a match for a payment covering several invoices, a
  partial one, or two vouchers that fit equally: those get a notice to
  match the line in ERPNext's Banking app.

What it leaves:

- **A match card** (``integrations.erpnext_match``, operation ``match``):
  ERPNext's ``draft_check`` as the reviewer, with what it would allocate
  and clear, only while ``ERPNEXT_BANK_MATCH`` and the pin hold, within
  ``card_cap_day`` cards and ``amount_cap_day`` of their total a day in the
  company's currency. Otherwise its notice.
- **"Nothing to match"**: one notice a run listing the lines nothing fits.
- **"A person matches these"**: one notice a run listing the lines that
  need the Banking app (several invoices, a partial payment, a payment
  with no voucher yet, two vouchers that fit equally).
- **Each month**, a notice listing the matches approved the month before,
  to check against the bank statement (``extra_notices``, from this
  install's own records).
"""
from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

from openexecutive.integrations import erpnext_match
from openexecutive.watch.registry import (
    MAX_LIST,
    MAX_REPEAT,
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

JOB_ID = erpnext_match.JOB_ID
DOCTYPE = erpnext_match.DOCTYPE
# Its reads' entries in oe_connector_watch_jobs. Read 1 is listed there once
# per account, with the account's name in place of $bank_account.
LINES_SHAPE: Mapping[str, Any] = {"bank_account": "$bank_account", "from_date": "$date"}
CANDIDATES_SHAPE: Mapping[str, Any] = {"bank_transaction": "$bank_line"}
SI_FIELDS = ["name", "customer", "outstanding_amount", "currency", "due_date"]
PI_FIELDS = ["name", "supplier", "outstanding_amount", "currency", "due_date"]
SI_SHAPE: Mapping[str, Any] = {
    "doctype": "Sales Invoice",
    "fields": SI_FIELDS,
    "filters": {"company": "$company", "docstatus": 1, "outstanding_amount": [">", 0], "customer": ["in", "$customers"]},
    "limit": "$int",
}
PI_SHAPE: Mapping[str, Any] = {
    "doctype": "Purchase Invoice",
    "fields": PI_FIELDS,
    "filters": {"company": "$company", "docstatus": 1, "outstanding_amount": [">", 0], "supplier": ["in", "$parties"]},
    "limit": "$int",
}
READ_LIMIT = 500
# How far back read 1 asks for lines.
LOOKBACK_DAYS = 60
# At most this many accounts, and lines whose candidates are read, a run.
MAX_ACCOUNTS = 10
MAX_LINES = MAX_REPEAT
# A line this young waits for the next run (the statement may be partial).
MIN_AGE_DAYS = 1
# A reference counts only this long, on word boundaries.
MIN_REFERENCE = 6
# Invoices tried together for "one payment for several invoices".
MAX_COMBINED = 4
MAX_INVOICES_TRIED = 15
SCHEDULE = time(10, 0)
HIGH, MEDIUM, LOW = "high", "medium", "low"
# What a notice says, in code's own words.
NOTHING_LINE = "Nothing in ERPNext fits these bank lines yet. Match them in ERPNext's Banking app when the voucher is there."
PERSON_LINE = (
    "These bank lines need a person in ERPNext's Banking app (Bank Reconciliation Tool): the job never makes a "
    "voucher, and never matches a partial payment or one payment for several invoices."
)
MATCH_LINE = "A likely match no card was left for. Match it in ERPNext's Banking app if it's right."
MONTH_LINE = (
    "Check each of these matches against the bank statement for the month. To undo one, unreconcile the bank "
    "line in ERPNext."
)
# For a match approved whose result ERPNext never confirmed (the call failed
# or timed out, and the card wasn't approved again): it may have been made.
UNKNOWN_OUTCOME = "Not confirmed: check in ERPNext whether it was made"
NO_ACCOUNTS_LINE = "Name the bank accounts to match in Settings → Watch jobs; an ERPNext operator lists the same ones."
CUT_LINE = "Not every bank line could be read this run, so some aren't shown."
# Why a line needs a person, by code: code's words.
WHY: dict[str, str] = {
    "several": "One payment that may cover several unpaid invoices",
    "partial": "A partial payment",
    "no_voucher": "A payment with no voucher in ERPNext yet",
    "ambiguous": "More than one voucher fits equally",
}
MONTH_ITEMS_MAX = 100


# --------------------------------------------------------------------------- #
# Reading the results
# --------------------------------------------------------------------------- #


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _cents(value: float) -> int:
    return round(value * 100)


def _day(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _name(value: object) -> str:
    from openexecutive.integrations.erpnext_changes import _DOC_RE

    return value if isinstance(value, str) and _DOC_RE.fullmatch(value) else ""


def _text(value: object, limit: int = 300) -> str:
    return value[:limit] if isinstance(value, str) else ""


_UNKNOWN = ("?", "?")


def _party(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """A row's party: (type, name), None when it explicitly has none, or
    ``_UNKNOWN`` when the keys are missing or malformed ("can't tell")."""
    if "party_type" not in row or "party" not in row:
        return _UNKNOWN
    kind, name = row.get("party_type"), row.get("party")
    if kind is None and name is None:
        return None
    if kind in ("Customer", "Supplier") and _name(name):
        return str(kind), str(name)
    return _UNKNOWN


@dataclass(frozen=True)
class Line:
    name: str
    account: str
    date: date
    direction: str
    amount: float
    currency: str
    unallocated: float
    reference: str
    description: str
    party: tuple[str, str] | None


def lines(result: Mapping[str, Any], company: str) -> tuple[list[Line], bool]:
    """The bank lines read 1 returned (``{"each": [...]}``, one result per
    account), each account's company the job's, and whether any list was
    cut. A skipped read is {} and none."""
    if not result:
        return [], False
    each = result.get("each")
    if not isinstance(each, list):
        raise ValueError("no accounts")
    out: list[Line] = []
    cut = result.get("truncated") is True
    for account_result in each:
        if not isinstance(account_result, Mapping) or account_result.get("company") != company:
            raise ValueError("an account of another company")
        account, rows = _name(account_result.get("bank_account")), account_result.get("lines")
        if not account or not isinstance(rows, list):
            raise ValueError("no lines")
        cut = cut or account_result.get("truncated") is True
        for row in rows:
            line = _line(row, account)
            if line is not None:
                out.append(line)
    return out, cut


def _line(row: object, account: str) -> Line | None:
    if not isinstance(row, Mapping):
        return None
    name, day = _name(row.get("name")), _day(row.get("date"))
    amount, unallocated = _amount(row.get("amount")), _amount(row.get("unallocated_amount"))
    currency, direction = row.get("currency"), row.get("direction")
    if (
        not name or day is None or amount is None or unallocated is None or direction not in ("in", "out")
        or not isinstance(currency, str)
    ):
        return None
    return Line(
        name=name, account=account, date=day, direction=str(direction), amount=round(amount, 2),
        currency=currency, unallocated=round(unallocated, 2), reference=_text(row.get("reference"), 140),
        description=_text(row.get("description")), party=_party(row),
    )


def to_match(found: Sequence[Line], today: date) -> list[Line]:
    """The lines whose candidates are read: still unallocated and at least a
    day old, oldest first (then by name)."""
    return sorted(
        (ln for ln in found if ln.unallocated > 0 and (today - ln.date).days >= MIN_AGE_DAYS),
        key=lambda ln: (ln.date, ln.name),
    )


@dataclass(frozen=True)
class Candidate:
    doctype: str
    name: str
    amount: float
    currency: str
    party: tuple[str, str] | None
    posting_date: date | None
    reference: str


def candidates(result: Mapping[str, Any], company: str) -> dict[str, list[Candidate]]:
    """Each line's candidates from read 2 (``{"each": [...]}``), by the
    line's name. A skipped read is {} and none."""
    if not result:
        return {}
    each = result.get("each")
    if not isinstance(each, list):
        raise ValueError("no candidates")
    out: dict[str, list[Candidate]] = {}
    for found in each:
        if not isinstance(found, Mapping) or found.get("company") != company:
            raise ValueError("candidates of another company")
        line, rows = found.get("line"), found.get("candidates")
        name = _name(line.get("name")) if isinstance(line, Mapping) else ""
        if not name or not isinstance(rows, list):
            raise ValueError("no candidates")
        kept: list[Candidate] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("doctype") not in erpnext_match.VOUCHER_TYPES:
                continue
            voucher, amount = _name(row.get("name")), _amount(row.get("amount"))
            currency = row.get("currency")
            if not voucher or amount is None or not isinstance(currency, str):
                continue
            kept.append(Candidate(
                doctype=str(row["doctype"]), name=voucher, amount=round(amount, 2), currency=currency,
                party=_party(row), posting_date=_day(row.get("posting_date")),
                reference=_text(row.get("reference"), 140),
            ))
        out[name] = kept
    return out


def reference_in(reference: str, *texts: str) -> bool:
    """Whether a voucher's reference, at least 6 characters, appears in the
    bank's text on word boundaries (case aside)."""
    ref = reference.strip()
    if len(ref) < MIN_REFERENCE or "[number]" in ref:
        return False
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(ref)}(?![A-Za-z0-9])", re.IGNORECASE)
    return any(pattern.search(text) for text in texts if text)


def _party_name_in(party: tuple[str, str] | None, text: str) -> bool:
    if party is None or party == _UNKNOWN or len(party[1]) < 4:
        return False
    return reference_in(party[1], text) if len(party[1]) >= MIN_REFERENCE else party[1].lower() in text.lower()


def confidence(line: Line, candidate: Candidate) -> str | None:
    """How likely ``candidate`` is the voucher for ``line``: high, medium,
    low, or None (another currency, dated after the line, another party's)."""
    if (
        candidate.currency != line.currency or candidate.posting_date is None or candidate.posting_date > line.date
    ):
        return None
    if line.party == _UNKNOWN or candidate.party == _UNKNOWN:
        # Can't tell: at most low, and only on the amount.
        return LOW if _cents(candidate.amount) == _cents(line.unallocated) else None
    if line.party is not None and candidate.party is not None and candidate.party != line.party:
        return None
    same_amount = _cents(candidate.amount) == _cents(line.unallocated)
    same_party = line.party is not None and candidate.party == line.party
    if same_amount and same_party:
        return HIGH
    if same_amount and line.party is None and reference_in(candidate.reference, line.reference, line.description):
        return MEDIUM
    if same_amount or same_party or _party_name_in(candidate.party, line.description):
        return LOW
    return None


def exact_reference(line: Line, found: Sequence[Candidate]) -> bool:
    """Whether a candidate carries exactly the line's own reference: ERPNext's
    own matching rules take such a line."""
    ref = line.reference.strip().lower()
    return len(ref) >= MIN_REFERENCE and "[number]" not in ref and any(
        c.reference.strip().lower() == ref for c in found
    )


@dataclass(frozen=True)
class Outcome:
    """What the rules make of one line: a voucher to propose (``match``,
    with its confidence), why a person matches it (``person``: a ``WHY``
    code), or nothing fits (``nothing``); and the weaker candidates the card
    may show."""

    line: Line
    match: Candidate | None = None
    level: str = ""
    person: str = ""
    weaker: tuple[Candidate, ...] = ()


def classify(line: Line, found: Sequence[Candidate]) -> Outcome | None:
    """The line's outcome from its candidates alone, or None when ERPNext's
    own rules take it (an exact reference)."""
    if exact_reference(line, found):
        return None
    rated = [(c, confidence(line, c)) for c in found]
    for level in (HIGH, MEDIUM):
        best = [c for c, got in rated if got == level]
        if len(best) == 1:
            weaker = tuple(c for c, got in rated if got is not None and c is not best[0])
            return Outcome(line, match=best[0], level=level, weaker=weaker)
        if len(best) > 1:
            return Outcome(line, person="ambiguous")
    same_side = [c for c, got in rated if got is not None]
    if any(_cents(c.amount) > _cents(line.unallocated) for c in same_side if c.party == line.party and c.party):
        return Outcome(line, person="partial")
    return Outcome(line, weaker=tuple(same_side))


@dataclass(frozen=True)
class Invoice:
    name: str
    party: str
    outstanding: float
    currency: str


def invoices(result: Mapping[str, Any], party_field: str) -> list[Invoice]:
    if not result:
        return []
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ValueError("no rows")
    out: list[Invoice] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name, party, owed = _name(row.get("name")), _name(row.get(party_field)), _amount(row.get("outstanding_amount"))
        currency = row.get("currency")
        if name and party and owed is not None and owed > 0 and isinstance(currency, str):
            out.append(Invoice(name, party, round(owed, 2), currency))
    return out


def by_invoices(line: Line, open_invoices: Sequence[Invoice]) -> str:
    """Why a person matches a line nothing fits, from its party's unpaid
    invoices (a ``WHY`` code), or "" when they say nothing: one invoice of
    exactly the amount (no voucher yet), several that add up to it, or one
    larger (a partial payment)."""
    if line.party is None or line.party == _UNKNOWN:
        return ""
    kind = "Customer" if line.direction == "in" else "Supplier"
    if line.party[0] != kind:
        return ""
    mine = sorted(
        (i for i in open_invoices if i.party == line.party[1] and i.currency == line.currency),
        key=lambda i: (i.outstanding, i.name),
    )[:MAX_INVOICES_TRIED]
    target = _cents(line.unallocated)
    if any(_cents(i.outstanding) == target for i in mine):
        return "no_voucher"
    for size in range(2, MAX_COMBINED + 1):
        for combo in itertools.combinations(mine, size):
            if sum(_cents(i.outstanding) for i in combo) == target:
                return "several"
    if any(_cents(i.outstanding) > target for i in mine):
        return "partial"
    return ""


# --------------------------------------------------------------------------- #
# The reads' arguments
# --------------------------------------------------------------------------- #


def _lines_args(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    accounts = [a for a in settings["bank_accounts"] if isinstance(a, str) and a][:MAX_ACCOUNTS]
    if not accounts or not settings["company"]:
        return None
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return Repeated(tuple({"bank_account": a, "from_date": since} for a in accounts))


def _settings_company(settings: Settings) -> str:
    return str(settings["company"])


def _candidates_args(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    found, _cut = lines(prior[0], _settings_company(settings)) if prior else ([], False)
    due = to_match(found, today)
    if not due:
        return None
    return Repeated(tuple({"bank_transaction": ln.name} for ln in due[:MAX_LINES]), truncated=len(due) > MAX_LINES)


def _unfitted(settings: Settings, today: date, prior: Prior) -> list[Line]:
    """The lines no voucher fits (after reads 1 and 2), oldest first."""
    company = _settings_company(settings)
    found, _cut = lines(prior[0], company)
    cands = candidates(prior[1], company) if len(prior) > 1 else {}
    out: list[Line] = []
    for line in to_match(found, today):
        if line.name not in cands:
            continue
        outcome = classify(line, cands[line.name])
        if outcome is not None and outcome.match is None and not outcome.person:
            out.append(line)
    return out


def _parties(settings: Settings, today: date, prior: Prior, kind: str) -> tuple[list[str], bool]:
    names: list[str] = []
    for line in _unfitted(settings, today, prior):
        if line.party is not None and line.party != _UNKNOWN and line.party[0] == kind and line.party[1] not in names:
            names.append(line.party[1])
    return names[:MAX_LIST], len(names) > MAX_LIST


def _sales_args(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    parties, cut = _parties(settings, today, prior, "Customer")
    if not parties:
        return None
    return Derived({
        "doctype": "Sales Invoice",
        "fields": list(SI_FIELDS),
        "filters": {"company": settings["company"], "docstatus": 1, "outstanding_amount": [">", 0],
                    "customer": ["in", parties]},
        "limit": READ_LIMIT,
    }, truncated=cut)


def _purchase_args(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    parties, cut = _parties(settings, today, prior, "Supplier")
    if not parties:
        return None
    return Derived({
        "doctype": "Purchase Invoice",
        "fields": list(PI_FIELDS),
        "filters": {"company": settings["company"], "docstatus": 1, "outstanding_amount": [">", 0],
                    "supplier": ["in", parties]},
        "limit": READ_LIMIT,
    }, truncated=cut)


# --------------------------------------------------------------------------- #
# The findings
# --------------------------------------------------------------------------- #


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f}{f' {currency}' if currency else ''}"


def _line_values(line: Line) -> tuple[tuple[str, str], ...]:
    """A line as a notice shows it. Never a low candidate."""
    return (
        ("Bank account", line.account),
        ("Date", line.date.isoformat()),
        ("Money", "received" if line.direction == "in" else "paid out"),
        ("Amount", _money(line.unallocated, line.currency)),
        ("Party", line.party[1] if line.party and line.party != _UNKNOWN else "none"),
        ("Reference", line.reference or "none"),
    )


def build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[CardSpec | DraftSpec]:
    """The run's findings: a match card for each line one voucher fits (with
    its notice for when no card can be left), one notice of the lines a
    person matches in the Banking app, and one of the lines nothing fits.
    Figures from the reads only."""
    lines_result, candidates_result, sales_result, purchase_result = reads
    company = _settings_company(settings)
    out: list[CardSpec | DraftSpec] = []
    if not settings["bank_accounts"]:
        return [CardSpec(key=f"no-accounts{_SEP}{today.isoformat()}", summary="No bank accounts to match",
                         notes=(NO_ACCOUNTS_LINE,))]
    found, cut = lines(lines_result, company)
    cands = candidates(candidates_result, company)
    cut = cut or candidates_result.get("truncated") is True
    open_invoices = invoices(sales_result, "customer") + invoices(purchase_result, "supplier")
    person: list[tuple[Line, str]] = []
    nothing: list[Line] = []
    for line in to_match(found, today):
        if line.name not in cands:
            continue
        outcome = classify(line, cands[line.name])
        if outcome is None:
            continue
        if outcome.match is not None:
            voucher = outcome.match
            key = f"match{_SEP}{line.name}{_SEP}{voucher.doctype}{_SEP}{voucher.name}"
            out.append(DraftSpec(
                key=key,
                operation=erpnext_match.OPERATION,
                doctype=DOCTYPE,
                name=line.name,
                fields={"vouchers": [{"doctype": voucher.doctype, "name": voucher.name}]},
                expect={"company": company, "bank_account": line.account},
                title=f"Match bank line {line.name} with {voucher.doctype} {voucher.name}",
                group=line.account,
                notice=CardSpec(
                    key=key,
                    summary=f"Likely match for bank line {line.name}: {voucher.doctype} {voucher.name}",
                    values=(*_line_values(line), ("Voucher", f"{voucher.doctype} {voucher.name}"),
                            ("Its date", voucher.posting_date.isoformat() if voucher.posting_date else ""),
                            ("Its amount", _money(voucher.amount, voucher.currency)),
                            ("Confidence", outcome.level)),
                    notes=(MATCH_LINE,),
                ),
            ))
        elif outcome.person:
            person.append((line, outcome.person))
        else:
            why = by_invoices(line, open_invoices)
            if why:
                person.append((line, why))
            else:
                nothing.append(line)
    if person:
        out.append(CardSpec(
            key=f"person{_SEP}{today.isoformat()}",
            summary="Bank lines to match in ERPNext's Banking app",
            items=tuple(
                CardSpec(key=f"person{_SEP}{line.name}", summary=f"Bank line {line.name}: {WHY[why]}",
                         values=_line_values(line))
                for line, why in person
            ),
            notes=(PERSON_LINE,),
        ))
    if nothing:
        out.append(CardSpec(
            key=f"nothing{_SEP}{today.isoformat()}",
            summary="Bank lines with nothing to match",
            items=tuple(
                CardSpec(key=f"nothing{_SEP}{line.name}", summary=f"Bank line {line.name}", values=_line_values(line))
                for line in nothing
            ),
            notes=(NOTHING_LINE,),
        ))
    if cut:
        notices = [s for s in out if isinstance(s, CardSpec)]
        if notices:
            first = notices[0]
            out[out.index(first)] = CardSpec(
                first.key, first.summary, first.values, first.facts, first.items, (*first.notes, CUT_LINE), first.count,
            )
        else:
            out.append(CardSpec(key=f"cut{_SEP}{today.isoformat()}", summary="Bank lines not all read",
                                notes=(CUT_LINE,)))
    return out


def check_fields(operation: str, doctype: str, fields: Mapping[str, Any], name: str = "") -> dict[str, Any] | str:
    """A bank-matching card's fields, as they are checked, stored and sent:
    a match only (``erpnext_match.match_fields``). Run before each check and
    again at approval."""
    if operation != erpnext_match.OPERATION or doctype != DOCTYPE:
        return "The bank-matching job leaves bank matches only."
    return erpnext_match.match_fields(dict(fields) if isinstance(fields, Mapping) else fields, name)


# --------------------------------------------------------------------------- #
# The month's matches (this install's own records)
# --------------------------------------------------------------------------- #


def _previous_month(today: date) -> str:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def month_notices(settings: Settings, today: date) -> list[CardSpec]:
    """Once a month: the matches approved the month before, from the job's
    cards, for a person to check against the bank statement. Left on the
    month's first run whose notice for that month isn't there yet."""
    from openexecutive.integrations import erpnext_drafts as drafts
    from openexecutive.memory.decision_ledger import STATUS_APPROVED_UNCHANGED
    from openexecutive.people.store import get_person
    from openexecutive.watch.runner import dedup_key, raised_ever

    month = _previous_month(today)
    key = f"month{_SEP}{month}"
    if raised_ever(dedup_key(JOB_ID, key)):
        return []
    items: list[CardSpec] = []
    for card in sorted(drafts.job_cards(JOB_ID, open_only=False), key=lambda c: (c.resolved_at or "", c.id)):
        payload = drafts._payload(card)
        if payload.get("operation") != erpnext_match.OPERATION:
            continue
        made = card.status == STATUS_APPROVED_UNCHANGED
        # An approval tried and never confirmed: ERPNext may have made it.
        tried = not made and int(payload.get("attempts") or 0) > 0
        when = card.resolved_at or ""
        if not (made or tried) or (made and not when.startswith(month)) or (tried and when and not when.startswith(month)):
            continue
        kept = erpnext_match.slice_of_stored(payload.get("slice"))
        if kept is None:
            continue
        line = kept["line"]
        approver = get_person(card.resolver_person_id) if isinstance(card.resolver_person_id, int) else None
        items.append(CardSpec(
            key=f"month{_SEP}{card.id}",
            summary=f"Bank line {line['name']}",
            values=(
                ("Bank account", kept["bank_account"]),
                ("Date", line["date"]),
                ("Amount", _money(kept["total"], line["currency"])),
                ("Vouchers", ", ".join(f"{v['doctype']} {v['name']}" for v in kept["vouchers"])),
                ("Approved by", approver.full_name if approver is not None else "unknown"),
                ("Approved on", (card.resolved_at or "")[:10]) if made else ("Outcome", UNKNOWN_OUTCOME),
            ),
        ))
    if not items:
        return []
    more = len(items) - MONTH_ITEMS_MAX
    notes = (MONTH_LINE,) + ((f"And {more} more: see the job's cards.",) if more > 0 else ())
    return [CardSpec(
        key=key, summary=f"Bank matches made in {month}", items=tuple(items[:MONTH_ITEMS_MAX]), notes=notes,
        count=len(items),
        # The check on the reviewer's approvals goes to the principal, never
        # back to the reviewer who made them.
        to_principal=True,
    )]


def _month_notices(settings: Settings, today: date) -> Sequence[CardSpec]:
    return month_notices(settings, today)


JOB: WatchJob = WatchJob(
    id=JOB_ID,
    title="Bank matching",
    reads=(
        WatchRead("bank_lines_to_match", LINES_SHAPE, _lines_args, "the bank lines to match", repeat=MAX_ACCOUNTS),
        WatchRead("bank_line_candidates", CANDIDATES_SHAPE, _candidates_args, "the bank lines' candidate vouchers",
                  repeat=MAX_LINES),
        WatchRead("list_records", SI_SHAPE, _sales_args, "the customers' unpaid invoices"),
        WatchRead("list_records", PI_SHAPE, _purchase_args, "the suppliers' unpaid invoices"),
    ),
    build=build,
    schedule=SCHEDULE,
    card_cap_day=30,
    default_settings={"company": "", "bank_accounts": [], "amount_cap_day": erpnext_match.AMOUNT_CAP_DEFAULT},
    check_fields=check_fields,
    setting_bounds={"bank_accounts": (0, MAX_ACCOUNTS), "amount_cap_day": (1, erpnext_match.AMOUNT_CAP_MAX)},
    card_noun="match cards",
    extra_notices=_month_notices,
)


def listing(accounts: Sequence[str]) -> dict[str, Any]:
    """The job's entry in ``oe_connector_watch_jobs`` as an operator writes
    it for ``accounts``: read 1 once per account, by name in place of
    ``$bank_account``, which is this side's own slot."""
    reads: list[dict[str, Any]] = [
        {"tool": "bank_lines_to_match", **{**LINES_SHAPE, "bank_account": account}} for account in accounts
    ]
    reads.append({"tool": "bank_line_candidates", **CANDIDATES_SHAPE})
    reads.append({"tool": "list_records", **json.loads(json.dumps(SI_SHAPE))})
    reads.append({"tool": "list_records", **json.loads(json.dumps(PI_SHAPE))})
    return {"as_job_user": True, "reads": reads}

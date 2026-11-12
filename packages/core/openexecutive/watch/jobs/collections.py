"""The collections job (phase 1): who to chase today, and the reminders due.

Each morning at 07:00 company time, before the brief, it reads:

1. **Accounts Receivable** by due date for the company on the day
   (``run_report``): every unpaid invoice with its customer, due date and
   outstanding amount;
2. **the Dunnings sent** (``list_records``, submitted), with each one's
   customer, date, type and the invoices it covered, and **the Dunnings
   still in draft** (the same, ``docstatus`` 0), so a customer with a letter
   waiting gets no second one;
3. **the overdue customers' payment record** over the last 12 months
   (``customer_payment_record``, withheld from every other caller), for the
   50 largest by amount × days overdue, derived from read 1 and skipped when
   nobody is overdue.

Its rules are code (``rank``, ``due_invoices``), unit-tested on fixed data:

- **Who to chase, in order:** the largest and longest debts first (at least
  the median overdue amount, and ``LONG_DAYS`` or more overdue); then a
  usually prompt customer late for the first time (``PROMPT_MIN_SETTLED``
  settled invoices in 12 months, none paid late); then the rest. Within each,
  amount × days overdue, then the amount, then the name.
- **Which invoice is due for a reminder:** overdue by ``first_after_days``
  with no Dunning sent for it, or its last Dunning more than
  ``repeat_after_days`` ago. The type follows the reminder's number:
  ``type_first`` for the first, ``type_repeat`` after (empty: the company's
  default type).
- **Never a reminder for:** a customer with a Dunning in draft, one whose
  overdue total is below ``min_amount``, one marked ``phone_only``, or one
  whose payment record couldn't be read (``not_shown``, or not among the 50
  asked about).

It leaves one "Who to chase today" notice (up to ``NOTICE_MAX`` customers,
each with a reason line from the reason step) and a Dunning draft card per
(customer, invoice) due, in ranking order, at most ``card_cap_day`` a day.
The cards go only to the job's reviewer (``watch.cards.route``): ERPNext
checks each as them, and nothing is made until they approve it.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time
from statistics import median
from typing import Any

from openexecutive.watch.registry import (
    MAX_LIST,
    CardSpec,
    Derived,
    DraftSpec,
    Prior,
    ReadArguments,
    ReasonSchema,
    Settings,
    WatchJob,
    WatchRead,
)

JOB_ID = "collections"
DOCTYPE = "Dunning"
# Its reads' entries in oe_connector_watch_jobs, as oe_connector lists them
# (tests/unit/watch_job_vectors.json pins them).
RECEIVABLES_SHAPE: Mapping[str, Any] = {
    "report_name": "Accounts Receivable",
    "filters": {"company": "$company", "report_date": "$date", "ageing_based_on": "Due Date"},
    "limit": "$int",
}
DUNNING_FIELDS = ["name", "customer", "posting_date", "dunning_type", "overdue_payments.sales_invoice"]
SENT_SHAPE: Mapping[str, Any] = {
    "doctype": "Dunning",
    "fields": DUNNING_FIELDS,
    "filters": {"company": "$company", "docstatus": 1},
    "limit": "$int",
}
WAITING_SHAPE: Mapping[str, Any] = {
    "doctype": "Dunning",
    "fields": ["name", "customer"],
    "filters": {"company": "$company", "docstatus": 0},
    "limit": "$int",
}
RECORD_SHAPE: Mapping[str, Any] = {"company": "$company", "customers": "$customers"}
# The most rows a read asks for (oe_connector's own cap).
READ_LIMIT = 500
# A debt this long overdue, and at least the median amount, comes first.
LONG_DAYS = 60
# "Usually prompt": this many settled invoices in 12 months, none paid late.
PROMPT_MIN_SETTLED = 3
NOTICE_MAX = 10
# The most reminder cards one run proposes (the runner caps them by the day).
DRAFTS_MAX = 50
# What each rank says, in code's words.
RULE_LARGE_LONG = "largest and longest overdue"
RULE_PROMPT = "usually pays on time, late for the first time"
RULE_REST = "by amount and days overdue"


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


def _rows(result: Mapping[str, Any], *, required: bool) -> list[Mapping[str, Any]]:
    rows = result.get("rows")
    if rows is None and not required and not result:
        return []
    if not isinstance(rows, list):
        raise ValueError("no rows")
    return [r for r in rows if isinstance(r, Mapping)]


@dataclass
class Overdue:
    invoice: str
    due: date
    days: int
    amount: float


@dataclass
class Debtor:
    name: str
    invoices: list[Overdue] = field(default_factory=list)

    @property
    def amount(self) -> float:
        return round(sum(i.amount for i in self.invoices), 2)

    @property
    def days(self) -> int:
        return max((i.days for i in self.invoices), default=0)

    @property
    def score(self) -> float:
        return self.amount * self.days


def overdue(receivables: Mapping[str, Any], today: date) -> dict[str, Debtor]:
    """Every customer with a Sales Invoice past its due date and still owing,
    from the receivables report's rows."""
    debtors: dict[str, Debtor] = {}
    for row in _rows(receivables, required=True):
        kind = row.get("voucher_type")
        if kind not in (None, "", "Sales Invoice"):
            continue
        customer = _name(row.get("party") or row.get("customer"))
        invoice = _name(row.get("voucher_no"))
        due = _day(row.get("due_date"))
        amount = _amount(row.get("outstanding"))
        if not customer or not invoice or due is None or amount is None or amount <= 0 or due >= today:
            continue
        debtors.setdefault(customer, Debtor(customer)).invoices.append(
            Overdue(invoice, due, (today - due).days, round(amount, 2))
        )
    return debtors


def by_score(debtors: Mapping[str, Debtor]) -> list[Debtor]:
    return sorted(debtors.values(), key=lambda d: (-d.score, -d.amount, d.name))


@dataclass(frozen=True)
class Sent:
    """The Dunnings submitted for one invoice: how many, and the last one's date."""

    count: int
    last: date | None


def sent(dunnings: Mapping[str, Any]) -> tuple[dict[str, Sent], dict[str, int]]:
    """By invoice, the Dunnings sent for it; by customer, how many Dunnings
    they were sent. The list has a row per Dunning and invoice it covers."""
    by_invoice: dict[str, set[str]] = {}
    last: dict[str, date] = {}
    by_customer: dict[str, set[str]] = {}
    for row in _rows(dunnings, required=True):
        name = _name(row.get("name"))
        invoice = _name(row.get("sales_invoice") or row.get("overdue_payments.sales_invoice"))
        customer = _name(row.get("customer"))
        if not name:
            continue
        if customer:
            by_customer.setdefault(customer, set()).add(name)
        if not invoice:
            continue
        by_invoice.setdefault(invoice, set()).add(name)
        posted = _day(row.get("posting_date"))
        if posted is not None and (invoice not in last or posted > last[invoice]):
            last[invoice] = posted
    return (
        {inv: Sent(len(names), last.get(inv)) for inv, names in by_invoice.items()},
        {customer: len(names) for customer, names in by_customer.items()},
    )


def waiting(drafts: Mapping[str, Any]) -> set[str]:
    """The customers with a Dunning still in draft."""
    return {c for c in (_name(r.get("customer")) for r in _rows(drafts, required=True)) if c}


def records(result: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], set[str]]:
    """``customer_payment_record``'s rows by customer, and the customers it
    couldn't show (``not_shown``). A skipped read is {} and neither."""
    if not result:
        return {}, set()
    rows = result.get("customers")
    if not isinstance(rows, list):
        raise ValueError("no customers")
    out = {
        _name(r.get("customer")): r for r in rows if isinstance(r, Mapping) and _name(r.get("customer"))
    }
    hidden = result.get("not_shown")
    return out, {_name(n) for n in hidden if _name(n)} if isinstance(hidden, list) else set()


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usually_prompt(record: Mapping[str, Any] | None) -> bool:
    """Late for the first time: enough settled invoices in 12 months, none
    paid late."""
    if record is None:
        return False
    settled, late = _count(record.get("settled_invoices")), _count(record.get("paid_late"))
    return settled is not None and settled >= PROMPT_MIN_SETTLED and late == 0


def rank(debtors: Mapping[str, Debtor], recs: Mapping[str, Mapping[str, Any]]) -> list[tuple[Debtor, str]]:
    """Who to chase, in order, each with the rule that put them there. A
    customer without a record is ranked without the "usually prompt" step."""
    if not debtors:
        return []
    middle = median(d.amount for d in debtors.values())

    def tier(debtor: Debtor) -> tuple[int, str]:
        if debtor.amount >= middle and debtor.days >= LONG_DAYS:
            return 0, RULE_LARGE_LONG
        if usually_prompt(recs.get(debtor.name)):
            return 1, RULE_PROMPT
        return 2, RULE_REST

    ranked = [(debtor, *tier(debtor)) for debtor in by_score(debtors)]
    ranked.sort(key=lambda t: t[1])  # stable: within a tier, by_score's order
    return [(debtor, rule) for debtor, _tier, rule in ranked]


def due_invoices(
    debtor: Debtor, sent_for: Mapping[str, Sent], settings: Settings, today: date,
) -> list[tuple[Overdue, int]]:
    """The debtor's invoices due for a reminder, oldest first, each with the
    reminder's number."""
    out: list[tuple[Overdue, int]] = []
    for invoice in sorted(debtor.invoices, key=lambda i: (-i.days, i.invoice)):
        before = sent_for.get(invoice.invoice)
        if before is None or before.count == 0:
            if invoice.days >= int(settings["first_after_days"]):
                out.append((invoice, 1))
        elif before.last is not None and (today - before.last).days > int(settings["repeat_after_days"]):
            out.append((invoice, before.count + 1))
    return out


def dunning_type(settings: Settings, number: int) -> str:
    return str(settings["type_first"] if number == 1 else settings["type_repeat"])


# --------------------------------------------------------------------------- #
# The reads' arguments
# --------------------------------------------------------------------------- #


def _receivables(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    return {
        "report_name": "Accounts Receivable",
        "filters": {"company": settings["company"], "report_date": today.isoformat(), "ageing_based_on": "Due Date"},
        "limit": READ_LIMIT,
    }


def _dunnings(docstatus: int, fields: list[str]) -> Any:
    def build(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
        return {
            "doctype": "Dunning",
            "fields": list(fields),
            "filters": {"company": settings["company"], "docstatus": docstatus},
            "limit": READ_LIMIT,
        }

    return build


def _record(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    """The overdue customers' payment record: the 50 largest by amount ×
    days, from read 1. Skipped when nobody is overdue."""
    debtors = by_score(overdue(prior[0], today)) if prior else []
    if not debtors:
        return None
    return Derived(
        {"company": settings["company"], "customers": [d.name for d in debtors[:MAX_LIST]]},
        truncated=len(debtors) > MAX_LIST,
    )


# --------------------------------------------------------------------------- #
# The findings
# --------------------------------------------------------------------------- #


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f}{f' {currency}' if currency else ''}"


def _pays(record: Mapping[str, Any] | None) -> str:
    if record is None:
        return "record couldn't be read"
    settled = _count(record.get("settled_invoices")) or 0
    if settled == 0:
        return "no settled invoices in 12 months"
    on_time = _count(record.get("paid_on_time")) or 0
    middle = record.get("median_days_after_due")
    after = f", median {middle:g} days after due" if isinstance(middle, int | float) and not isinstance(middle, bool) else ""
    return f"{on_time} of {settled} on time in 12 months{after}"


def _facts(debtor: Debtor, rule: str, record: Mapping[str, Any] | None, reminders: int) -> dict[str, str | int | float]:
    facts: dict[str, str | int | float] = {
        "overdue_amount": debtor.amount,
        "days_overdue": debtor.days,
        "overdue_invoices": len(debtor.invoices),
        "reminders_sent": reminders,
        "ranked_as": rule,
    }
    if record is not None:
        for key in ("settled_invoices", "paid_late", "median_days_after_due"):
            value = record.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                facts[key] = value
    return facts


def build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[CardSpec | DraftSpec]:
    """The "Who to chase today" notice and the reminder drafts due, figures
    from the reads only."""
    receivables, sent_result, drafts_result, record_result = reads
    debtors = overdue(receivables, today)
    if not debtors:
        return []
    sent_for, reminders = sent(sent_result)
    has_draft = waiting(drafts_result)
    recs, not_shown = records(record_result)
    ranked = rank(debtors, recs)
    given = receivables.get("currency")
    currency = given if isinstance(given, str) else ""
    phone_only = set(settings["phone_only"])

    items: list[CardSpec] = []
    for debtor, rule in ranked[:NOTICE_MAX]:
        record = recs.get(debtor.name)
        due = due_invoices(debtor, sent_for, settings, today)
        items.append(CardSpec(
            key=f"customer:{debtor.name}",
            summary=debtor.name,
            values=(
                ("Overdue", _money(debtor.amount, currency)),
                ("Oldest", f"{debtor.days} days overdue"),
                ("Invoices overdue", str(len(debtor.invoices))),
                ("Pays", _pays(record)),
                ("Reminders sent", str(reminders.get(debtor.name, 0))),
                ("Reminders due", str(len(due))),
                ("Ranked", rule),
            ),
            facts=_facts(debtor, rule, record, reminders.get(debtor.name, 0)),
        ))

    notes: list[str] = []
    if len(ranked) > NOTICE_MAX:
        notes.append(f"{len(ranked) - NOTICE_MAX} more customers are overdue; the first {NOTICE_MAX} are shown.")
    unread = [d for d, _rule in ranked if d.name not in recs]
    if unread:
        notes.append(
            f"{len(unread)} overdue customers' payment records couldn't be read, so they are ranked "
            "without how they usually pay and get no reminder draft today."
        )
    drafts: list[DraftSpec] = []
    if sent_result.get("truncated") is True or drafts_result.get("truncated") is True:
        notes.append("Not every reminder already sent could be read, so no reminder drafts were prepared today.")
    else:
        for debtor, _rule in ranked:
            if (
                debtor.name not in recs or debtor.name in not_shown or debtor.name in has_draft
                or debtor.name in phone_only or debtor.amount < settings["min_amount"]
            ):
                continue
            for invoice, number in due_invoices(debtor, sent_for, settings, today):
                kind = dunning_type(settings, number)
                fields = {"sales_invoice": invoice.invoice, **({"dunning_type": kind} if kind else {})}
                drafts.append(DraftSpec(
                    key=f"dunning:{invoice.invoice}:{kind}",
                    operation="create",
                    doctype=DOCTYPE,
                    fields=fields,
                    expect={"sales_invoice": invoice.invoice, "customer": debtor.name, "company": str(settings["company"])},
                    title=f"Payment reminder {number} for {invoice.invoice}: {debtor.name}",
                    reason_key=f"customer:{debtor.name}",
                    group=debtor.name,
                ))
    notice = CardSpec(
        key=f"chase:{settings['company']}:{today.isoformat()}",
        summary="Who to chase today",
        values=(("Customers overdue", str(len(ranked))),
                ("Overdue in all", _money(round(sum(d.amount for d in debtors.values()), 2), currency))),
        items=tuple(items),
        notes=tuple(notes),
        count=len(ranked),
    )
    return [notice, *drafts[:DRAFTS_MAX]]


def check_fields(operation: str, doctype: str, fields: Mapping[str, Any]) -> dict[str, Any] | str:
    """A collections card's fields: a Dunning's {sales_invoice, dunning_type,
    posting_date} exactly as ``erpnext_drafts`` checks a Dunning, and only
    those. Run before each check and again at approval."""
    from openexecutive.integrations.erpnext_drafts import _DUNNING_FIELDS, _dunning

    if operation != "create" or doctype != DOCTYPE:
        return "The collections job leaves Dunning drafts only."
    if not isinstance(fields, Mapping) or not fields or set(fields) - _DUNNING_FIELDS:
        return "A collections card's fields are sales_invoice, dunning_type and posting_date."
    try:
        out = _dunning(dict(fields))
    except ValueError as exc:
        return str(exc)
    if out != dict(fields):
        return "A collections card's fields must be as the job built them."
    return out


JOB: WatchJob = WatchJob(
    id=JOB_ID,
    title="Collections",
    reads=(
        WatchRead("run_report", RECEIVABLES_SHAPE, _receivables, "the receivables report"),
        WatchRead("list_records", SENT_SHAPE, _dunnings(1, DUNNING_FIELDS), "the reminders sent"),
        WatchRead("list_records", WAITING_SHAPE, _dunnings(0, ["name", "customer"]), "the reminders in draft"),
        WatchRead("customer_payment_record", RECORD_SHAPE, _record, "the customers' payment records"),
    ),
    build=build,
    # Before the morning brief.
    schedule=time(7, 0),
    card_cap_day=5,
    default_settings={
        "company": "",
        "first_after_days": 7,
        "repeat_after_days": 14,
        "min_amount": 0,
        "type_first": "",
        "type_repeat": "",
        "phone_only": [],
    },
    reason_schema=ReasonSchema(
        "Say in one sentence why this customer is worth chasing today: how much they owe, for how long, "
        "and how they usually pay."
    ),
    check_fields=check_fields,
    setting_bounds={"first_after_days": (1, 400), "repeat_after_days": (1, 400), "min_amount": (0, 1_000_000_000)},
    card_noun="reminder drafts",
)

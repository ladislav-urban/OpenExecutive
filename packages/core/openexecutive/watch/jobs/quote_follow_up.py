"""The quote-follow-up job (phase 1): the quotations about to lapse, and those
that lapsed this week with no order, each with whom to call.

Each weekday at 07:30 company time it reads:

1. **every submitted quotation to a Customer valid until at least a week
   ago** (``list_records``, ``quotation_to`` Customer, ``valid_till >=
   today - LAPSED_DAYS``). One read: a shape holds neither a list of
   statuses nor two bounds on one field, so code sorts what is lapsing from
   what has lapsed;
2. **the same for quotations to a Lead**, with only their name, date,
   value, currency and status: counted, never named (no party, name or
   contact field is ever read for them);
3. **the customers' Sales Orders** since the oldest of read 1's quotations
   (``list_records``, ``customer in`` read 1's customers, at most 50, the
   most pressing first), with each order line's quotation
   (``items.prevdoc_docname``). Derived from read 1 and skipped when no
   quotation to a customer counts.

No read relies on ``list_records``' ``titles``.

Its rules are code (``candidates``, ``build``), unit-tested on fixed data:

- **Lapsing:** still open (Open, Replied or Partially Ordered) and valid
  until today or within ``days_ahead`` days.
- **Lapsed:** valid until 1 to ``LAPSED_DAYS`` days ago and Expired (or
  still Open or Replied, when ERPNext's daily expiry hasn't run yet).
- **Never:** below ``min_amount``; a quotation a Sales Order line names
  (itself or the one it was amended from), whatever ERPNext's status says;
  a quotation revised since (another names it in ``amended_from``): a
  revision counts once, as its latest.
- **Order:** lapsed first, then by value × closeness to lapsing
  (``grand_total / (1 + days)``), then by name.
- **Quotations to a Lead** (read 2) are counted, each row once (its
  ``amended_from`` isn't read), never named or carded: the job doesn't read
  Lead, nor any name field of those quotations.

What it leaves:

- **One notice, "Quotations to follow up":** up to ``NOTICE_MAX``, each
  with the customer, value, date quoted, valid till, days left (or "lapsed
  N days ago") and status, and code's count of those to leads. A note says
  when a read was cut.
- **A ToDo draft card per quotation to a customer**, for the job's
  reviewer: assigned to the job's ``assign_to`` (an ERPNext user), else the
  reviewer; on the quotation; dated today, or the day before it lapses when
  that's later; High for the three largest, else Medium; its description
  code's template only (``description``). No card when the quotation's
  orders couldn't all be read.

A quotation is raised once while lapsing and once after it lapses, never
daily (``WatchJob.once``, the finding key ``q:<name>:<lapsing|lapsed>``): a
row a notice told isn't told again, and a card left once isn't left again.
No model is called: every line is code's.

A known residual, as in payment checks: a quotation's row reaches the
reviewer's notice when its card's check doesn't run that run (the day's cap,
a card already waiting), even if ERPNext would keep the quotation from them;
only a check ERPNext refuses for their permissions takes the row off.
"""
from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

from openexecutive.watch.registry import (
    DATE_RANGE_DAYS,
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

JOB_ID = "quote-follow-up"
DOCTYPE = "ToDo"
QUOTATION = "Quotation"
QUOTE_FIELDS = [
    "name", "party_name", "customer_name", "transaction_date", "valid_till", "grand_total",
    "currency", "status", "order_type", "amended_from",
]
# A lead's quotation: never a party, name or contact field.
LEAD_FIELDS = ["name", "valid_till", "grand_total", "currency", "status"]
ORDER_FIELDS = ["name", "customer", "transaction_date", "items.prevdoc_docname"]
# Its reads' entries in oe_connector_watch_jobs, as oe_connector lists them
# (tests/unit/watch_job_vectors.json pins them).
QUOTES_SHAPE: Mapping[str, Any] = {
    "doctype": QUOTATION,
    "fields": QUOTE_FIELDS,
    "filters": {"company": "$company", "docstatus": 1, "quotation_to": "Customer", "valid_till": [">=", "$date"]},
    "limit": "$int",
}
LEADS_SHAPE: Mapping[str, Any] = {
    "doctype": QUOTATION,
    "fields": LEAD_FIELDS,
    "filters": {"company": "$company", "docstatus": 1, "quotation_to": "Lead", "valid_till": [">=", "$date"]},
    "limit": "$int",
}
ORDERS_SHAPE: Mapping[str, Any] = {
    "doctype": "Sales Order",
    "fields": ORDER_FIELDS,
    "filters": {
        "company": "$company",
        "docstatus": ["<", 2],
        "customer": ["in", "$customers"],
        "transaction_date": [">=", "$date"],
    },
    "limit": "$int",
}
READ_LIMIT = 500
# A quotation that lapsed this many days ago or less still counts (B1).
LAPSED_DAYS = 7
NOTICE_MAX = 10
# The quotations a run marks High: the largest this many.
HIGH_TOP = 3
# The most ToDo cards one run proposes (the runner caps them by the day).
DRAFTS_MAX = 50
LAPSING, LAPSED = "lapsing", "lapsed"
# Open, in the catalog's own sense, and what ERPNext's daily expiry leaves.
OPEN_STATUSES = frozenset({"Open", "Replied", "Partially Ordered"})
LAPSED_STATUSES = frozenset({"Expired", "Open", "Replied"})
DESCRIPTION_MAX = 1000
CUSTOMER_MAX = 140
# A ToDo's description, code's template only (never a model's).
TEMPLATE = (
    "Call {customer} about {quotation} ({value}, valid till {valid_till}): ask whether it's still wanted, "
    "and what would win it. If it's lapsed, offer to re-quote at today's prices."
)
_TEMPLATE_RE = re.compile(
    r"Call (?P<customer>.{1,140}) about (?P<quotation>\S{1,140}) \((?P<value>-?[\d,]+\.\d\d(?: [A-Z]{3})?), "
    r"valid till (?P<valid_till>\d{4}-\d{2}-\d{2})\): ask whether it's still wanted, and what would win it\. "
    r"If it's lapsed, offer to re-quote at today's prices\."
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# A currency as a ToDo names it: ISO 4217's three capitals, else left out.
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
KEPT_LINE = (
    "The ERP didn't let the job's reviewer prepare its task, so it is told here: follow it up in the ERP, "
    "or give the reviewer access to the quotation."
)
LEADS_LINE = "{n} more to leads, not named."
QUOTES_CUT_LINE = "Not every quotation could be read this run, so some may not be shown."
LEADS_CUT_LINE = "Not every quotation to a lead could be read this run, so the count of those may be short."
ORDERS_CUT_LINE = (
    "Not every sales order could be read this run, so a quotation shown may already have an order; "
    "no tasks were prepared for those."
)
MORE_LINE = "{n} more quotations to follow up; the first {shown} are shown."


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
    """A ``list_records`` result's rows. A skipped read is {} and none."""
    if not result and not required:
        return []
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ValueError("no rows")
    return [r for r in rows if isinstance(r, Mapping)]


@dataclass(frozen=True)
class Quote:
    name: str
    # The Customer (its id), or "" for a quotation to a Lead or anyone else.
    customer: str
    customer_name: str
    quoted: date | None
    valid_till: date
    grand_total: float
    currency: str
    status: str
    amended_from: str

    @property
    def to_customer(self) -> bool:
        return bool(self.customer)


def quotes(result: Mapping[str, Any], *, leads: bool = False) -> list[Quote]:
    """The quotations in read 1's result (to customers), or with ``leads``
    in read 2's: those keep no customer, name or revision, whatever a row
    holds."""
    out: list[Quote] = []
    for row in _rows(result, required=True):
        name = _name(row.get("name"))
        valid_till = _day(row.get("valid_till"))
        total = _amount(row.get("grand_total"))
        if not name or valid_till is None or total is None:
            continue
        customer = "" if leads else _name(row.get("party_name"))
        if not leads and not customer:
            continue
        out.append(Quote(
            name=name, customer=customer,
            customer_name="" if leads else (_name(row.get("customer_name")) or customer),
            quoted=None if leads else _day(row.get("transaction_date")), valid_till=valid_till,
            # ERPNext's text capped (_name) before anything cleans it.
            grand_total=round(total, 2), currency=_name(row.get("currency")),
            status=_name(row.get("status")),
            amended_from="" if leads else _name(row.get("amended_from")),
        ))
    return out


def ordered(result: Mapping[str, Any]) -> set[str]:
    """The quotations a Sales Order line names (read 3). The list has a row
    per order and line. A skipped read is {} and none."""
    out: set[str] = set()
    for row in _rows(result, required=False):
        quotation = _name(row.get("prevdoc_docname") or row.get("items.prevdoc_docname"))
        if quotation:
            out.add(quotation)
    return out


def state(quote: Quote, settings: Settings, today: date) -> str | None:
    """Lapsing, lapsed, or None when the quotation doesn't count today (by
    its dates and status only)."""
    days = (quote.valid_till - today).days
    if 0 <= days <= int(settings["days_ahead"]) and quote.status in OPEN_STATUSES:
        return LAPSING
    if -LAPSED_DAYS <= days <= -1 and quote.status in LAPSED_STATUSES:
        return LAPSED
    return None


def latest(found: Sequence[Quote]) -> list[Quote]:
    """Each revision counted once, as its latest: a quotation another names
    in ``amended_from`` is dropped."""
    replaced = {q.amended_from for q in found if q.amended_from}
    return [q for q in found if q.name not in replaced]


@dataclass(frozen=True)
class FollowUp:
    quote: Quote
    state: str
    # Days until it lapses (0: today), or minus the days since it lapsed.
    days: int

    @property
    def key(self) -> str:
        return f"q:{self.quote.name}:{self.state}"

    @property
    def score(self) -> float:
        return self.quote.grand_total / (1 + abs(self.days))


def candidates(
    result: Mapping[str, Any], settings: Settings, today: date, *, leads: bool = False,
) -> list[FollowUp]:
    """Read 1's quotations that count by date, status, amount and revision,
    in the job's order, before any order is checked; with ``leads``, read
    2's, each row counted once."""
    out: list[FollowUp] = []
    floor = int(settings["min_amount"])
    read = quotes(result, leads=leads)
    for quote in (read if leads else latest(read)):
        found = state(quote, settings, today)
        if found is None or quote.grand_total < floor:
            continue
        out.append(FollowUp(quote, found, (quote.valid_till - today).days))
    return sorted(out, key=lambda f: (f.state != LAPSED, -f.score, f.quote.name))


def customers_to_check(result: Mapping[str, Any], settings: Settings, today: date) -> tuple[list[str], bool, date | None]:
    """The customers whose orders read 2 asks for (the most pressing
    quotations' first, at most ``MAX_LIST``), whether more were cut, and the
    oldest of their quotations' dates."""
    from openexecutive.watch.registry import _slot_holds

    order: list[str] = []
    oldest: date | None = None
    for follow in candidates(result, settings, today):
        quote = follow.quote
        # An id the slot wouldn't take (an invisible character in it) is
        # never asked about: its quotations stay unchecked, with no task.
        if not quote.to_customer or not _slot_holds("$customers", [quote.customer], today):
            continue
        if quote.customer not in order:
            order.append(quote.customer)
        if quote.customer in order[:MAX_LIST]:
            day = quote.quoted or quote.valid_till
            oldest = day if oldest is None or day < oldest else oldest
    return order[:MAX_LIST], len(order) > MAX_LIST, oldest


# --------------------------------------------------------------------------- #
# The reads' arguments
# --------------------------------------------------------------------------- #


def _quotations(to: str, fields: list[str]) -> Any:
    def arguments(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
        return {
            "doctype": QUOTATION,
            "fields": list(fields),
            "filters": {
                "company": settings["company"],
                "docstatus": 1,
                "quotation_to": to,
                "valid_till": [">=", (today - timedelta(days=LAPSED_DAYS)).isoformat()],
            },
            "limit": READ_LIMIT,
        }

    return arguments


_quotes = _quotations("Customer", QUOTE_FIELDS)
_leads = _quotations("Lead", LEAD_FIELDS)


def _orders(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    customers, cut, oldest = customers_to_check(prior[0], settings, today) if prior else ([], False, None)
    if not customers:
        return None
    # Within the slot's date range, whatever a quotation's own date.
    since = min(max(oldest or today, today - timedelta(days=DATE_RANGE_DAYS)), today)
    return Derived({
        "doctype": "Sales Order",
        "fields": list(ORDER_FIELDS),
        "filters": {
            "company": settings["company"],
            "docstatus": ["<", 2],
            "customer": ["in", customers],
            "transaction_date": [">=", since.isoformat()],
        },
        "limit": READ_LIMIT,
    }, truncated=cut)


# --------------------------------------------------------------------------- #
# The findings
# --------------------------------------------------------------------------- #


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f}{f' {currency}' if currency else ''}"


def _when(follow: FollowUp) -> str:
    if follow.state == LAPSED:
        days = -follow.days
        return f"lapsed {days} day{'s' if days != 1 else ''} ago"
    if follow.days == 0:
        return "lapses today"
    return f"{follow.days} day{'s' if follow.days != 1 else ''} left"


def settled(text: str) -> str:
    """``text`` cleaned as a notice cleans it (one line, no links or
    markdown, at most 140), again until cleaning changes nothing, so a
    stored ToDo can be held to it: a link's "[link]" loses its brackets,
    and the result is its own clean. "" when it never settles."""
    from openexecutive.watch.runner import clean

    for _ in range(5):
        again = clean(text, CUSTOMER_MAX)
        if again == text:
            return text
        text = again
    return ""


def _customer_text(quote: Quote) -> str:
    """The customer as the ToDo names it: ERPNext's name, ``settled``."""
    return settled(quote.customer_name) or settled(quote.customer) or "the customer"


def _todo_currency(currency: str) -> str:
    return currency if _CURRENCY_RE.fullmatch(currency) else ""


def nameable(name: str) -> bool:
    """Whether a quotation's name can stand in a ToDo's description as is:
    ERPNext's name pattern, and its own clean."""
    from openexecutive.integrations.erpnext_changes import _DOC_RE

    return _DOC_RE.fullmatch(name) is not None and not any(c.isspace() for c in name) and settled(name) == name


def description(quote: Quote) -> str:
    """The ToDo's description: code's template around the quotation's own
    figures and the customer's cleaned name, at most 1,000 characters."""
    text = TEMPLATE.format(
        customer=_customer_text(quote), quotation=quote.name,
        value=_money(quote.grand_total, _todo_currency(quote.currency)),
        valid_till=quote.valid_till.isoformat(),
    )
    return " ".join(_CONTROL.sub(" ", text).split())[:DESCRIPTION_MAX]


def _row(follow: FollowUp, notes: tuple[str, ...] = ()) -> CardSpec:
    """A quotation as the notice shows it: one of its rows, or a notice of
    its own (``notes``)."""
    quote = follow.quote
    return CardSpec(
        key=follow.key,
        summary=quote.customer_name or quote.customer,
        values=(
            ("Quotation", quote.name),
            ("Value", _money(quote.grand_total, quote.currency)),
            ("Quoted", quote.quoted.isoformat() if quote.quoted else ""),
            ("Valid till", quote.valid_till.isoformat()),
            ("Lapses", _when(follow)),
            ("Status", quote.status),
        ),
        notes=notes,
    )


def todo_date(follow: FollowUp, today: date) -> date:
    """Today, or the day before it lapses when that's later."""
    return max(today, follow.quote.valid_till - timedelta(days=1))


def build(
    reads: Sequence[Mapping[str, Any]], settings: Settings, today: date, *,
    told: Collection[str] = frozenset(), assignee: str = "",
) -> list[CardSpec | DraftSpec]:
    """The "Quotations to follow up" notice and a ToDo per quotation to a
    customer, figures from the reads only. ``told``: the finding keys a
    notice told before (left out of this one); ``assignee``: who the ToDos
    are for (``assign_to``, else the reviewer's ERPNext user)."""
    quotes_result, leads_result, orders_result = reads
    found = candidates(quotes_result, settings, today)
    checked, cut, _oldest = customers_to_check(quotes_result, settings, today)
    asked = set(checked)
    closed = ordered(orders_result)
    orders_cut = orders_result.get("truncated") is True

    def is_ordered(quote: Quote) -> bool:
        return quote.name in closed or bool(quote.amended_from and quote.amended_from in closed)

    named = [f for f in found if not is_ordered(f.quote)]
    leads = candidates(leads_result, settings, today, leads=True) if leads_result else []
    if not named and not leads:
        return []
    # Orders not read for every customer: no task for those quotations.
    unchecked = {f.quote.name for f in named if f.quote.customer not in asked or orders_cut}
    # A name the ToDo's template can't hold as is gets no task either.
    untasked = {f.quote.name for f in named if not nameable(f.quote.name)}

    new = [f for f in named if f.key not in told]
    new_leads = [f for f in leads if f.key not in told]
    shown = new[:NOTICE_MAX]
    items = tuple(_row(f) for f in shown)
    notes: list[str] = []
    if len(new) > NOTICE_MAX:
        notes.append(MORE_LINE.format(n=len(new) - NOTICE_MAX, shown=NOTICE_MAX))
    if new_leads:
        notes.append(LEADS_LINE.format(n=len(new_leads)))
    if quotes_result.get("truncated") is True:
        notes.append(QUOTES_CUT_LINE)
    if leads_result.get("truncated") is True:
        notes.append(LEADS_CUT_LINE)
    if unchecked & {f.quote.name for f in new}:
        notes.append(ORDERS_CUT_LINE)
    out: list[CardSpec | DraftSpec] = []
    if items or new_leads:
        out.append(CardSpec(
            key=f"quotes:{settings['company']}:{today.isoformat()}",
            summary="Quotations to follow up",
            items=items,
            notes=tuple(notes),
            count=len(new),
            counted=tuple(f.key for f in new_leads),
        ))
    largest = {f.quote.name for f in sorted(named, key=lambda f: (-f.quote.grand_total, f.quote.name))[:HIGH_TOP]}
    drafts: list[DraftSpec] = []
    for follow in named:
        quote = follow.quote
        if quote.name in unchecked or quote.name in untasked:
            continue
        text = description(quote)
        drafts.append(DraftSpec(
            key=follow.key,
            operation="create",
            doctype=DOCTYPE,
            fields={
                "description": text,
                "allocated_to": assignee,
                "priority": "High" if quote.name in largest else "Medium",
                "date": todo_date(follow, today).isoformat(),
                "reference_type": QUOTATION,
                "reference_name": quote.name,
            },
            expect={"reference": f"{QUOTATION} {quote.name}", "description": text},
            title=f"Follow up {quote.name}: {_customer_text(quote)}",
            group=quote.customer,
            # Told on its own only when ERPNext keeps the quotation from the
            # reviewer (the runner strips its row from the reviewer's notice
            # and sends this to the principal).
            notice=_row(follow, notes=(KEPT_LINE,)),
        ))
    return [*out, *drafts[:DRAFTS_MAX]]


def assignee(settings: Settings | None = None) -> str:
    """Who the job's ToDos are for: its ``assign_to`` setting, else its
    reviewer's email (their ERPNext user), else "" (no card can be left)."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.people.store import get_person
    from openexecutive.watch import registry
    from openexecutive.watch.settings import effective_settings, get_row

    job = registry.get(JOB_ID)
    if job is None:
        return ""
    row = get_row(JOB_ID)
    given = str((settings if settings is not None else effective_settings(job, row)).get("assign_to") or "")
    if given:
        return given
    reviewer_id = getattr(row, "reviewer_person_id", None)
    if not isinstance(reviewer_id, int) or isinstance(reviewer_id, bool):
        return ""
    person = get_person(reviewer_id)
    email = normalize_email(person.email or "") if person is not None else ""
    return email if "@" in email else ""


def _build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[CardSpec | DraftSpec]:
    """``build`` with what this install knows: the findings told before and
    who the ToDos are for."""
    from openexecutive.watch.runner import told

    keys = [f.key for f in candidates(reads[0], settings, today)]
    if reads[1]:
        keys += [f.key for f in candidates(reads[1], settings, today, leads=True)]
    return build(reads, settings, today, told=told(JOB, keys), assignee=assignee(settings))


def check_fields(operation: str, doctype: str, fields: Mapping[str, Any], name: str = "") -> dict[str, Any] | str:
    """A quote-follow-up card's fields, as they are checked, stored and sent:
    a new ToDo on a Quotation, assigned to the job's assignee now, High or
    Medium, its description exactly the job's template naming that
    quotation, and exactly ``erpnext_drafts._todo``'s output. Run before
    each check and again at approval."""
    from openexecutive.integrations.erpnext_changes import _DOC_RE
    from openexecutive.integrations.erpnext_drafts import _TODO_FIELDS, _todo

    if operation != "create" or doctype != DOCTYPE or name:
        return "The quote-follow-up job leaves ToDo drafts only."
    if not isinstance(fields, Mapping) or set(fields) != _TODO_FIELDS:
        return "A quote-follow-up card's fields are a ToDo's six, all of them."
    reference = fields.get("reference_name")
    if fields.get("reference_type") != QUOTATION or not isinstance(reference, str) or not _DOC_RE.fullmatch(reference):
        return "A quote-follow-up card's ToDo is on a Quotation."
    if fields.get("priority") not in ("High", "Medium"):
        return "A quote-follow-up card's priority is High or Medium."
    text = fields.get("description")
    found = _TEMPLATE_RE.fullmatch(text) if isinstance(text, str) else None
    if (
        found is None or found.group("quotation") != reference or len(text or "") > DESCRIPTION_MAX
        or _CONTROL.search(text or "") or text != " ".join((text or "").split())
        or found.group("customer") != settled(found.group("customer")) or not nameable(reference)
    ):
        return "A quote-follow-up card's description is the job's own: Call <customer> about <quotation> ..."
    expected = assignee()
    if not expected or fields.get("allocated_to") != expected:
        return "A quote-follow-up card's ToDo is for the job's assignee: its assign_to, else its reviewer."
    try:
        out = _todo(dict(fields))
    except ValueError as exc:
        return str(exc)
    if out != dict(fields):
        return "A quote-follow-up card's fields must be as the job built them."
    return out


JOB: WatchJob = WatchJob(
    id=JOB_ID,
    title="Quote follow-up",
    reads=(
        WatchRead("list_records", QUOTES_SHAPE, _quotes, "the quotations"),
        WatchRead("list_records", LEADS_SHAPE, _leads, "the quotations to leads"),
        WatchRead("list_records", ORDERS_SHAPE, _orders, "the customers' sales orders"),
    ),
    build=_build,
    # Each weekday morning, after collections and before the working day.
    schedule=time(7, 30),
    weekdays=frozenset(range(5)),
    card_cap_day=5,
    default_settings={"company": "", "days_ahead": 5, "min_amount": 0, "assign_to": ""},
    check_fields=check_fields,
    setting_bounds={"days_ahead": (1, 30), "min_amount": (0, 1_000_000_000)},
    card_noun="follow-up tasks",
    once=True,
)

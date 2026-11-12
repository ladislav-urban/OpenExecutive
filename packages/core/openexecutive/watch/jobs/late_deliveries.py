"""The late-deliveries job (phase 1): the open purchase orders past their
date, worst first, each with its supplier's delivery record and a chase note
for the buyer.

Each weekday at 07:15 company time it reads:

1. **every submitted Purchase Order still to receive, not fully received
   and due before today** (``list_records``, ``per_received < 100``,
   ``schedule_date < today``), with its lines' item, quantity, received
   quantity and date (``items.*``: a row per order and line), in two reads
   of one fixed status each: 1a "To Receive and Bill", 1b "To Receive". A
   shape holds no list of statuses, and one read of every status filled
   its 500 rows with Closed and On Hold orders' unreceived lines, so code
   merges the two (and still drops Closed, On Hold and Completed, should
   one come back). Each call leaves out the order's own ``schedule_date``
   (a call may ask fewer fields than its shape lists): ERPNext returns a
   line's field under its bare name, so a bare ``schedule_date`` is then
   only ever the line's;
2. **the suppliers' delivery record** over the last 12 months
   (``run_report`` Supplier Delivery Record, one row per supplier: lines
   received, lines late, average days late). Derived from reads 1a and 1b
   and skipped when no order counts. The report takes one optional supplier,
   never a list, so it's one call for all.

Neither reads Contact, a supplier's bank details or a person's hours.

Its rules are code (``candidates``, ``build``), unit-tested on fixed data:

- **A late line:** received less than ordered and due before today. An
  order is as late as its oldest late line, in days, and counts from
  ``min_days_late`` days; never a supplier in ``skip_suppliers``. Every
  text ERPNext returns is capped at 140 characters before it is cleaned.
- **Order:** an order no earlier week's card was left for first, then
  the one carded longest ago (``card_history``: so a weekly restart never
  starves the newest late orders of a card while the stale ones get one
  every week), then the most days late, the order's value and its name.
- **The supplier's record**, in code's words: "late on 4 of 20 lines in 12
  months, 3.5 days on average", "on time on all 20 lines in 12 months", or
  "no receipts in 12 months" when the report has none for them.

What it leaves:

- **One notice, "Late deliveries":** up to ``NOTICE_MAX`` orders, each with
  its supplier, how late, the oldest due date, what's missing, the value,
  the status and the supplier's record. A note says when a read was cut.
- **A ToDo draft card per late order**, for the job's reviewer: assigned to
  the job's ``assign_to`` (an ERPNext user), else the reviewer; on the
  order; dated today; High when more than a week late, else Medium; its
  description a chase note from code's template only (``description``),
  which the buyer sends from their own mail. No card for an order read
  from a list that was cut (its lines may be short), nor while an earlier week's card for
  the same order still waits, nor when the order's name or an item's
  doesn't fit the template (the notice says so).

An order is raised once a week while it's late (``WatchJob.once``, the
finding key ``po:<name>:<ISO week>``): a row a notice told isn't told again
that week, and a card left once that week isn't left again. No model is
called: every line is code's.

A known residual, as in payment checks and quote follow-up: an order's row
reaches the reviewer's notice when its card's check doesn't run that run
(the day's cap, a card already waiting), even if ERPNext would keep the
order from them; only a check ERPNext refuses for their permissions takes
the row off.
"""
from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

from openexecutive.watch.registry import (
    CardSpec,
    DraftSpec,
    Prior,
    ReadArguments,
    Settings,
    WatchJob,
    WatchRead,
)

JOB_ID = "late-deliveries"
DOCTYPE = "ToDo"
ORDER = "Purchase Order"
REPORT = "Supplier Delivery Record"
ORDER_FIELDS = [
    "name", "supplier", "status", "transaction_date", "schedule_date", "per_received", "grand_total",
    "currency", "items.item_code", "items.qty", "items.received_qty", "items.schedule_date",
]
# What read 1 asks for: the shape's fields less the order's own
# schedule_date, so a line's date can't be mistaken for it.
CALL_FIELDS = [f for f in ORDER_FIELDS if f != "schedule_date"]
# Its reads' entries in oe_connector_watch_jobs, as oe_connector lists them
# (tests/unit/watch_job_vectors.json pins them).
# The open statuses read 1 reads, one fixed status a read (1a, 1b).
TO_RECEIVE_AND_BILL, TO_RECEIVE = "To Receive and Bill", "To Receive"


def _orders_shape(status: str) -> Mapping[str, Any]:
    return {
        "doctype": ORDER,
        "fields": ORDER_FIELDS,
        "filters": {
            "company": "$company", "docstatus": 1, "status": status, "per_received": ["<", 100],
            "schedule_date": ["<", "$date"],
        },
        "limit": "$int",
    }


ORDERS_SHAPE = _orders_shape(TO_RECEIVE_AND_BILL)
TO_RECEIVE_SHAPE = _orders_shape(TO_RECEIVE)
RECORD_SHAPE: Mapping[str, Any] = {
    "report_name": REPORT,
    "filters": {"company": "$company", "from_date": "$date", "to_date": "$date"},
}
READ_LIMIT = 500
# The supplier's record covers this many days back (12 months).
RECORD_DAYS = 365
NOTICE_MAX = 10
# More days late than this: the ToDo is High.
HIGH_AFTER_DAYS = 7
# The most ToDo cards one run proposes (the runner caps them by the day).
DRAFTS_MAX = 50
# What a notice row lists of an order's late lines.
LINES_SHOWN = 3
# Orders that aren't waiting on a delivery, whatever their lines say.
SHUT_STATUSES = frozenset({"Closed", "On Hold", "Completed"})
DESCRIPTION_MAX = 1000
TEXT_MAX = 140
# How ``ranked`` places an order held last week without a card: as carded
# after every real card.
HELD_RANK = 2**62
# A ToDo's description, code's template only (never a model's).
TEMPLATE = (
    "Chase {supplier} on {order}: {lines} due {due}, {missing} missing. Ask for a confirmed delivery date "
    "and update the order's schedule date. Their record: {record}."
)
RECORD_LATE = "late on {late} of {lines} lines in 12 months, {average} days on average"
RECORD_ON_TIME = "on time on all {lines} lines in 12 months"
RECORD_NONE = "no receipts in 12 months"
RECORD_UNREAD = "not read this run"
_RECORD_RE = (
    r"late on \d{1,7} of \d{1,7} lines in 12 months, \d{1,5}(?:\.\d)? days on average"
    r"|on time on all \d{1,7} lines in 12 months|no receipts in 12 months|not read this run"
)
_QTY_RE = r"\d{1,10}(?:\.\d{1,3})?"
_TEMPLATE_RE = re.compile(
    r"Chase (?P<supplier>.{1,140}) on (?P<order>\S{1,140}): (?:1 line|[1-9]\d{0,3} lines) "
    r"due \d{4}-\d{2}-\d{2}, " + _QTY_RE + r" of (?P<item>.{1,140})"
    r"(?: and (?:1 more line|[1-9]\d{0,3} more lines))? missing\. Ask for a confirmed delivery date and update "
    r"the order's schedule date\. Their record: (?:" + _RECORD_RE + r")\."
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
KEPT_LINE = (
    "The ERP didn't let the job's reviewer prepare its task, so it is told here: chase it in the ERP, "
    "or give the reviewer access to the purchase order."
)
ORDERS_CUT_LINE = (
    "Not every order line could be read this run, so some late orders may be missing or shown short; "
    "no tasks were prepared for orders from the list that was cut."
)
RECORD_CUT_LINE = "Not every supplier's delivery record could be read this run; those are shown as not read."
MORE_LINE = "{n} more late orders; the first {shown} are shown."
UNTASKED_LINE = (
    "Some orders shown couldn't get a task: their name or an item's doesn't fit the task's text. "
    "Chase them in the ERP."
)


# --------------------------------------------------------------------------- #
# Reading the results
# --------------------------------------------------------------------------- #


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _count(value: object) -> int | None:
    """A whole, non-negative count from a report's cell, or None."""
    found = _amount(value)
    if found is None or found < 0 or found != int(found) or found > 9_999_999:
        return None
    return int(found)


def _day(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _name(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= TEXT_MAX else ""


def _rows(result: Mapping[str, Any], *, required: bool) -> list[Mapping[str, Any]]:
    """A result's rows. A skipped read is {} and none."""
    if not result and not required:
        return []
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ValueError("no rows")
    return [r for r in rows if isinstance(r, Mapping)]


def _line_value(row: Mapping[str, Any], field: str) -> Any:
    """A line's field, under its dotted key or ERPNext's own: a child field
    comes back by its bare name. Read 1 never asks for the order's own
    ``schedule_date``, so a bare one is the line's."""
    dotted = f"items.{field}"
    return row[dotted] if dotted in row else row.get(field)


@dataclass(frozen=True)
class Line:
    item: str
    qty: float
    received: float
    due: date

    @property
    def missing(self) -> float:
        return round(self.qty - self.received, 3)


@dataclass
class Order:
    name: str
    supplier: str
    status: str
    grand_total: float
    currency: str
    lines: list[Line]


def orders(result: Mapping[str, Any]) -> list[Order]:
    """Read 1's orders, each with its lines (a row per order and line), in
    the order they first appear."""
    found: dict[str, Order] = {}
    for row in _rows(result, required=True):
        name = _name(row.get("name"))
        supplier = _name(row.get("supplier"))
        if not name or not supplier:
            continue
        order = found.get(name)
        if order is None:
            # ERPNext's text capped (_name) before anything cleans it.
            order = found[name] = Order(
                name=name, supplier=supplier, status=_name(row.get("status")),
                grand_total=round(_amount(row.get("grand_total")) or 0.0, 2),
                currency=_name(row.get("currency")), lines=[],
            )
        qty, received = _amount(_line_value(row, "qty")), _amount(_line_value(row, "received_qty"))
        due = _day(_line_value(row, "schedule_date"))
        if qty is None or received is None or due is None:
            continue
        order.lines.append(Line(item=_name(_line_value(row, "item_code")), qty=qty, received=received, due=due))
    return list(found.values())


@dataclass(frozen=True)
class Late:
    order: Order
    lines: tuple[Line, ...]
    # Days since the oldest late line was due.
    days: int
    week: str

    @property
    def due(self) -> date:
        return min(line.due for line in self.lines)

    @property
    def key(self) -> str:
        return f"po:{self.order.name}:{self.week}"


def week_of(day: date) -> str:
    """An ISO week, as the finding key holds it (2026-W46)."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def previous_week(week: str) -> str:
    """The ISO week before ``week`` (2026-W46 -> 2026-W45)."""
    year, number = week.split("-W")
    return week_of(date.fromisocalendar(int(year), int(number), 1) - timedelta(days=7))


def candidates(result: Mapping[str, Any], settings: Settings, today: date) -> list[Late]:
    """Read 1's orders that count today, in the job's order."""
    skipped = set(settings.get("skip_suppliers") or [])
    floor = int(settings["min_days_late"])
    week = week_of(today)
    out: list[Late] = []
    for order in orders(result):
        if order.status in SHUT_STATUSES or order.supplier in skipped:
            continue
        late = tuple(line for line in order.lines if line.missing > 0 and line.due < today)
        if not late:
            continue
        days = (today - min(line.due for line in late)).days
        if days >= floor:
            out.append(Late(order, late, days, week))
    return sorted(out, key=lambda f: (-f.days, -f.order.grand_total, f.order.name))


def ranked(found: Sequence[Late], carded: Mapping[str, int]) -> list[Late]:
    """``found`` in the order rows and cards are given: an order no earlier
    week's card was left for first, then the one carded longest ago
    (``carded``: each order's latest such card, by the ledger's id, which
    only grows), then as ``candidates`` sorts (days late, value, name)."""
    return sorted(found, key=lambda f: (f.order.name in carded, carded.get(f.order.name, 0)))


# --------------------------------------------------------------------------- #
# The reads' arguments
# --------------------------------------------------------------------------- #


def _orders_of(status: str) -> Any:
    def arguments(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
        return {
            "doctype": ORDER,
            "fields": list(CALL_FIELDS),
            "filters": {
                "company": settings["company"],
                "docstatus": 1,
                "status": status,
                "per_received": ["<", 100],
                "schedule_date": ["<", today.isoformat()],
            },
            "limit": READ_LIMIT,
        }

    return arguments


_orders = _orders_of(TO_RECEIVE_AND_BILL)
_to_receive = _orders_of(TO_RECEIVE)


def merged(first: Mapping[str, Any], second: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """Reads 1a and 1b as one result, and the orders read from a list that
    was cut (their lines may be short). An order in both (its status changed
    between the calls) keeps 1a's rows only, so no line counts twice."""
    rows_a, rows_b = _rows(first, required=True), _rows(second, required=True)
    seen = {_name(r.get("name")) for r in rows_a}
    rows = [*rows_a, *(r for r in rows_b if _name(r.get("name")) not in seen)]
    cut: set[str] = set()
    for result, part in ((first, rows_a), (second, rows_b)):
        if result.get("truncated") is True:
            cut |= {_name(r.get("name")) for r in part}
    cut.discard("")
    truncated = first.get("truncated") is True or second.get("truncated") is True
    return {"rows": rows, "truncated": truncated}, cut


def _record(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    if len(prior) < 2 or not candidates(merged(prior[0], prior[1])[0], settings, today):
        return None
    return {
        "report_name": REPORT,
        "filters": {
            "company": settings["company"],
            "from_date": (today - timedelta(days=RECORD_DAYS)).isoformat(),
            "to_date": today.isoformat(),
        },
    }


def records(result: Mapping[str, Any]) -> dict[str, str]:
    """Each supplier's record in code's words, from read 2's rows. A row
    whose counts don't read is left out."""
    out: dict[str, str] = {}
    for row in _rows(result, required=False):
        supplier = _name(row.get("supplier"))
        lines, late = _count(row.get("lines_received")), _count(row.get("lines_late"))
        average = _amount(row.get("average_days_late"))
        if not supplier or lines is None or late is None or average is None or average < 0 or late > lines:
            continue
        if not lines:
            out[supplier] = RECORD_NONE
        elif not late:
            out[supplier] = RECORD_ON_TIME.format(lines=lines)
        else:
            out[supplier] = RECORD_LATE.format(late=late, lines=lines, average=_number(min(average, 99_999.0), 1))
    return out


# --------------------------------------------------------------------------- #
# The findings
# --------------------------------------------------------------------------- #


def _number(value: float, places: int) -> str:
    """A quantity as a ToDo names it: no grouping, at most ``places``
    decimals, no trailing zeros."""
    text = f"{round(value, places):.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f}{f' {currency}' if currency else ''}"


def settled(text: str) -> str:
    """``text`` cleaned as a notice cleans it, again until cleaning changes
    nothing, so a stored ToDo can be held to it ("" when it never settles)."""
    from openexecutive.watch.jobs.quote_follow_up import settled as settle

    return settle(text)


def nameable(name: str) -> bool:
    """Whether an order's name can stand in a ToDo's description as is:
    ERPNext's name pattern, no whitespace, and its own clean."""
    from openexecutive.watch.jobs.quote_follow_up import nameable as fits

    return fits(name)


def _supplier_text(late: Late) -> str:
    return settled(late.order.supplier) or "the supplier"


def _lines_text(n: int) -> str:
    return f"{n} line{'s' if n != 1 else ''}"


def _first(late: Late) -> Line:
    """The line a ToDo names: the oldest late, then the most missing."""
    return sorted(late.lines, key=lambda line: (line.due, -line.missing, line.item))[0]


def description(late: Late, record: str) -> str:
    """The ToDo's description: code's template around the order's own
    figures and the supplier's and item's cleaned names, at most 1,000
    characters."""
    first = _first(late)
    more = len(late.lines) - 1
    missing = f"{_number(first.missing, 3)} of {settled(first.item) or 'an item'}"
    if more:
        missing += f" and {more} more line{'s' if more != 1 else ''}"
    text = TEMPLATE.format(
        supplier=_supplier_text(late), order=late.order.name, lines=_lines_text(len(late.lines)),
        due=late.due.isoformat(), missing=missing, record=record,
    )
    return " ".join(_CONTROL.sub(" ", text).split())[:DESCRIPTION_MAX]


def _missing_shown(late: Late) -> str:
    """What a notice row says is missing: the first few lines, then a count."""
    ordered = sorted(late.lines, key=lambda line: (line.due, -line.missing, line.item))
    shown = [f"{_number(line.missing, 3)} of {line.item or 'an item'}" for line in ordered[:LINES_SHOWN]]
    rest = len(ordered) - LINES_SHOWN
    return "; ".join(shown) + (f"; and {rest} more lines" if rest > 0 else "")


def fits_template(text: object, reference: str) -> bool:
    """Whether ``text`` is the job's template naming the order
    ``reference``, as check_fields holds a stored card to it: the order's
    own name in its place, the supplier and item their own clean, one line,
    no control characters, at most 1,000. The builder holds its own text to
    it too, so a card it leaves is one check_fields takes."""
    if not isinstance(text, str):
        return False
    found = _TEMPLATE_RE.fullmatch(text)
    return not (
        found is None or found.group("order") != reference or len(text) > DESCRIPTION_MAX
        or _CONTROL.search(text) or text != " ".join(text.split())
        or found.group("supplier") != settled(found.group("supplier"))
        or found.group("item") != settled(found.group("item")) or not nameable(reference)
    )


def _row(late: Late, record: str, notes: tuple[str, ...] = ()) -> CardSpec:
    """An order as the notice shows it: one of its rows, or a notice of its
    own (``notes``)."""
    order = late.order
    return CardSpec(
        key=late.key,
        summary=order.supplier,
        values=(
            ("Order", order.name),
            ("Late", f"{late.days} day{'s' if late.days != 1 else ''}"),
            ("Due", late.due.isoformat()),
            ("Missing", _missing_shown(late)),
            ("Value", _money(order.grand_total, order.currency)),
            ("Status", order.status),
            ("Supplier's record", record),
        ),
        notes=notes,
    )


def build(
    reads: Sequence[Mapping[str, Any]], settings: Settings, today: date, *,
    told: Collection[str] = frozenset(), assignee: str = "", on_card: Collection[str] = frozenset(),
    carded: Mapping[str, int] | None = None,
) -> list[CardSpec | DraftSpec]:
    """The "Late deliveries" notice and a ToDo per late order, figures from
    the reads only. ``told``: the finding keys a notice told before (left
    out of this one); ``assignee``: who the ToDos are for (``assign_to``,
    else the reviewer's ERPNext user); ``on_card``: orders an earlier week's
    card still waits for (no second card); ``carded``: each order's latest
    earlier week's card (``ranked``)."""
    first, second, record_result = reads
    orders_result, cut = merged(first, second)
    found = ranked(candidates(orders_result, settings, today), carded or {})
    if not found:
        return []
    known = records(record_result)
    record_cut = record_result.get("truncated") is True
    orders_cut = orders_result.get("truncated") is True

    def record(late: Late) -> str:
        said = known.get(late.order.supplier)
        if said is not None:
            return said
        return RECORD_UNREAD if record_cut else RECORD_NONE

    drafts: list[DraftSpec] = []
    untasked: set[str] = set()
    for late in found:
        name = late.order.name
        # An order from a cut list may have lines short: no chase note then.
        if name in on_card or name in cut:
            continue
        text = description(late, record(late))
        if not fits_template(text, name):
            # A name, an item or a quantity the template can't hold as is
            # (the template's own words in an item code): on the notice only.
            untasked.add(name)
            continue
        drafts.append(DraftSpec(
            key=late.key,
            operation="create",
            doctype=DOCTYPE,
            fields={
                "description": text,
                "allocated_to": assignee,
                "priority": "High" if late.days > HIGH_AFTER_DAYS else "Medium",
                "date": today.isoformat(),
                "reference_type": ORDER,
                "reference_name": name,
            },
            expect={"reference": f"{ORDER} {name}", "description": text},
            title=f"Chase {name}: {_supplier_text(late)}",
            group=late.order.supplier,
            # Told on its own only when ERPNext keeps the order from the
            # reviewer (the runner strips its row from the reviewer's notice
            # and sends this to the principal).
            notice=_row(late, record(late), notes=(KEPT_LINE,)),
        ))
    new = [f for f in found if f.key not in told]
    shown = new[:NOTICE_MAX]
    notes: list[str] = []
    if len(new) > NOTICE_MAX:
        notes.append(MORE_LINE.format(n=len(new) - NOTICE_MAX, shown=NOTICE_MAX))
    if orders_cut:
        notes.append(ORDERS_CUT_LINE)
    if record_cut and any(record(f) == RECORD_UNREAD for f in new):
        notes.append(RECORD_CUT_LINE)
    if untasked & {f.order.name for f in shown}:
        notes.append(UNTASKED_LINE)
    out: list[CardSpec | DraftSpec] = []
    if shown:
        out.append(CardSpec(
            key=f"late:{settings['company']}:{today.isoformat()}",
            summary="Late deliveries",
            items=tuple(_row(f, record(f)) for f in shown),
            notes=tuple(notes),
            count=len(new),
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


def card_history(found: Sequence[Late]) -> tuple[set[str], dict[str, int]]:
    """From the ledger's listing of the job's cards (bounded by
    ``job_cards``' own cap), for the orders among ``found``: those an
    earlier week's card still waits for (live, or being approved: this
    week's finding gets no second card beside it), and each order's latest
    earlier week's card id, whatever became of it (``ranked``). An order
    held last week without a card (its check refused for good: a ToDo
    already open, or kept from the reviewer) ranks as carded most
    recently, so it doesn't take the day's first checks again. This week's
    own card or hold is the card store's to keep."""
    from openexecutive.integrations import erpnext_drafts as drafts
    from openexecutive.memory.decision_ledger import STATUS_EXECUTING, STATUS_PROPOSED
    from openexecutive.watch.cards import carded_key
    from openexecutive.watch.runner import dedup_key
    from openexecutive.watch.settings import told

    this_week = {f.order.name: dedup_key(JOB_ID, f.key) for f in found}
    waiting: set[str] = set()
    carded: dict[str, int] = {}
    for card in drafts.job_cards(JOB_ID, open_only=False):
        payload = drafts._payload(card)
        fields = payload.get("fields")
        if not isinstance(fields, Mapping) or fields.get("reference_type") != ORDER:
            continue
        name = fields.get("reference_name")
        if not isinstance(name, str) or name not in this_week or payload.get("dedup_key") == this_week[name]:
            continue
        if isinstance(card.id, int) and card.id > carded.get(name, 0):
            carded[name] = card.id
        if card.status == STATUS_EXECUTING or (card.status == STATUS_PROPOSED and drafts.card_live(card)):
            waiting.add(name)
    held_last_week = {
        carded_key(dedup_key(JOB_ID, f"po:{f.order.name}:{previous_week(f.week)}")): f.order.name
        for f in found if f.order.name not in carded
    }
    if held_last_week:
        for record in told(JOB_ID, list(held_last_week)):
            carded[held_last_week[record]] = HELD_RANK
    return waiting, carded


def on_earlier_card(found: Sequence[Late]) -> set[str]:
    """The orders among ``found`` an earlier week's card still waits for."""
    return card_history(found)[0]


def _build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[CardSpec | DraftSpec]:
    """``build`` with what this install knows: the findings told before, who
    the ToDos are for, and the orders' earlier cards (``card_history``)."""
    from openexecutive.watch.runner import told

    found = candidates(merged(reads[0], reads[1])[0], settings, today)
    if not found:
        return []
    waiting, carded = card_history(found)
    return build(
        reads, settings, today, told=told(JOB, [f.key for f in found]), assignee=assignee(settings),
        on_card=waiting, carded=carded,
    )


def check_fields(operation: str, doctype: str, fields: Mapping[str, Any], name: str = "") -> dict[str, Any] | str:
    """A late-deliveries card's fields, as they are checked, stored and sent:
    a new ToDo on a Purchase Order, assigned to the job's assignee now, High
    or Medium, its description exactly the job's template naming that order,
    and exactly ``erpnext_drafts._todo``'s output. Run before each check and
    again at approval."""
    from openexecutive.integrations.erpnext_changes import _DOC_RE
    from openexecutive.integrations.erpnext_drafts import _TODO_FIELDS, _todo

    if operation != "create" or doctype != DOCTYPE or name:
        return "The late-deliveries job leaves ToDo drafts only."
    if not isinstance(fields, Mapping) or set(fields) != _TODO_FIELDS:
        return "A late-deliveries card's fields are a ToDo's six, all of them."
    reference = fields.get("reference_name")
    if fields.get("reference_type") != ORDER or not isinstance(reference, str) or not _DOC_RE.fullmatch(reference):
        return "A late-deliveries card's ToDo is on a Purchase Order."
    if fields.get("priority") not in ("High", "Medium"):
        return "A late-deliveries card's priority is High or Medium."
    if not fits_template(fields.get("description"), reference):
        return "A late-deliveries card's description is the job's own: Chase <supplier> on <order> ..."
    expected = assignee()
    if not expected or fields.get("allocated_to") != expected:
        return "A late-deliveries card's ToDo is for the job's assignee: its assign_to, else its reviewer."
    try:
        out = _todo(dict(fields))
    except ValueError as exc:
        return str(exc)
    if out != dict(fields):
        return "A late-deliveries card's fields must be as the job built them."
    return out


JOB: WatchJob = WatchJob(
    id=JOB_ID,
    title="Late deliveries",
    reads=(
        WatchRead("list_records", ORDERS_SHAPE, _orders, "the late purchase orders to receive and bill"),
        WatchRead("list_records", TO_RECEIVE_SHAPE, _to_receive, "the late purchase orders to receive"),
        WatchRead("run_report", RECORD_SHAPE, _record, "the suppliers' delivery record"),
    ),
    build=_build,
    # Each weekday morning, between collections and quote follow-up.
    schedule=time(7, 15),
    weekdays=frozenset(range(5)),
    card_cap_day=5,
    default_settings={"company": "", "min_days_late": 2, "assign_to": "", "skip_suppliers": []},
    check_fields=check_fields,
    setting_bounds={"min_days_late": (1, 60)},
    card_noun="chase tasks",
    once=True,
)

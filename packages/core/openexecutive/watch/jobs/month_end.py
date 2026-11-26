"""The month-end close job (phase 2, A14): last month's close checklist,
read from the books, with a ToDo for whoever owns each open item.

**When** (``runs_on``, company time, weekdays at 07:30): the first working
day of the month, then each working day until ``close_days`` working days
have passed (default 5), then every fifth working day, until a run finds
every item clear. It closes the month before the run's own. A working day
is Monday to Friday; ERPNext's holiday list isn't read.

**Reads** (oe_connector's listing ``month-end``, as the job's own ERP user;
every month-end bound is ``$month_end``, the month's last day; each read
``may_fail``, so one refused or slow read leaves its item "not read" and
the run goes on):

1. supplier invoices in draft up to the month's end, newest first;
2. sales invoices in draft, the same;
3. Received Items To Be Billed, as at the month's end;
4. Delivered Items To Be Billed, the same;
5. Repost Item Valuation not Completed, posted by the month's end;
6. Asset Depreciation Schedule lines due by the month's end with no
   Journal Entry, newest first;
7. the company's live non-group accounts with a currency set (8a);
8. Exchange Rate Revaluations dated exactly the month's end (8b), only
   when an account is in another currency;
9. Items with deferred revenue (9a), then with deferred expense (9b) only
   when none has deferred revenue;
10. Process Deferred Accounting ending on or after the month's end (9c),
    only when an Item defers;
11. an Accounting Period covering the month (10a), and the Company's
    ``accounts_frozen_till_date`` (10b);
12. the company's live bank accounts (R11);
13. General Ledger's closing balance, one read per account in another
    currency (R8c, at most 10; only the closing row's balance is kept,
    never a voucher, party or remark);
14. Bank Reconciliation Statement, one read per bank account (R5, at
    most 10).

oe_connector's listing holds each account of 13 and 14 as a literal: one
it doesn't list is refused, and the item names it as not checked.

**Rules** (code, unit-tested on fixed reads; no model is called): each of
nine items is clear, open (its count, value and oldest date) or not read
(a read refused, cut or unreadable, never said as clear):

- **Supplier and sales invoices in draft** dated in the month.
- **Received and delivered, not billed:** a line pending billing dated on
  or before the month's end less ``unbilled_grace_days``.
- **Bank not reconciled:** an uncleared entry on a bank account's
  statement dated on or before the month's end less ``bank_grace_days``;
  a bank account not read is named.
- **Stock valuation:** a repost Queued, In Progress or Failed.
- **Depreciation:** a schedule line due by the month's end not posted.
- **Revaluation:** an account in another currency with a balance at the
  month's end and no revaluation dated that day; clear with no such
  account or no such balance. An account whose balance wasn't read is
  named.
- **Deferred postings:** an Item defers and no Process Deferred
  Accounting ends on or after the month's end.

The period's lock (an Accounting Period, or the company's frozen-till
date) is reported, never required, and gets no task.

**What it leaves:** one notice for the reviewer, kept current
(``updates_notice``): "Close of October: 6 of 9 clear", a row per item
with its state, figures and owner, and once all are clear, "All clear.
Lock the period?" It never locks the period. A ToDo draft card per open
item, for the reviewer to approve, assigned to the item's owner
(``assign_to_<item>``, else the reviewer), its text code's template
naming only the month, counts, sums and dates; once per item and month
(``once``, key ``close:<company>:<YYYY-MM>:<item>``), so an item that
clears and reopens gets no second card that month.
"""
from __future__ import annotations

import calendar
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any

from openexecutive.watch.registry import (
    CardSpec,
    DraftSpec,
    Prior,
    ReadArguments,
    Repeated,
    Settings,
    WatchJob,
    WatchRead,
)

JOB_ID = "month-end"
DOCTYPE = "ToDo"
READ_LIMIT = 500
# The most accounts read one by one (R8c, R5), as bank matching reads its own.
MAX_ACCOUNTS = 10
# Every fifth working day after the close window: about once a week.
WEEKLY_EVERY = 5
TEXT_MAX = 140
DESCRIPTION_MAX = 1000
MONTHS = tuple(calendar.month_name[1:])
# The General Ledger row that holds an account's balance at the month's end.
CLOSING_ROW = "'Closing (Opening + Total)'"
# A Repost Item Valuation still to settle.
OPEN_REPOSTS = frozenset({"Queued", "In Progress", "Failed"})
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

# --------------------------------------------------------------------------- #
# The items
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Item:
    key: str
    title: str
    # A ToDo's "what": "<count> <noun>" (the count's own plural).
    noun: str
    nouns: str
    # Where its owner looks, code's words.
    where: str


ITEMS: tuple[Item, ...] = (
    Item("purchase_drafts", "Supplier invoices in draft", "supplier invoice in draft dated in the month",
         "supplier invoices in draft dated in the month",
         "Submit or cancel them in Purchase Invoice (filter Draft, posting date in the month)."),
    Item("sales_drafts", "Sales invoices in draft", "sales invoice in draft dated in the month",
         "sales invoices in draft dated in the month",
         "Submit or cancel them in Sales Invoice (filter Draft, posting date in the month)."),
    Item("received", "Received, not billed", "received line not billed", "received lines not billed",
         "Bill them or close them from the report Received Items To Be Billed, as at the month's end."),
    Item("delivered", "Delivered, not billed", "delivered line not billed", "delivered lines not billed",
         "Bill them or close them from the report Delivered Items To Be Billed, as at the month's end."),
    Item("bank", "Bank not reconciled", "uncleared bank entry", "uncleared bank entries",
         "Reconcile them in Bank Reconciliation Tool, to the month's end."),
    Item("stock", "Stock valuation not settled", "stock repost not completed", "stock reposts not completed",
         "Check them in Repost Item Valuation, and retry the failed ones."),
    Item("depreciation", "Depreciation not posted", "depreciation line not posted",
         "depreciation lines not posted", "Post them from each asset's Depreciation Schedule."),
    Item("revaluation", "Revaluation not done", "account in another currency with a balance",
         "accounts in another currency with a balance",
         "Make an Exchange Rate Revaluation dated the month's last day."),
    Item("deferred", "Deferred postings not done", "deferral to post", "deferrals to post",
         "Run Process Deferred Accounting to the month's end."),
)
BY_KEY: Mapping[str, Item] = {item.key: item for item in ITEMS}
REQUIRED = len(ITEMS)
LOCK_TITLE = "Period locked"

CLEAR, OPEN, UNREAD = "clear", "open", "not read"
CUT_NOTE = "More rows than one read takes: not all could be read this run."


@dataclass
class Check:
    item: Item
    state: str
    count: int = 0
    # How many at least (a list cut before it ended).
    at_least: bool = False
    # A sum per currency ("" when the read gives none).
    sums: dict[str, float] = field(default_factory=dict)
    oldest: date | None = None
    # Code's words on why it's clear or not read.
    note: str = ""
    # Accounts this run didn't read (ERPNext's names, shown in the notice's
    # untrusted block only).
    not_checked: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# The month
# --------------------------------------------------------------------------- #


def period(today: date) -> tuple[date, date]:
    """The month the run closes: the one before ``today``'s, first and last day."""
    last = today.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def month_key(today: date) -> str:
    return period(today)[1].strftime("%Y-%m")


def month_name(today: date) -> str:
    return MONTHS[period(today)[1].month - 1]


def working_day(day: date) -> int:
    """``day``'s working day of its month (Monday to Friday), from 1; 0 on a
    weekend."""
    if day.weekday() >= 5:
        return 0
    return sum(1 for n in range(1, day.day + 1) if day.replace(day=n).weekday() < 5)


def _notice_key(company: str, today: date) -> str:
    return f"close:{company}:{month_key(today)}"


def _item_key(company: str, today: date, item: str) -> str:
    return f"close:{company}:{month_key(today)}:{item}"


ALL_CLEAR = "Close of {month}: all clear"
SOME_CLEAR = "Close of {month}: {clear} of {required} clear"
SUBJECTS = (
    r"Close of (?:" + "|".join(MONTHS) + r"): (?:all clear|\d{1,2} of \d{1,2} clear)"
)


def runs_on(settings: Settings, day: date) -> bool:
    """The month's first working day, each working day up to ``close_days``,
    then every fifth, until this month's run said all clear."""
    company = settings.get("company")
    if not isinstance(company, str) or not company:
        return False
    n = working_day(day)
    close_days = int(settings["close_days"])
    if not n or not (n <= close_days or (n - close_days) % WEEKLY_EVERY == 0):
        return False
    return not said_all_clear(company, day)


def said_all_clear(company: str, day: date) -> bool:
    """Whether this month's latest notice says all clear: its subject is
    code's own (``ALL_CLEAR``), read from this install's alerts only."""
    from openexecutive.alerts import store
    from openexecutive.watch.runner import SOURCE, dedup_key

    if not store._resolve_db_path(None).exists():
        return False
    with store._get_conn() as conn:
        row = conn.execute(
            "SELECT headline FROM alerts WHERE source = ? AND dedup_key = ? ORDER BY id DESC LIMIT 1",
            (SOURCE, f"private:{dedup_key(JOB_ID, _notice_key(company, day))}"),
        ).fetchone()
    return row is not None and row["headline"] == ALL_CLEAR.format(month=month_name(day))


# --------------------------------------------------------------------------- #
# The reads (oe_connector's listing, tests/unit/watch_job_vectors.json)
# --------------------------------------------------------------------------- #

PI_FIELDS = ["name", "supplier", "posting_date", "grand_total", "currency"]
SI_FIELDS = ["name", "customer", "posting_date", "grand_total", "currency"]


def _drafts_shape(doctype: str, fields: list[str]) -> Mapping[str, Any]:
    return {
        "doctype": doctype, "fields": fields,
        "filters": {"company": "$company", "docstatus": 0, "posting_date": ["<=", "$month_end"]},
        "order_by": "posting_date desc", "limit": "$int",
    }


def _billed_shape(report: str) -> Mapping[str, Any]:
    return {"report_name": report, "filters": {"company": "$company", "posting_date": "$month_end"}}


PI_SHAPE = _drafts_shape("Purchase Invoice", PI_FIELDS)
SI_SHAPE = _drafts_shape("Sales Invoice", SI_FIELDS)
RECEIVED_REPORT, DELIVERED_REPORT = "Received Items To Be Billed", "Delivered Items To Be Billed"
RECEIVED_SHAPE, DELIVERED_SHAPE = _billed_shape(RECEIVED_REPORT), _billed_shape(DELIVERED_REPORT)
REPOST_SHAPE: Mapping[str, Any] = {
    "doctype": "Repost Item Valuation", "fields": ["name", "status", "posting_date", "voucher_type", "voucher_no"],
    "filters": {"company": "$company", "docstatus": 1, "status": ["!=", "Completed"],
                "posting_date": ["<=", "$month_end"]},
    "limit": "$int",
}
DEPRECIATION_SHAPE: Mapping[str, Any] = {
    "doctype": "Asset Depreciation Schedule",
    "fields": ["name", "asset", "depreciation_schedule.schedule_date", "depreciation_schedule.journal_entry",
               "depreciation_schedule.depreciation_amount"],
    "filters": {"company": "$company", "docstatus": 1, "depreciation_schedule.schedule_date": ["<=", "$month_end"],
                "depreciation_schedule.journal_entry": ["is", "not set"]},
    "order_by": "depreciation_schedule.schedule_date desc", "limit": "$int",
}
ACCOUNTS_SHAPE: Mapping[str, Any] = {
    "doctype": "Account", "fields": ["name", "account_currency", "account_type"],
    "filters": {"company": "$company", "is_group": 0, "disabled": 0, "account_currency": ["!=", ""]},
    "limit": "$int",
}
REVALUATION_SHAPE: Mapping[str, Any] = {
    "doctype": "Exchange Rate Revaluation", "fields": ["name", "posting_date"],
    "filters": {"company": "$company", "docstatus": 1, "posting_date": "$month_end"}, "limit": "$int",
}


def _deferring_shape(flag: str) -> Mapping[str, Any]:
    return {"doctype": "Item", "fields": ["name"], "filters": {flag: 1, "disabled": 0}, "limit": "$int"}


DEFERRED_REVENUE_SHAPE = _deferring_shape("enable_deferred_revenue")
DEFERRED_EXPENSE_SHAPE = _deferring_shape("enable_deferred_expense")
DEFERRED_RUN_SHAPE: Mapping[str, Any] = {
    "doctype": "Process Deferred Accounting", "fields": ["name", "type", "start_date", "end_date"],
    "filters": {"company": "$company", "docstatus": 1, "end_date": [">=", "$month_end"]}, "limit": "$int",
}
PERIOD_SHAPE: Mapping[str, Any] = {
    "doctype": "Accounting Period", "fields": ["name", "start_date", "end_date"],
    "filters": {"company": "$company", "start_date": ["<=", "$date"], "end_date": [">=", "$month_end"]},
    "limit": "$int",
}
COMPANY_SHAPE: Mapping[str, Any] = {
    "doctype": "Company", "fields": ["name", "accounts_frozen_till_date"], "filters": {"name": "$company"},
    "limit": "$int",
}
BANKS_SHAPE: Mapping[str, Any] = {
    "doctype": "Account", "fields": ["name"],
    "filters": {"company": "$company", "is_group": 0, "disabled": 0, "account_type": "Bank"}, "limit": "$int",
}
# One per account in another currency, the account a literal in the
# listing ($account: this side's own slot, a list of one).
BALANCE_SHAPE: Mapping[str, Any] = {
    "report_name": "General Ledger",
    "filters": {"company": "$company", "from_date": "$month_end", "to_date": "$month_end", "account": ["$account"],
                "categorize_by": "Categorize by Voucher (Consolidated)"},
}
# One per bank account, the account a literal in the listing.
STATEMENT_SHAPE: Mapping[str, Any] = {
    "report_name": "Bank Reconciliation Statement",
    "filters": {"company": "$company", "account": "$bank_account", "report_date": "$month_end"},
}

# Each read's place in ``reads`` (and so in ``build``'s results).
(R_PI, R_SI, R_RECEIVED, R_DELIVERED, R_REPOST, R_DEPRECIATION, R_ACCOUNTS, R_REVALUATION, R_REVENUE, R_EXPENSE,
 R_DEFERRED, R_PERIOD, R_COMPANY, R_BANKS, R_BALANCES, R_STATEMENTS) = range(16)


# The fields each read asks for, of those its listing allows: only what the
# rules use.
CALL_FIELDS: Mapping[str, list[str]] = {
    "Purchase Invoice": ["posting_date", "grand_total", "currency"],
    "Sales Invoice": ["posting_date", "grand_total", "currency"],
    "Repost Item Valuation": ["status", "posting_date"],
    "Asset Depreciation Schedule": [
        "depreciation_schedule.schedule_date", "depreciation_schedule.journal_entry",
        "depreciation_schedule.depreciation_amount",
    ],
    "Account": ["name", "account_currency"],
    "Process Deferred Accounting": ["name"],
}


def _list_args(shape: Mapping[str, Any], filters: dict[str, Any], limit: int = READ_LIMIT) -> dict[str, Any]:
    fields = list(shape["fields"])
    asked = [f for f in CALL_FIELDS.get(shape["doctype"], fields) if f in fields] or fields
    out: dict[str, Any] = {"doctype": shape["doctype"], "fields": asked, "filters": filters}
    if "order_by" in shape:
        out["order_by"] = shape["order_by"]
    out["limit"] = limit
    return out


def _ends(today: date) -> tuple[str, str]:
    first, last = period(today)
    return first.isoformat(), last.isoformat()


def _drafts(shape: Mapping[str, Any]) -> Any:
    def arguments(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
        return _list_args(shape, {
            "company": settings["company"], "docstatus": 0, "posting_date": ["<=", _ends(today)[1]],
        })

    return arguments


def _billed(report: str) -> Any:
    def arguments(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
        return {"report_name": report, "filters": {"company": settings["company"], "posting_date": _ends(today)[1]}}

    return arguments


def _reposts(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    return _list_args(REPOST_SHAPE, {
        "company": settings["company"], "docstatus": 1, "status": ["!=", "Completed"],
        "posting_date": ["<=", _ends(today)[1]],
    })


def _depreciation(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    return _list_args(DEPRECIATION_SHAPE, {
        "company": settings["company"], "docstatus": 1, "depreciation_schedule.schedule_date": ["<=", _ends(today)[1]],
        "depreciation_schedule.journal_entry": ["is", "not set"],
    })


def _accounts(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    return _list_args(ACCOUNTS_SHAPE, {
        "company": settings["company"], "is_group": 0, "disabled": 0, "account_currency": ["!=", ""],
    })


def _revaluations(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    if len(prior) <= R_ACCOUNTS or not foreign_accounts(prior[R_ACCOUNTS])[0]:
        return None
    return _list_args(REVALUATION_SHAPE, {
        "company": settings["company"], "docstatus": 1, "posting_date": _ends(today)[1],
    })


def _deferring_revenue(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    return _list_args(DEFERRED_REVENUE_SHAPE, {"enable_deferred_revenue": 1, "disabled": 0}, limit=1)


def _deferring_expense(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    if len(prior) > R_REVENUE and _has_rows(prior[R_REVENUE]):
        return None
    return _list_args(DEFERRED_EXPENSE_SHAPE, {"enable_deferred_expense": 1, "disabled": 0}, limit=1)


def _deferred_runs(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    if len(prior) <= R_EXPENSE or not (_has_rows(prior[R_REVENUE]) or _has_rows(prior[R_EXPENSE])):
        return None
    return _list_args(DEFERRED_RUN_SHAPE, {
        "company": settings["company"], "docstatus": 1, "end_date": [">=", _ends(today)[1]],
    })


def _period(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    first, last = _ends(today)
    return _list_args(PERIOD_SHAPE, {
        "company": settings["company"], "start_date": ["<=", first], "end_date": [">=", last],
    })


def _company(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    return _list_args(COMPANY_SHAPE, {"name": settings["company"]}, limit=1)


def _banks(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    return _list_args(BANKS_SHAPE, {
        "company": settings["company"], "is_group": 0, "disabled": 0, "account_type": "Bank",
    })


def _balances(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    if len(prior) <= R_ACCOUNTS:
        return None
    accounts = foreign_accounts(prior[R_ACCOUNTS])[0]
    if not accounts:
        return None
    last = _ends(today)[1]
    return Repeated(tuple(
        {"report_name": "General Ledger", "filters": {
            "company": settings["company"], "from_date": last, "to_date": last, "account": [account],
            "categorize_by": "Categorize by Voucher (Consolidated)",
        }} for account in accounts
    ), truncated=len(accounts) > MAX_ACCOUNTS)


def _statements(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    if len(prior) <= R_BANKS:
        return None
    accounts = bank_accounts(prior[R_BANKS])[0]
    if not accounts:
        return None
    last = _ends(today)[1]
    return Repeated(tuple(
        {"report_name": "Bank Reconciliation Statement", "filters": {
            "company": settings["company"], "account": account, "report_date": last,
        }} for account in accounts
    ), truncated=len(accounts) > MAX_ACCOUNTS)


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
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= TEXT_MAX else ""


def _unread(result: Mapping[str, Any]) -> bool:
    """A read that may fail and wasn't read: never "no rows"."""
    return result.get("unread") is True


def _rows(result: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """A result's rows, or None when it wasn't read or has none to read."""
    if not result or _unread(result):
        return None
    rows = result.get("rows")
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, Mapping)]


def _cut(result: Mapping[str, Any]) -> bool:
    return result.get("truncated") is True


def _has_rows(result: Mapping[str, Any]) -> bool:
    rows = _rows(result)
    return bool(rows)


def _currency(value: object) -> str:
    return value if isinstance(value, str) and _CURRENCY_RE.fullmatch(value) else ""


def foreign_accounts(result: Mapping[str, Any]) -> tuple[list[str], bool]:
    """Read 8a's accounts in a currency other than the company's (a null
    currency is the company's), in order, and whether they couldn't be
    told: not read, cut, or no company currency."""
    rows = _rows(result)
    home = _currency(result.get("currency")) if result else ""
    if rows is None or not home:
        return [], True
    names = [
        _name(r.get("name")) for r in rows
        if _currency(r.get("account_currency")) and _currency(r.get("account_currency")) != home
    ]
    return [n for n in dict.fromkeys(names) if n], _cut(result)


def bank_accounts(result: Mapping[str, Any]) -> tuple[list[str], bool]:
    """R11's bank accounts, in order, and whether the list couldn't be told."""
    rows = _rows(result)
    if rows is None:
        return [], True
    names = [_name(r.get("name")) for r in rows]
    return [n for n in dict.fromkeys(names) if n], _cut(result)


def closing_balance(result: Mapping[str, Any]) -> float | None:
    """An account's balance at the month's end: the number in General
    Ledger's closing row, and nothing else of the result (its voucher rows
    name parties and remarks). None when the row or the number isn't there."""
    rows = _rows(result)
    if rows is None:
        return None
    found = [r for r in rows if r.get("account") == CLOSING_ROW]
    if len(found) != 1:
        return None
    return _amount(found[0].get("balance"))


def _each(result: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
    """A repeated read's results, in call order, and whether its list was cut."""
    each = result.get("each") if result else None
    if not isinstance(each, list):
        return [], False
    return [e if isinstance(e, Mapping) else {"unread": True} for e in each], result.get("truncated") is True


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #


def _counted(
    item: Item, dated: list[tuple[date, float | None, str]], *, at_least: bool, cut: bool = False,
) -> Check:
    """An item from its dated findings (date, amount, currency). Nothing
    counted on a cut result is not read, never clear: the rows past the cut
    may hold one."""
    if not dated:
        return Check(item, UNREAD, note=CUT_NOTE) if cut else Check(item, CLEAR)
    sums: dict[str, float] = {}
    for _, amount, currency in dated:
        if amount is not None:
            sums[currency] = round(sums.get(currency, 0.0) + amount, 2)
    return Check(item, OPEN, count=len(dated), at_least=at_least, sums=sums, oldest=min(d for d, _a, _c in dated))


def check_drafts(item: Item, result: Mapping[str, Any], today: date) -> Check:
    rows = _rows(result)
    if rows is None:
        return Check(item, UNREAD)
    first, last = period(today)
    days = [(d, r) for r in rows if (d := _day(r.get("posting_date"))) is not None]
    dated = [(d, _amount(r.get("grand_total")), _currency(r.get("currency"))) for d, r in days if first <= d <= last]
    # Newest first: a list cut while still inside the month may have more.
    at_least = _cut(result) and (not days or min(d for d, _r in days) >= first)
    return _counted(item, dated, at_least=at_least)


def check_billed(item: Item, result: Mapping[str, Any], today: date, grace: int) -> Check:
    rows = _rows(result)
    if rows is None:
        return Check(item, UNREAD)
    cutoff = period(today)[1] - timedelta(days=grace)
    currency = _currency(result.get("currency"))
    dated: list[tuple[date, float | None, str]] = []
    for r in rows:
        d, pending = _day(r.get("date")), _amount(r.get("pending_amount"))
        if d is not None and pending is not None and pending > 0 and d <= cutoff:
            dated.append((d, pending, currency))
    return _counted(item, dated, at_least=_cut(result), cut=_cut(result))


def check_reposts(item: Item, result: Mapping[str, Any], today: date) -> Check:
    rows = _rows(result)
    if rows is None:
        return Check(item, UNREAD)
    last = period(today)[1]
    dated: list[tuple[date, float | None, str]] = [
        (d, None, "") for r in rows
        if r.get("status") in OPEN_REPOSTS and (d := _day(r.get("posting_date"))) is not None and d <= last
    ]
    return _counted(item, dated, at_least=_cut(result), cut=_cut(result))


def check_depreciation(item: Item, result: Mapping[str, Any], today: date) -> Check:
    rows = _rows(result)
    if rows is None:
        return Check(item, UNREAD)
    last = period(today)[1]

    def field_of(row: Mapping[str, Any], name: str) -> Any:
        dotted = f"depreciation_schedule.{name}"
        return row[dotted] if dotted in row else row.get(name)

    dated = [
        (d, _amount(field_of(r, "depreciation_amount")), "") for r in rows
        if not field_of(r, "journal_entry") and (d := _day(field_of(r, "schedule_date"))) is not None and d <= last
    ]
    return _counted(item, dated, at_least=_cut(result), cut=_cut(result))


def check_bank(item: Item, banks: Mapping[str, Any], statements: Mapping[str, Any], today: date,
               grace: int) -> Check:
    accounts, banks_cut = bank_accounts(banks)
    if _unread(banks) or _rows(banks) is None:
        return Check(item, UNREAD, note="The bank accounts couldn't be listed.")
    if not accounts and not banks_cut:
        return Check(item, CLEAR, note="No bank accounts.")
    each, cut = _each(statements)
    cutoff = period(today)[1] - timedelta(days=grace)
    dated: list[tuple[date, float | None, str]] = []
    unread: list[str] = []
    statement_cut = False
    for n, account in enumerate(accounts):
        result = each[n] if n < len(each) else {"unread": True}
        rows = _rows(result)
        if rows is None:
            unread.append(account)
            continue
        statement_cut = statement_cut or _cut(result)
        for r in rows:
            # A statement's summary rows have no posting date: never an entry.
            d = _day(r.get("posting_date"))
            if d is None or r.get("clearance_date") or d > cutoff:
                continue
            debit, credit = _amount(r.get("debit")) or 0.0, _amount(r.get("credit")) or 0.0
            dated.append((d, round(debit + credit, 2), _currency(r.get("account_currency"))))
    checked = _counted(item, dated, at_least=statement_cut)
    checked.not_checked = tuple(unread)
    if banks_cut or cut:
        checked.note = "Not every bank account could be read this run."
    elif statement_cut:
        checked.note = CUT_NOTE
    if checked.state == CLEAR and (unread or banks_cut or cut or statement_cut):
        checked.state = UNREAD
    return checked


def check_revaluation(item: Item, accounts: Mapping[str, Any], balances: Mapping[str, Any],
                      revaluations: Mapping[str, Any], today: date) -> Check:
    foreign, untold = foreign_accounts(accounts)
    if _unread(accounts) or _rows(accounts) is None or not _currency(accounts.get("currency")):
        return Check(item, UNREAD, note="The accounts in other currencies couldn't be listed.")
    if not foreign and not untold:
        return Check(item, CLEAR, note="No accounts in another currency.")
    each, cut = _each(balances)
    with_balance: list[str] = []
    unread: list[str] = []
    for n, account in enumerate(foreign):
        result = each[n] if n < len(each) else {"unread": True}
        balance = closing_balance(result)
        if balance is None:
            unread.append(account)
        elif round(balance, 2) != 0:
            with_balance.append(account)
    if with_balance:
        last = period(today)[1]
        rows = _rows(revaluations)
        if rows is None:
            return Check(item, UNREAD, note="The revaluations couldn't be read.", not_checked=tuple(unread))
        if any(_day(r.get("posting_date")) == last for r in rows):
            return Check(item, CLEAR, note="Revaluation dated the month's last day.", not_checked=tuple(unread))
        return Check(item, OPEN, count=len(with_balance), oldest=None, not_checked=tuple(unread))
    if unread or untold or cut:
        return Check(item, UNREAD, note="Not every balance in another currency could be read this run.",
                     not_checked=tuple(unread))
    return Check(item, CLEAR, note="No balance in another currency.")


def check_deferred(item: Item, revenue: Mapping[str, Any], expense: Mapping[str, Any],
                   runs: Mapping[str, Any]) -> Check:
    defers = _has_rows(revenue) or _has_rows(expense)
    if not defers:
        if _unread(revenue) or _unread(expense) or _rows(revenue) is None:
            return Check(item, UNREAD, note="The deferring items couldn't be read.")
        return Check(item, CLEAR, note="Nothing is deferred.")
    rows = _rows(runs)
    if rows is None:
        return Check(item, UNREAD, note="The deferred accounting runs couldn't be read.")
    if rows:
        return Check(item, CLEAR, note="Deferred accounting run to the month's end.")
    return Check(item, OPEN, count=1)


def lock_state(periods: Mapping[str, Any], company: Mapping[str, Any], today: date) -> str:
    """The period's lock, in code's words: locked, not locked, or not read."""
    first, last = period(today)
    period_rows, company_rows = _rows(periods), _rows(company)
    if period_rows and any(
        (s := _day(r.get("start_date"))) is not None and (e := _day(r.get("end_date"))) is not None
        and s <= first and e >= last for r in period_rows
    ):
        return "locked by an Accounting Period"
    if company_rows and any(
        (f := _day(r.get("accounts_frozen_till_date"))) is not None and f >= last for r in company_rows
    ):
        return "locked: accounts frozen to the month's end"
    if period_rows is None or company_rows is None:
        return UNREAD
    return "not locked"


def known_currencies(accounts: Mapping[str, Any]) -> frozenset[str]:
    """The currencies the company's accounts use (read 8a and its company
    currency): the only ones a notice or card names. Any other code is
    ERPNext's own text and is dropped."""
    rows = _rows(accounts) or []
    found = {_currency(r.get("account_currency")) for r in rows} | {_currency(accounts.get("currency"))}
    return frozenset(c for c in found if c)


def _named_currencies(check: Check, known: frozenset[str]) -> Check:
    """``check`` with any currency the company's accounts don't use
    unnamed (its sum kept under "")."""
    if any(c and c not in known for c in check.sums):
        merged: dict[str, float] = {}
        for currency, amount in check.sums.items():
            key = currency if currency in known else ""
            merged[key] = round(merged.get(key, 0.0) + amount, 2)
        check.sums = merged
    return check


def checks(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[Check]:
    """The nine items, in order, from the reads."""
    r = list(reads) + [{}] * (R_STATEMENTS + 1 - len(reads))
    known = known_currencies(r[R_ACCOUNTS])
    return [_named_currencies(c, known) for c in _checks(r, settings, today)]


def _checks(r: list[Mapping[str, Any]], settings: Settings, today: date) -> list[Check]:
    return [
        check_drafts(BY_KEY["purchase_drafts"], r[R_PI], today),
        check_drafts(BY_KEY["sales_drafts"], r[R_SI], today),
        check_billed(BY_KEY["received"], r[R_RECEIVED], today, int(settings["unbilled_grace_days"])),
        check_billed(BY_KEY["delivered"], r[R_DELIVERED], today, int(settings["unbilled_grace_days"])),
        check_bank(BY_KEY["bank"], r[R_BANKS], r[R_STATEMENTS], today, int(settings["bank_grace_days"])),
        check_reposts(BY_KEY["stock"], r[R_REPOST], today),
        check_depreciation(BY_KEY["depreciation"], r[R_DEPRECIATION], today),
        check_revaluation(BY_KEY["revaluation"], r[R_ACCOUNTS], r[R_BALANCES], r[R_REVALUATION], today),
        check_deferred(BY_KEY["deferred"], r[R_REVENUE], r[R_EXPENSE], r[R_DEFERRED]),
    ]


# --------------------------------------------------------------------------- #
# What it leaves
# --------------------------------------------------------------------------- #


def _money(sums: Mapping[str, float]) -> str:
    """A sum in one currency ("1,234.50 USD"), or "" for none or several."""
    if len(sums) != 1:
        return ""
    [(currency, amount)] = sums.items()
    return f"{amount:,.2f}" + (f" {currency}" if currency else "")


def _count_text(check: Check) -> str:
    noun = check.item.noun if check.count == 1 else check.item.nouns
    return f"{'at least ' if check.at_least else ''}{check.count} {noun}"


def description(check: Check, today: date) -> str:
    """A ToDo's text: code's template around the month, a count, a sum and a
    date, never an ERPNext name."""
    _first, last = period(today)
    parts = [_count_text(check)]
    money = _money(check.sums)
    if money:
        parts.append(money)
    if check.oldest is not None:
        parts.append(f"oldest {check.oldest.isoformat()}")
    text = f"Month-end close of {MONTHS[last.month - 1]} {last.year}: {', '.join(parts)}. {check.item.where}"
    return text[:DESCRIPTION_MAX]


def _template_re(item: Item) -> re.Pattern[str]:
    noun = rf"(?:{re.escape(item.noun)}|{re.escape(item.nouns)})"
    return re.compile(
        r"Month-end close of (?P<month>" + "|".join(MONTHS) + r") (?P<year>\d{4}): (?:at least )?\d{1,6} "
        + noun + r"(?:, -?\d{1,3}(?:,\d{3})*\.\d{2}(?: [A-Z]{3})?)?(?:, oldest (?P<oldest>\d{4}-\d{2}-\d{2}))?\. "
        + re.escape(item.where),
        re.ASCII,
    )


TEMPLATES: Mapping[str, re.Pattern[str]] = {item.key: _template_re(item) for item in ITEMS}


def item_of(text: object, day: object = None) -> Item | None:
    """The item whose template ``text`` is, in full, or None. With ``day``
    (a ToDo's date, "YYYY-MM-DD"), the text's month must be the one before
    the day's, and its oldest date on or before that month's end."""
    if not isinstance(text, str) or len(text) > DESCRIPTION_MAX:
        return None
    for item in ITEMS:
        found = TEMPLATES[item.key].fullmatch(text)
        if found is None:
            continue
        if day is None:
            return item
        when = _day(day) if isinstance(day, str) and len(day) == 10 else None
        if when is None:
            return None
        last = period(when)[1]
        if (MONTHS.index(found.group("month")) + 1, int(found.group("year"))) != (last.month, last.year):
            return None
        oldest = found.group("oldest")
        if oldest is not None and ((d := _day(oldest)) is None or d > last):
            return None
        return item
    return None


def _state_text(check: Check) -> str:
    if check.state == OPEN:
        return "open"
    return check.state


def _row(check: Check, company: str, today: date, owner_text: str) -> CardSpec:
    values: list[tuple[str, str]] = [("State", _state_text(check))]
    if check.state == OPEN:
        values.append(("Count", f"{'at least ' if check.at_least else ''}{check.count}"))
        money = _money(check.sums)
        if money:
            values.append(("Value", money))
        elif len(check.sums) > 1:
            values.append(("Value", "in several currencies"))
        if check.oldest is not None:
            values.append(("Oldest", check.oldest.isoformat()))
        values.append(("Owner", owner_text or "no one: set its owner or a reviewer"))
    if check.note:
        values.append(("Note", check.note))
    if check.not_checked:
        values.append(("Not checked", "; ".join(check.not_checked) + "; add them to the watch listing"))
    return CardSpec(key=_item_key(company, today, check.item.key), summary=check.item.title, values=tuple(values))


def _owner_text(item: str, who: Mapping[str, str], settings: Settings) -> str:
    """Who takes an item's task, as the notice says it: the setting that
    names them, never the address (a notice shows no addresses)."""
    if not who.get(item):
        return ""
    return f"its owner ({setting_key(item)})" if settings.get(setting_key(item)) else "the reviewer"


def subject(found: Sequence[Check], today: date) -> str:
    clear = sum(1 for c in found if c.state == CLEAR)
    if clear == REQUIRED:
        return ALL_CLEAR.format(month=month_name(today))
    return SOME_CLEAR.format(month=month_name(today), clear=clear, required=REQUIRED)


ALL_CLEAR_LINE = (
    "All clear. Lock the period? Create an Accounting Period for {month} {year} in the ERP, listing the "
    "document types to close: the job never locks it."
)
UNREAD_LINE = "A check shown as not read couldn't be read this run, so it isn't counted as clear."
NO_OWNER_LINE = (
    "An open item has no one to take its task (no owner set and no reviewer), so no task was prepared for it."
)


def build(
    reads: Sequence[Mapping[str, Any]], settings: Settings, today: date, *,
    owners: Mapping[str, str] | None = None,
) -> list[CardSpec | DraftSpec]:
    """The close notice and a ToDo per open item, figures from the reads
    only. ``owners``: who takes each item's task (``assignees``)."""
    company = str(settings["company"])
    found = checks(reads, settings, today)
    lock = lock_state(reads[R_PERIOD] if len(reads) > R_PERIOD else {},
                      reads[R_COMPANY] if len(reads) > R_COMPANY else {}, today)
    who = dict(owners or {})
    first, last = period(today)
    notes: list[str] = []
    if any(c.state == UNREAD for c in found):
        notes.append(UNREAD_LINE)
    if all(c.state == CLEAR for c in found):
        notes.append(ALL_CLEAR_LINE.format(month=MONTHS[last.month - 1], year=last.year))
    if any(c.state == OPEN and not who.get(c.item.key) for c in found):
        notes.append(NO_OWNER_LINE)
    rows = [_row(c, company, today, _owner_text(c.item.key, who, settings)) for c in found]
    rows.append(CardSpec(key=_item_key(company, today, "lock"), summary=LOCK_TITLE, values=(("State", lock),)))
    clear = sum(1 for c in found if c.state == CLEAR)
    notice = CardSpec(
        key=_notice_key(company, today),
        summary=f"Month-end close of {MONTHS[last.month - 1]} {last.year}",
        values=(("Clear", f"{clear} of {REQUIRED}"), ("Period", f"{first.isoformat()} to {last.isoformat()}")),
        items=tuple(rows),
        notes=tuple(notes),
        count=REQUIRED - clear,
        subject=subject(found, today),
    )
    drafts: list[DraftSpec] = []
    for check in found:
        owner = who.get(check.item.key, "")
        if check.state != OPEN or not owner:
            continue
        text = description(check, today)
        if item_of(text) is not check.item:
            continue
        drafts.append(DraftSpec(
            key=_item_key(company, today, check.item.key),
            operation="create",
            doctype=DOCTYPE,
            fields={"description": text, "allocated_to": owner, "priority": "Medium", "date": today.isoformat()},
            expect={"description": text},
            title=f"Close of {MONTHS[last.month - 1]}: {check.item.title}",
            group=check.item.title,
        ))
    return [notice, *drafts]


# --------------------------------------------------------------------------- #
# Who takes each item's task
# --------------------------------------------------------------------------- #


def setting_key(item: str) -> str:
    return f"assign_to_{item}"


def assignees(settings: Settings | None = None) -> dict[str, str]:
    """Who takes each item's task: its ``assign_to_<item>``, else the
    reviewer's email (their ERPNext user); an item whose setting can't take
    tasks (the configurer, a demo account) gets "" (no card)."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.people.store import get_person
    from openexecutive.watch import registry
    from openexecutive.watch.settings import assign_to_problem, effective_settings, get_row

    job = registry.get(JOB_ID)
    if job is None:
        return {}
    row = get_row(JOB_ID)
    current = settings if settings is not None else effective_settings(job, row)
    reviewer = ""
    reviewer_id = getattr(row, "reviewer_person_id", None)
    if isinstance(reviewer_id, int) and not isinstance(reviewer_id, bool):
        person = get_person(reviewer_id)
        email = normalize_email(person.email or "") if person is not None else ""
        reviewer = email if "@" in email and not assign_to_problem(email, row.configured_by_person_id) else ""
    out: dict[str, str] = {}
    for item in ITEMS:
        given = str(current.get(setting_key(item.key)) or "")
        if given:
            out[item.key] = "" if assign_to_problem(given, row.configured_by_person_id) else given
        else:
            out[item.key] = reviewer
    return out


def _assign_notice(settings: Settings, today: date) -> CardSpec | None:
    """The principal's notice, once a week, that an item's owner setting
    can't take its tasks."""
    from openexecutive.watch.settings import ASSIGN_TO_LINES, assign_problem, get_row

    problem = assign_problem(settings, get_row(JOB_ID).configured_by_person_id)
    if not problem:
        return None
    year, week, _ = today.isocalendar()
    return CardSpec(
        key=f"assign-to:{JOB_ID}:{year}-W{week:02d}", summary="A setting of this job needs a change",
        notes=(ASSIGN_TO_LINES[problem],), to_principal=True,
    )


def _build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[CardSpec | DraftSpec]:
    out = build(reads, settings, today, owners=assignees(settings))
    notice = _assign_notice(settings, today)
    return [notice, *out] if notice is not None else out


def check_fields(operation: str, doctype: str, fields: Mapping[str, Any], name: str = "") -> dict[str, Any] | str:
    """A month-end card's fields, as they are checked, stored and sent: a new
    ToDo with no document, Medium, dated, its text exactly one item's
    template, for that item's owner now, and exactly ``_todo``'s output. Run
    before each check and again at approval."""
    from openexecutive.integrations.erpnext_drafts import _todo

    if operation != "create" or doctype != DOCTYPE or name:
        return "The month-end job leaves ToDo drafts only."
    if not isinstance(fields, Mapping) or set(fields) != {"description", "allocated_to", "priority", "date"}:
        return "A month-end card's fields are a ToDo's description, allocated_to, priority and date."
    if fields.get("priority") != "Medium":
        return "A month-end card's priority is Medium."
    item = item_of(fields.get("description"), fields.get("date"))
    if item is None:
        return (
            "A month-end card's description is the job's own (Month-end close of <month> ...), "
            "dated in the month after."
        )
    expected = assignees().get(item.key, "")
    if not expected or fields.get("allocated_to") != expected:
        return "A month-end card's ToDo is for the item's owner: its assign_to setting, else the reviewer."
    try:
        out = _todo(dict(fields))
    except ValueError as exc:
        return str(exc)
    if out != dict(fields):
        return "A month-end card's fields must be as the job built them."
    return out


JOB: WatchJob = WatchJob(
    id=JOB_ID,
    title="Month-end close",
    reads=(
        WatchRead("list_records", PI_SHAPE, _drafts(PI_SHAPE), "the supplier invoices in draft", may_fail=True),
        WatchRead("list_records", SI_SHAPE, _drafts(SI_SHAPE), "the sales invoices in draft", may_fail=True),
        WatchRead("run_report", RECEIVED_SHAPE, _billed(RECEIVED_REPORT), "the received items to bill",
                  may_fail=True),
        WatchRead("run_report", DELIVERED_SHAPE, _billed(DELIVERED_REPORT), "the delivered items to bill",
                  may_fail=True),
        WatchRead("list_records", REPOST_SHAPE, _reposts, "the stock reposts", may_fail=True),
        WatchRead("list_records", DEPRECIATION_SHAPE, _depreciation, "the depreciation lines", may_fail=True),
        WatchRead("list_records", ACCOUNTS_SHAPE, _accounts, "the accounts and their currencies", may_fail=True),
        WatchRead("list_records", REVALUATION_SHAPE, _revaluations, "the exchange rate revaluations",
                  may_fail=True),
        WatchRead("list_records", DEFERRED_REVENUE_SHAPE, _deferring_revenue, "the items deferring revenue",
                  may_fail=True),
        WatchRead("list_records", DEFERRED_EXPENSE_SHAPE, _deferring_expense, "the items deferring expense",
                  may_fail=True),
        WatchRead("list_records", DEFERRED_RUN_SHAPE, _deferred_runs, "the deferred accounting runs",
                  may_fail=True),
        WatchRead("list_records", PERIOD_SHAPE, _period, "the accounting periods", may_fail=True),
        WatchRead("list_records", COMPANY_SHAPE, _company, "the company's frozen date", may_fail=True),
        WatchRead("list_records", BANKS_SHAPE, _banks, "the bank accounts", may_fail=True),
        WatchRead("run_report", BALANCE_SHAPE, _balances, "the balances in other currencies",
                  repeat=MAX_ACCOUNTS, may_fail=True),
        WatchRead("run_report", STATEMENT_SHAPE, _statements, "the bank reconciliation statements",
                  repeat=MAX_ACCOUNTS, may_fail=True),
    ),
    build=_build,
    schedule=time(7, 30),
    weekdays=frozenset(range(5)),
    runs_on=runs_on,
    card_cap_day=10,
    default_settings={
        "company": "", "close_days": 5, "unbilled_grace_days": 0, "bank_grace_days": 5,
        **{setting_key(item.key): "" for item in ITEMS},
    },
    check_fields=check_fields,
    setting_bounds={"close_days": (1, 15), "unbilled_grace_days": (0, 60), "bank_grace_days": (0, 60)},
    card_noun="close tasks",
    once=True,
    periodic_keys=True,
    updates_notice=True,
    subject_pattern=SUBJECTS,
)


def listing(foreign: Sequence[str], banks: Sequence[str]) -> list[dict[str, Any]]:
    """The job's entry in oe_connector_watch_jobs, as oe_connector lists it,
    for these foreign-currency and bank accounts: each fixed read, then a
    balance read per foreign account and a statement per bank account, the
    account a literal in place of this side's own slot."""
    fixed = [{"tool": read.tool, **read.shape} for read in JOB.reads[:R_BALANCES]]
    balance = [
        {"tool": "run_report", **{**BALANCE_SHAPE, "filters": {**BALANCE_SHAPE["filters"], "account": [a]}}}
        for a in foreign
    ]
    statement = [
        {"tool": "run_report", **{**STATEMENT_SHAPE, "filters": {**STATEMENT_SHAPE["filters"], "account": a}}}
        for a in banks
    ]
    return [*fixed, *balance, *statement]

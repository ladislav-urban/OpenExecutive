"""Check 3 of the answer check: a headline figure the reply built from a list
read, confirmed against ERPNext's standard report for it (oe_connector's
``docs/answer-check-plan.md``, section 4; PR (c)).

Check 1 passes a figure that matches what the turn read. But a total added up
from ``list_records`` (submitted Sales Invoices' ``base_net_total``, say) can
still differ from ERPNext's own figure: credit notes counted differently,
document currency for company currency, journal entries missed, a list cut off.
oe_connector's ``MEASURES`` (from ``answer_check_context``) name each headline
figure's standard report and its "quick ways", the list reads that answer it
directly. This module finds the reads of the turn that are one of them, and
what total each gives (``quick_totals``). ``answer_check`` asks oe_connector's
hidden ``confirm_figure`` for the report's total, as the turn's own caller, for
a figure check 1 matched to one, at most ``MAX_CONFIRMS`` a turn:

- the same, rounded as the reply shows it: the figure is confirmed (a total
  over a cut-off list too);
- different: the report's total is the figure's right value, written in by code.

A read is a quick way only when nothing narrows it beyond the measure's own
terms: one DocType, the measure's field summed (by its aggregate, or every
row), submitted documents, the company, the period (or none, for a balance as
of today), and for receivables and payables one customer or supplier. A read
grouped or filtered any other way ("the overdue ones") answers something else,
and is left to checks 1 and 2. For the measures read from the ledger by account
type (cash and bank, cost of goods sold), a read of GL Entry's debit and credit
on named accounts is one, and the accounts go to oe_connector, which checks
they are of the measure's type (else "not_this_measure": no measure) and runs
the report on exactly those, so a figure about one bank account is never held
to all of them.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from openexecutive.orchestrator.figure_check import ErpnextResult

# Report runs a turn may ask for (plan section 4: "a cap of 3 calls").
MAX_CONFIRMS = 3

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A balance as of now (a document's outstanding is today's): no date filter.
_BALANCES = frozenset({"receivables_outstanding", "payables_outstanding"})
# A balance as of a day: no start, an end or none (today).
_AS_OF = frozenset({"cash_and_bank", "stock_value"})
# A flow over a period: both ends needed.
_PERIODS = frozenset({"revenue", "cost_of_goods_sold"})
# What oe_connector takes as `accounts`.
MAX_ACCOUNTS = 50
_PARTIES = {"receivables_outstanding": ("Customer", "customer"), "payables_outstanding": ("Supplier", "supplier")}
# Filters a quick way may carry besides its period and party.
_PLAIN_FILTERS = frozenset({"docstatus", "company", "is_cancelled"})


@dataclass(frozen=True)
class QuickTotal:
    """The total a quick way read gave, and how to ask its report for it."""

    value: float
    measure: str
    company: str
    from_date: str | None
    to_date: str
    party_type: str | None = None
    party: str | None = None
    accounts: tuple[str, ...] = ()
    truncated: bool = False
    # Added up from the read's rows, not its SUM aggregate: check 1 must have
    # matched the figure as that column's sum, never as one row's value.
    from_rows: bool = False

    def arguments(self) -> dict[str, Any]:
        args: dict[str, Any] = {"measure": self.measure, "company": self.company, "to_date": self.to_date}
        if self.from_date:
            args["from_date"] = self.from_date
        if self.party_type and self.party:
            args["party_type"], args["party"] = self.party_type, self.party
        if self.accounts:
            args["accounts"] = list(self.accounts)
        return args


def _filters(raw: Any) -> list[tuple[str, str, Any]] | None:
    """A read's filters as oe_connector ran them (``[[field, op, value]]``),
    or None when they aren't in that shape."""
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 3 or not isinstance(item[0], str) or not isinstance(item[1], str):
            return None
        out.append((item[0], item[1].strip().lower(), item[2]))
    return out


def _period(filters: list[tuple[str, str, Any]]) -> tuple[str | None, str | None] | None:
    """From and to of the posting_date filters, or None when they're of
    another kind."""
    start: str | None = None
    end: str | None = None
    for field, op, value in filters:
        if field != "posting_date":
            continue
        # A bound given twice: ERPNext applies both, so the period isn't one.
        if op == "between" and isinstance(value, list) and len(value) == 2 and start is None and end is None:
            start, end = str(value[0]), str(value[1])
        elif op == ">=" and start is None:
            start = str(value)
        elif op == "<=" and end is None:
            end = str(value)
        else:
            return None
    for day in (start, end):
        if day is not None and not _DATE_RE.match(day):
            return None
    return start, end


def _number(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _ledger(result: ErpnextResult) -> float | None:
    """GL Entry's debit less credit, as the read gave them (both SUM
    aggregates, or every row's)."""
    rows = result.payload.get("rows") if isinstance(result.payload, dict) else None
    if not isinstance(rows, list):
        return None
    aggregates = result.arguments.get("aggregates")
    if isinstance(aggregates, list) and aggregates:
        if len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        sums: dict[str, float] = {}
        for agg in aggregates:
            if isinstance(agg, dict) and str(agg.get("function", "")).upper() == "SUM" and agg.get("field") in (
                "debit", "credit",
            ):
                got = _number(rows[0].get(agg.get("as") or f"sum_{agg['field']}"))
                if got is not None:
                    sums[str(agg["field"])] = got
        return sums["debit"] - sums["credit"] if len(sums) == 2 else None
    requested = result.arguments.get("fields")
    if not isinstance(requested, list) or not {"debit", "credit"} <= set(requested):
        return None
    total = 0.0
    for row in rows:
        debit, credit = (_number(row.get(k)) if isinstance(row, dict) else None for k in ("debit", "credit"))
        if debit is None or credit is None:
            return None
        total += debit - credit
    return total


def _summed(result: ErpnextResult, fields: Iterable[str]) -> tuple[float, str] | None:
    """The total of one of ``fields`` the read gave (its SUM aggregate, or its
    rows added up), and the field."""
    payload = result.payload
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    wanted = list(fields)
    aggregates = result.arguments.get("aggregates")
    if isinstance(aggregates, list) and aggregates:
        if len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        for agg in aggregates:
            if not isinstance(agg, dict) or str(agg.get("function", "")).upper() != "SUM":
                continue
            field = agg.get("field")
            if field in wanted:
                alias = agg.get("as") or f"sum_{str(field).replace('.', '_')}"
                value = rows[0].get(alias)
                if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
                    return float(value), str(field)
        return None
    requested = result.arguments.get("fields")
    if not isinstance(requested, list):
        return None
    for field in wanted:
        if field not in requested:
            continue
        values = [_number(row.get(field)) if isinstance(row, dict) else None for row in rows]
        present = [v for v in values if v is not None]
        if present and len(present) == len(values):
            return sum(present), field
    return None


def _quick(result: ErpnextResult, measure: dict[str, Any], quick: dict[str, Any], today: date) -> QuickTotal | None:
    payload = result.payload
    if result.tool != "erpnext__list_records" or not isinstance(payload, dict):
        return None
    if quick.get("doctype") != (payload.get("doctype") or result.arguments.get("doctype")):
        return None
    # Grouped, sorted or cut to a few rows: the top one's figure, not the measure's.
    if result.arguments.get("group_by") or result.arguments.get("order_by"):
        return None
    aggregated = bool(result.arguments.get("aggregates"))
    if result.arguments.get("limit") and not aggregated:
        return None
    fields = quick.get("fields")
    # Only the filters oe_connector says it ran; none echoed, no quick way.
    filters = _filters(payload.get("filters")) if "filters" in payload else None
    if not isinstance(fields, list) or filters is None:
        return None
    name = str(measure.get("measure"))
    party_type, party_field = _PARTIES.get(name, (None, None))
    party: str | None = None
    accounts: tuple[str, ...] = ()
    by_type = bool(quick.get("account_type"))
    for field, op, value in filters:
        if field == "posting_date":
            continue
        if field == party_field and op == "=" and isinstance(value, str):
            party = value
        elif by_type and field == "account" and op == "=" and isinstance(value, str):
            accounts = (value,)
        elif by_type and field == "account" and op == "in" and isinstance(value, list):
            if not value or len(value) > MAX_ACCOUNTS or not all(isinstance(v, str) for v in value):
                return None
            accounts = tuple(value)
        elif field not in _PLAIN_FILTERS or op != "=":
            return None
    if by_type and not accounts:
        # Every account of the type: a list read can't have chosen them so.
        return None
    if any(f == "docstatus" and v not in (1, "1") for f, _, v in filters):
        return None
    period = _period(filters)
    if period is None:
        return None
    start, end = period
    if name in _BALANCES:
        if start or end:
            return None
        start, end = None, today.isoformat()
    elif name in _AS_OF:
        if start:
            return None
        end = end or today.isoformat()
    elif name in _PERIODS:
        if not (start and end):
            return None
    else:
        return None
    company = payload.get("company") or next((v for f, _, v in filters if f == "company"), None)
    if not isinstance(company, str) or not company:
        return None
    if by_type:
        ledger = _ledger(result)
        value = ledger
    else:
        summed = _summed(result, fields)
        value = summed[0] if summed is not None else None
    if value is None:
        return None
    return QuickTotal(
        value=abs(value), measure=name, company=company, from_date=start, to_date=str(end),
        party_type=party_type if party else None, party=party, accounts=accounts,
        truncated=payload.get("truncated") is True, from_rows=not aggregated,
    )


def quick_totals(results: Iterable[ErpnextResult], measures: Any, today: date) -> list[QuickTotal]:
    """Every read of the turn that is one of a measure's quick ways."""
    if not isinstance(measures, list):
        return []
    out: list[QuickTotal] = []
    for result in results:
        for measure in measures:
            if not isinstance(measure, dict):
                continue
            for quick in measure.get("quick") or []:
                if isinstance(quick, dict):
                    got = _quick(result, measure, quick, today)
                    if got is not None and got not in out:
                        out.append(got)
    return out


def report_total(payload: Any) -> float | None:
    """``confirm_figure``'s total, or None when it can't be used (an error, or
    a report ERPNext cut off)."""
    if not isinstance(payload, dict) or "error" in payload or payload.get("truncated") is True:
        return None
    total = payload.get("total")
    if isinstance(total, int | float) and not isinstance(total, bool) and math.isfinite(total):
        return float(total)
    return None

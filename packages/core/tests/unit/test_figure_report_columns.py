"""run_report with `columns` (ladislav-urban/oe_connector#617): the answer
check reads a slim report exactly as it reads the full one. Its figures are
found, a truncated result is still cut off, and `columns_kept` /
`columns_dropped` are never figures. Made-up companies and amounts."""
from __future__ import annotations

from datetime import date
from typing import Any

from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    NumberFormat,
    check_figures,
    sources_for,
)
from openexecutive.orchestrator.figure_confirm import quick_totals, report_total

COMPANY = "Example Trading Co"
# The slim set the guide names, as #617's own test spells it.
SLIM = ["posting_date", "due_date", "party", "voucher_type", "voucher_no", "currency", "invoiced", "outstanding"]
_TYPES = {"posting_date": "Date", "due_date": "Date", "party": "Dynamic Link", "voucher_type": "Data",
          "voucher_no": "Dynamic Link", "currency": "Link", "invoiced": "Currency", "outstanding": "Currency"}
ROWS = [
    {"posting_date": "2026-09-02", "due_date": "2026-10-02", "party": "Northwind Supplies",
     "voucher_type": "Sales Invoice", "voucher_no": "SINV-0101", "currency": "USD",
     "invoiced": 15000.0, "outstanding": 12500.0},
    {"posting_date": "2026-10-11", "due_date": "2026-11-10", "party": "Southgate Partners",
     "voucher_type": "Sales Invoice", "voucher_no": "SINV-0102", "currency": "USD",
     "invoiced": 35700.0, "outstanding": 35700.0},
    {"posting_date": "2026-10-20", "due_date": "2026-11-19", "party": "Kestrel Werke",
     "voucher_type": "Sales Invoice", "voucher_no": "SINV-0103", "currency": "EUR",
     "invoiced": 8400.0, "outstanding": 8400.0},
]
MEASURES = [
    {"measure": "receivables_outstanding", "report": "Accounts Receivable Summary",
     "quick": [{"doctype": "Sales Invoice", "fields": ["outstanding_amount"]}]},
]


def _report(*, slim: bool, truncated: bool = False) -> ErpnextResult:
    payload: dict[str, Any] = {
        "report_name": "Accounts Receivable",
        "company": COMPANY,
        "currency": "USD",
        "columns": [{"fieldname": f, "fieldtype": _TYPES[f]} for f in SLIM],
        "rows": [dict(r) for r in ROWS],
        "row_count": 7 if truncated else len(ROWS),
        "truncated": truncated,
    }
    if slim:
        # What #617 adds when `columns` is given; nothing else changes.
        payload["columns_kept"] = list(SLIM)
        payload["columns_dropped"] = 21
    arguments: dict[str, Any] = {"report_name": "Accounts Receivable", "filters": {"company": COMPANY}}
    if slim:
        arguments["columns"] = list(SLIM)
    return ErpnextResult("erpnext__run_report", arguments, payload)


def _statuses(text: str, result: ErpnextResult) -> dict[str, str]:
    fmt = NumberFormat.parse(None)
    return {f.text: f.status for f in check_figures(text, sources_for([result], fmt=fmt), fmt)}


def test_the_reader_reads_a_slim_report_as_the_full_one() -> None:
    for truncated in (False, True):
        slim = sources_for([_report(slim=True, truncated=truncated)])
        full = sources_for([_report(slim=False, truncated=truncated)])
        assert slim == full
    # Neither key is read as a value: 21 columns dropped is no figure.
    assert 21.0 not in sources_for([_report(slim=True)]).values


def test_the_empty_column_count_is_never_a_figure() -> None:
    # ladislav-urban/oe_connector#619 trims empty columns and says how many.
    result = _report(slim=True)
    result.payload["columns_empty"] = 17
    assert 17.0 not in sources_for([result]).values
    assert sources_for([result]) == sources_for([_report(slim=True)])
    assert _statuses("17 customers are overdue.", result)["17"] != "confirmed"


def test_a_slim_reports_figures_are_found() -> None:
    got = _statuses(
        "Northwind owes $12,500 and Southgate $35,700; the dollar invoices come to $48,200, "
        "and Kestrel owes €8,400.",
        _report(slim=True),
    )
    assert got == {"$12,500": "confirmed", "$35,700": "confirmed", "$48,200": "confirmed", "€8,400": "confirmed"}


def test_a_truncated_slim_report_is_still_cut_off() -> None:
    got = _statuses("The largest is $35,700; the dollar invoices come to $48,200.", _report(slim=True, truncated=True))
    assert got == {"$35,700": "confirmed", "$48,200": "from_truncated"}


def test_the_extra_keys_are_never_a_figure() -> None:
    assert _statuses("It left out 21 columns, $21 in all.", _report(slim=True))["$21"] == "unconfirmed"


def test_check_3_reads_a_slim_report_as_before() -> None:
    # Check 3's quick ways are list reads: a report, slim or not, is never one.
    today = date(2026, 11, 18)
    assert quick_totals([_report(slim=True)], MEASURES, today) == []
    assert quick_totals([_report(slim=False)], MEASURES, today) == []
    # The report it reruns gives its total; extra keys beside it change nothing,
    # and a cut-off one still gives none.
    payload = {"total": 56600.0, "columns_kept": ["outstanding"], "columns_dropped": 3, "truncated": False}
    assert report_total(payload) == 56600.0
    assert report_total({**payload, "truncated": True}) is None

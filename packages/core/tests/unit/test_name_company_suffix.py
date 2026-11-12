"""Account, cost center and warehouse names written without their company
suffix ("Truck Diesel" for "Truck Diesel - ABC") are confirmed, and a table
whose rows no longer add up to its kept total goes whole (a demo reshoot).
Made-up names and figures."""

from __future__ import annotations

from openexecutive.orchestrator import answer_correct
from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    NumberFormat,
    find_figures,
)
from openexecutive.orchestrator.name_check import Name, check_names, name_sources_for

FMT = NumberFormat.parse(None)

# ---------------------------------------------------------------------------
# Account names without the company suffix; a total left beside one row

BUDGET = ErpnextResult(
    "erpnext__run_report",
    {"report_name": "Budget Variance Report"},
    {
        "report_name": "Budget Variance Report",
        "columns": [
            {"fieldname": "budget_against", "fieldtype": "Link", "label": "Cost Center", "options": "Cost Center"},
            {"fieldname": "account", "fieldtype": "Link", "label": "Account", "options": "Account"},
            {"fieldname": "actual", "fieldtype": "Float", "label": "Actual"},
        ],
        "rows": [
            {"budget_against": "Yard - BRK", "account": "Truck Diesel - BRK", "actual": 410.0},
            {"budget_against": "Yard - BRK", "account": "Shop Supplies - BRK", "actual": 95.5},
        ],
    },
)


def _names(text: str) -> dict[str, str]:
    return {n.text: n.status for n in check_names(text, name_sources_for([BUDGET]))}


def test_an_account_without_its_company_suffix_is_confirmed() -> None:
    got = _names("| Account | Actual |\n|---|---|\n| Truck Diesel | 410 |\n| Shop Supplies | 95.5 |\n")
    assert got == {"Truck Diesel": "confirmed", "Shop Supplies": "confirmed"}
    # Any other short name is still unknown, and a suffix must be one.
    got = _names("| Account | Actual |\n|---|---|\n| Truck Petrol | 410 |\n| Truck Diesel - | 95.5 |\n")
    assert got["Truck Petrol"] != "confirmed" and got["Truck Diesel -"] != "confirmed"


def test_only_a_short_company_suffix_is_dropped() -> None:
    src = name_sources_for([ErpnextResult(
        "erpnext__run_report", {},
        {"columns": [{"fieldname": "account", "fieldtype": "Link", "options": "Account"}],
         "rows": [{"account": "Fuel - not an abbr"}, {"account": "Rent - ABCDEFGHIJK"}]},
    )])
    got = {n.text: n.status for n in check_names("| Account |\n|---|\n| Fuel |\n| Rent |\n", src)}
    assert got == {"Fuel": "unconfirmed", "Rent": "unconfirmed"}


def test_a_table_whose_rows_no_longer_add_up_to_its_total_goes_whole() -> None:
    text = (
        "Fuel ran over in October, by $210.00 on $200.00 budgeted. Supplies stayed at $95.50. "
        "Rent was $1,200.00 and power $310.00.\n\n"
        "| Account | Actual |\n|---|---|\n| Truck Diesel | $410.00 |\n| Mystery Line | $50.00 |\n"
        "| Shop Supplies | $95.50 |\n| Total | $555.50 |\n"
    )
    every = find_figures(text)
    start = text.index("Mystery Line")
    bad = Name(start, start + len("Mystery Line"), "Mystery Line", "cell")
    out = answer_correct.remove(text, [bad], every)
    assert "|" not in out and "Total" not in out
    assert out.startswith("Fuel ran over in October")
    # Rows that still add up keep the table.
    fine = text.replace("| Mystery Line | $50.00 |", "| Mystery Line | $0.00 |").replace("$555.50", "$505.50")
    every = find_figures(fine)
    start = fine.index("Mystery Line")
    bad = Name(start, start + len("Mystery Line"), "Mystery Line", "cell")
    out = answer_correct.remove(fine, [bad], every)
    assert "| Total | $505.50 |" in out and "Mystery" not in out



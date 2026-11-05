"""Check 3 of the answer check (oe_connector's docs/answer-check-plan.md,
section 4; PR (c)): a headline figure added up from a list read is held to its
standard report. Made-up companies and amounts."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check, answer_review, mcp_gateway
from openexecutive.orchestrator.answer_review import Review
from openexecutive.orchestrator.figure_check import ErpnextResult
from openexecutive.orchestrator.figure_confirm import MAX_CONFIRMS, quick_totals, report_total

TODAY = date(2026, 11, 5)
MEASURES = [
    {"measure": "revenue", "report": "Profit and Loss Statement",
     "quick": [{"doctype": "Sales Invoice", "fields": ["base_net_total", "grand_total"]}]},
    {"measure": "receivables_outstanding", "report": "Accounts Receivable Summary",
     "quick": [{"doctype": "Sales Invoice", "fields": ["outstanding_amount"]}]},
    {"measure": "cash_and_bank", "report": "General Ledger",
     "quick": [{"doctype": "GL Entry", "fields": ["debit", "credit"], "account_type": ["Bank", "Cash"]}]},
]
COMPANY = "Example Trading Co"


def _read(arguments: dict[str, Any], rows: list[dict[str, Any]], filters: list[list[Any]], **extra: Any) -> ErpnextResult:
    payload = {"doctype": arguments["doctype"], "rows": rows, "filters": filters, "company": COMPANY, **extra}
    return ErpnextResult("erpnext__list_records", arguments, payload)


REVENUE = _read(
    {"doctype": "Sales Invoice", "aggregates": [{"function": "SUM", "field": "base_net_total", "as": "revenue"}]},
    [{"revenue": 123450.0}],
    [["company", "=", COMPANY], ["posting_date", "between", ["2026-10-01", "2026-10-31"]], ["docstatus", "=", 1]],
)
OWED = _read(
    {"doctype": "Sales Invoice", "fields": ["name", "outstanding_amount"]},
    [{"name": "SINV-0001", "outstanding_amount": 30000.0}, {"name": "SINV-0002", "outstanding_amount": 18200.0}],
    [["company", "=", COMPANY], ["customer", "=", "Northwind Supplies"], ["docstatus", "=", 1]],
)


def test_a_reads_total_is_a_quick_way_only_when_nothing_else_narrows_it() -> None:
    got = quick_totals([REVENUE, OWED], MEASURES, TODAY)
    assert [(q.measure, q.value, q.from_date, q.to_date, q.party) for q in got] == [
        ("revenue", 123450.0, "2026-10-01", "2026-10-31", None),
        ("receivables_outstanding", 48200.0, None, "2026-11-05", "Northwind Supplies"),
    ]
    assert got[1].arguments() == {
        "measure": "receivables_outstanding", "company": COMPANY, "to_date": "2026-11-05",
        "party_type": "Customer", "party": "Northwind Supplies",
    }
    overdue = _read(OWED.arguments, OWED.payload["rows"], [*OWED.payload["filters"], ["status", "=", "Overdue"]])
    grouped = _read({**REVENUE.arguments, "group_by": ["customer"]}, [{"revenue": 1.0}], REVENUE.payload["filters"])
    open_ended = _read(REVENUE.arguments, REVENUE.payload["rows"], [["posting_date", ">=", "2026-10-01"]])
    drafts = _read(REVENUE.arguments, REVENUE.payload["rows"], [*REVENUE.payload["filters"][:2], ["docstatus", "=", 0]])
    assert quick_totals([overdue, grouped, open_ended, drafts], MEASURES, TODAY) == []


def test_a_ledger_read_on_named_accounts_is_one_with_its_accounts() -> None:
    cash = _read(
        {"doctype": "GL Entry", "aggregates": [
            {"function": "SUM", "field": "debit", "as": "d"}, {"function": "SUM", "field": "credit", "as": "c"},
        ]},
        [{"d": 90000.0, "c": 15000.0}],
        [["company", "=", COMPANY], ["account", "in", ["Main Bank - EX", "Petty Cash - EX"]],
         ["posting_date", "<=", "2026-10-31"], ["is_cancelled", "=", 0]],
    )
    (got,) = quick_totals([cash], MEASURES, TODAY)
    assert (got.measure, got.value, got.to_date, got.accounts) == (
        "cash_and_bank", 75000.0, "2026-10-31", ("Main Bank - EX", "Petty Cash - EX"),
    )
    # Every account of the type can't be what a list read chose.
    unnamed = _read(cash.arguments, cash.payload["rows"], [["company", "=", COMPANY]])
    assert quick_totals([unnamed], MEASURES, TODAY) == []


def test_a_report_total_counts_only_when_whole() -> None:
    assert report_total({"total": 118500.0, "truncated": False}) == 118500.0
    assert report_total({"total": None, "truncated": True}) is None
    assert report_total({"error": "not a measure", "reason": "not_this_measure"}) is None


# ---------------------------------------------------------------------------
# In the check


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    rows: list[Any] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: rows.append(kw.get("details")))

    async def no_review(**kw: Any) -> list[Review]:
        return [answer_review.CANNOT_TELL] * len(kw["flagged"])

    async def no_rewrite(*_a: Any) -> None:
        return None

    monkeypatch.setattr(answer_review, "review", no_review)
    monkeypatch.setattr(answer_check, "_rewrite", no_rewrite)
    return rows


def _turn(*results: ErpnextResult, brief: bool = False) -> answer_check.AnswerCheckTurn:
    turn = answer_check.AnswerCheckTurn(brief=brief)
    turn.results.extend(results)
    turn._context = {"number_format": "#,###.##", "measures": MEASURES}
    return turn


def _reports(monkeypatch: pytest.MonkeyPatch, totals: dict[str, float | None]) -> list[dict[str, Any]]:
    asked: list[dict[str, Any]] = []

    async def report(_turn: Any, quick: Any) -> float | None:
        asked.append(quick.arguments())
        return totals.get(quick.measure)

    monkeypatch.setattr(answer_check, "_report", report)
    return asked


def _check(text: str, turn: answer_check.AnswerCheckTurn) -> str:
    return asyncio.run(answer_check.check_reply(text, turn, question="How are we doing?", model="m"))


def test_a_list_total_the_report_disagrees_with_takes_the_reports(monkeypatch: pytest.MonkeyPatch, quiet: list[Any]) -> None:
    asked = _reports(monkeypatch, {"revenue": 118500.0})
    out = _check("Revenue in October was $123,450.", _turn(REVENUE))
    assert out == "Revenue in October was $118,500."
    assert asked == [{"measure": "revenue", "company": COMPANY, "to_date": "2026-10-31", "from_date": "2026-10-01"}]
    assert quiet[-1]["report_runs"] == 1 and quiet[-1]["report_differs"] == 1


def test_a_report_that_agrees_leaves_it_and_clears_a_cut_off_total(monkeypatch: pytest.MonkeyPatch) -> None:
    _reports(monkeypatch, {"revenue": 123450.0, "receivables_outstanding": 48200.0})
    assert _check("Revenue in October was $123,450.", _turn(REVENUE)) == "Revenue in October was $123,450."
    cut = _read(OWED.arguments, OWED.payload["rows"], OWED.payload["filters"], truncated=True)
    # A sum over a cut-off list is from_truncated in check 1; the report clears it.
    assert _check("Northwind Supplies owes $48,200.", _turn(cut)) == "Northwind Supplies owes $48,200."


def test_no_report_no_change_and_never_for_the_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _reports(monkeypatch, {"revenue": None})
    assert _check("Revenue in October was $123,450.", _turn(REVENUE)) == "Revenue in October was $123,450."
    asked.clear()
    assert _check("Revenue in October was $123,450.", _turn(REVENUE, brief=True)) == "Revenue in October was $123,450."
    assert asked == []  # the brief's service caller: confirm_figure refuses it


def test_at_most_three_reports_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _reports(monkeypatch, {"revenue": 1.0})
    months = [
        _read(REVENUE.arguments, [{"revenue": 1000.0 * (n + 1)}],
              [["company", "=", COMPANY], ["posting_date", "between", [f"2026-0{n + 1}-01", f"2026-0{n + 1}-28"]],
               ["docstatus", "=", 1]])
        for n in range(5)
    ]
    _check("Sales: $1,000, $2,000, $3,000, $4,000 and $5,000 over the months.", _turn(*months))
    assert len(asked) == MAX_CONFIRMS


def test_the_model_is_never_offered_confirm_figure(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit._erpnext_pins import pin_listing

    listing = "".join(f"## {n}\n**Description:** {n}\n\n" for n in ("erpnext__confirm_figure", "erpnext__run_report"))
    pin_listing(monkeypatch, listing)
    kept = mcp_gateway._drop_blocked_tools(listing)
    assert "erpnext__run_report" in kept and "erpnext__confirm_figure" not in kept

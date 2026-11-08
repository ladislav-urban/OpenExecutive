# ruff: noqa: F811 — pytest fixtures imported from test_answer_check share their names with the test parameters.
"""Counts of records in a reply, held to the counts the turn read (the pilot's
Nov 5, 2026 second retake: "Six customers are waiting" over five, "three
Cedar & Pine quotations" over two, "all six customer projects" over three, each
passed because some other value happened to be 6, 3 or 6).

Made-up companies and amounts."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check
from openexecutive.orchestrator.answer_review import CANNOT_TELL, Review
from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    NumberFormat,
    check_figures,
    sources_for,
)

from .test_answer_check import _reviews, _rewrites, audit_rows  # noqa: F401

# Five customers waiting, over six orders; one order's quantity is 6.
WAITING = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Sales Order", "fields": ["name", "customer", "status", "total_qty"]},
    {
        "doctype": "Sales Order",
        "rows": [
            {"name": "SO-1", "customer": "Alder Works", "status": "To Deliver", "total_qty": 6.0},
            {"name": "SO-2", "customer": "Birch Co", "status": "To Deliver", "total_qty": 40.0},
            {"name": "SO-3", "customer": "Birch Co", "status": "To Deliver", "total_qty": 52.0},
            {"name": "SO-4", "customer": "Cedar Ltd", "status": "To Deliver", "total_qty": 12.0},
            {"name": "SO-5", "customer": "Dune Inc", "status": "Overdue", "total_qty": 3.0},
            {"name": "SO-6", "customer": "Elm Group", "status": "Overdue", "total_qty": 9.0},
        ],
        "truncated": False,
    },
)


def _statuses(text: str) -> dict[str, str]:
    fmt = NumberFormat.parse(None)
    return {f.text: f.status for f in check_figures(text, sources_for([WAITING], fmt=fmt), fmt)}


def _turn() -> answer_check.AnswerCheckTurn:
    turn = answer_check.AnswerCheckTurn()
    turn.results.append(WAITING)
    turn._context = {"number_format": "#,###.##"}
    return turn


def _check(text: str) -> str:
    return asyncio.run(answer_check.check_reply(text, _turn(), question="Who is waiting on orders?", model="m"))


def test_a_count_of_records_is_held_to_the_counts_read() -> None:
    # 6 is a quantity in the rows, and six orders were read, but five customers.
    assert _statuses("Five customers are waiting.") == {"Five": "confirmed"}
    assert _statuses("Six orders are open.") == {"Six": "confirmed"}
    assert _statuses("Two Birch Co orders are open.") == {"Two": "confirmed"}
    assert _statuses("Seven customers are waiting.") == {"Seven": "unconfirmed"}
    # Six orders were read, but six customers weren't.
    assert _statuses("Six customers are waiting.") == {"Six": "unconfirmed"}
    assert _statuses("Three Birch Co orders are open.") == {"Three": "unconfirmed"}
    assert _statuses("2 orders are overdue.") == {"2": "confirmed"}


def test_a_quantity_is_still_any_value_read() -> None:
    # Breakers aren't records: 52 is a quantity, as before.
    assert _statuses("Birch Co is owed 52 breakers.") == {"52": "confirmed"}
    assert "6" not in _statuses("6 of 52 breakers went out.")


def test_a_wrong_count_with_a_known_value_is_written_in_words(monkeypatch: pytest.MonkeyPatch) -> None:
    _reviews(monkeypatch, Review("wrong", 5.0))
    _rewrites(monkeypatch, None)
    assert _check("Six customers are waiting on back orders.") == "Five customers are waiting on back orders."


def test_a_count_no_value_is_known_for_becomes_several(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any],
) -> None:
    _reviews(monkeypatch, CANNOT_TELL, CANNOT_TELL)
    _rewrites(monkeypatch, None)
    # In the lead, where removing it would have blanked the reply.
    assert _check("Seven customers are waiting on back orders. Birch Co is owed 52 breakers.") == (
        "Several customers are waiting on back orders. Birch Co is owed 52 breakers."
    )
    assert [r for r in audit_rows if r[0] == "answer_check"][-1][2]["whole_reply"] == 0
    assert _check("All seven customer orders are late. Birch Co is owed 52 breakers.") == (
        "All customer orders are late. Birch Co is owed 52 breakers."
    )


# ---------------------------------------------------------------------------
# Weekdays (orchestrator.weekday_check)


def test_a_weekday_beside_a_date_is_the_dates_own() -> None:
    from datetime import date

    from openexecutive.orchestrator import weekday_check

    today = date(2026, 11, 5)
    out, n = weekday_check.fix("- Northgate, $4,736.50, valid till Nov 8, expires Saturday.", today)
    assert (out, n) == ("- Northgate, $4,736.50, valid till Nov 8, expires Sunday.", 1)
    assert weekday_check.fix("It is due Sat, 8 November 2026.", today)[0] == "It is due Sun, 8 November 2026."
    assert weekday_check.fix("Due 2026-11-09 (Monday).", today) == ("Due 2026-11-09 (Monday).", 0)
    # Two dates, or none: which weekday goes with which isn't certain.
    two = "Nov 8 and Nov 11 both fall before Friday."
    assert weekday_check.fix(two, today) == (two, 0)
    assert weekday_check.fix("Call them on Friday.", today) == ("Call them on Friday.", 0)
    # A date in early January from November is next year's.
    assert weekday_check.fix("Due Jan 4, a Monday.", today) == ("Due Jan 4, a Monday.", 0)


def test_a_table_cell_no_value_is_known_for_shows_a_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    # The budget review in the retake: removing its rows emptied the table
    # and the whole reply went.
    _reviews(monkeypatch, CANNOT_TELL, CANNOT_TELL)
    _rewrites(monkeypatch, None)
    table = (
        "Orders by customer:\n\n"
        "| Customer | Qty |\n|---|---|\n"
        "| Birch Co | 777 |\n| Cedar Ltd | 888 |\n"
    )
    assert _check(table) == table.replace("777", "—").replace("888", "—").strip()


# ---------------------------------------------------------------------------
# The security review of ladislav-urban/OpenExecutive#165


def test_a_weekday_is_put_right_only_where_it_is_plainly_the_dates() -> None:
    from datetime import date

    from openexecutive.orchestrator import weekday_check

    today = date(2026, 11, 5)
    unchanged = [
        # Names that begin like a weekday.
        "Sun Pharma's invoice SINV-1 for $500 was due Nov 3 and is still unpaid.",
        "Thu Nguyen owes $250 on SINV-2, due Nov 10.",
        "Call Mon Health about PO-77 dated Nov 3.",
        "SAT prep invoice 2026-11-03",
        # A weekday in another clause, or far from the date.
        "The invoice dated Nov 3 is due; let's meet Friday.",
        "Quotation QTN-001 (Oct 30) expires soon; I'd chase them on Monday.",
        "Nov 3 is the date on the order that the supplier promised for delivery by Friday.",
        # Another line, code and links.
        "- Northgate quotation, valid till Nov 8\n  - Follow-up call set for Friday",
        "```json\n{\"date\": \"2026-11-08\", \"day\": \"Sat\"}\n```",
        "See https://erp.example.com/report?date=2026-11-08&day=Sat for it.",
        # Another event's day near the date (re-review of #165).
        "SINV-2 is due Nov 10, I will chase on Friday.",
        "Due Nov 10, follow up by Friday.",
        "Due Nov 10, pay by this Friday.",
        "The quote expires Nov 8, so call Monday.",
        "Invoice SINV-1 (Nov 3) is overdue, chase them Friday.",
        "Nov 8 delivery slipped to Friday.",
        "Payment for Nov 3 came in Monday.",
    ]
    for text in unchanged:
        assert weekday_check.fix(text, today) == (text, 0), text
    assert weekday_check.fix("Due Saturday, Nov 8.", today) == ("Due Sunday, Nov 8.", 1)
    assert weekday_check.fix("Due Nov 8 (Sat).", today) == ("Due Nov 8 (Sun).", 1)
    assert weekday_check.fix("Due Nov 8, which is a Saturday.", today) == ("Due Nov 8, which is a Sunday.", 1)


def test_a_count_of_none_or_one_is_never_several(monkeypatch: pytest.MonkeyPatch) -> None:
    _reviews(monkeypatch, CANNOT_TELL, CANNOT_TELL)
    _rewrites(monkeypatch, None)
    out = _check("Birch Co is owed 52 breakers. 0 overdue invoices remain.")
    assert "several" not in out.lower() and out.startswith("Birch Co is owed 52 breakers.")


def test_a_group_or_a_text_value_never_confirms_another_records_count() -> None:
    # Birch Co has two orders: that counts orders, not customers.
    assert _statuses("Two customers are waiting.") == {"Two": "unconfirmed"}
    named = ErpnextResult(
        "erpnext__list_records",
        {"doctype": "Sales Order", "fields": ["name", "customer"]},
        {
            "doctype": "Sales Order",
            "rows": [{"name": f"SO-{i}", "customer": "Invoice Masters Ltd"} for i in range(3)]
            + [{"name": "SO-9", "customer": "Alder Works"}],
            "truncated": False,
        },
    )
    fmt = NumberFormat.parse(None)
    got = {f.text: f.status for f in check_figures("Three invoices are overdue.", sources_for([named], fmt=fmt), fmt)}
    assert got == {"Three": "unconfirmed"}
    # "per invoice" is a rate, not a count of invoices.
    assert _statuses("Birch Co took 52 units per invoice.") == {"52": "confirmed"}


def test_a_short_id_with_a_leading_zero_is_never_a_count() -> None:
    # The pilot's Nov 6, 2026 check: "so 03469 bills goods we already have"
    # (a short invoice id) came out "so several bills goods…".
    from openexecutive.orchestrator.figure_check import find_figures

    assert find_figures("So 03469 bills goods we already have; hold 03469 or pay it.") == []
    assert _statuses("Seven customers are waiting.") == {"Seven": "unconfirmed"}

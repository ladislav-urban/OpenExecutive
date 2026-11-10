"""A correction check 2 names is held to the figure's own kind: a number of
days or a count is never written from money columns' values, and no value
more than three times off is written in (a demo reshoot: "averaging 62.5
days" became a mean of paid amounts). Made-up names and figures."""

from __future__ import annotations

from typing import Any

from openexecutive.orchestrator import answer_review
from openexecutive.orchestrator.answer_review import CANNOT_TELL, Review
from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    NumberFormat,
    check_figures,
    sources_for,
)

FMT = NumberFormat.parse(None)

# ---------------------------------------------------------------------------
# A figure that isn't money is never "corrected" to a money column's value

PAYMENTS = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Payment Entry"},
    {
        "doctype": "Payment Entry",
        "rows": [
            {"name": "PE-0001", "party": "Larch Joinery", "paid_amount": 1200.4, "days_to_pay": 80.0},
            {"name": "PE-0002", "party": "Larch Joinery", "paid_amount": 1801.2, "days_to_pay": 91.0},
        ],
    },
)
FEES = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Journal Entry"},
    {"doctype": "Journal Entry", "rows": [{"name": "JV-1", "fee_amount": 120.0}, {"name": "JV-2", "fee_amount": 131.0}]},
)


def _flag(text: str, figure: str, results: list[ErpnextResult]) -> Any:
    src = sources_for(results, fmt=FMT)
    fig = next(f for f in check_figures(text, src, FMT) if f.text == figure)
    assert fig.flagged
    return fig, src


def test_a_days_figure_is_never_corrected_to_a_money_average() -> None:
    fig, src = _flag("Larch Joinery pays late, averaging 84.7 days.", "84.7", [PAYMENTS, FEES])
    money = {"verdict": "wrong", "op": "average", "operands": [1200.4, 1801.2]}
    assert answer_review.read_verdict(money, fig, src) == CANNOT_TELL
    # Within three times of it, but a money column all the same.
    fees = {"verdict": "wrong", "op": "average", "operands": [120.0, 131.0]}
    assert answer_review.read_verdict(fees, fig, src) == CANNOT_TELL
    # The days column's own average is still written in.
    days = {"verdict": "wrong", "op": "average", "operands": [80, 91]}
    assert answer_review.read_verdict(days, fig, src) == Review("wrong", 85.5)


def test_a_count_is_never_corrected_to_a_money_value() -> None:
    fig, src = _flag("Larch Joinery made 13 payments.", "13", [PAYMENTS, FEES])
    money = {"verdict": "wrong", "op": "difference", "operands": [131.0, 120.0]}
    assert answer_review.read_verdict(money, fig, src) == CANNOT_TELL


def test_a_correction_more_than_three_times_off_is_refused() -> None:
    fig, src = _flag("Larch Joinery paid $350.00 last.", "$350.00", [PAYMENTS])
    far = {"verdict": "wrong", "op": "value", "operands": [1200.4]}
    assert answer_review.read_verdict(far, fig, src) == CANNOT_TELL
    fig, src = _flag("Larch Joinery paid $1,500.00 last.", "$1,500.00", [PAYMENTS])
    near = {"verdict": "wrong", "op": "value", "operands": [1801.2]}
    assert answer_review.read_verdict(near, fig, src) == Review("wrong", 1801.2)



"""A figure the reply works out from figures it shows ("5 × $71.00 = $355.00")
is held to that arithmetic, not to the records, which hold no such product;
and one the arithmetic gives another value for is written in by code (the
pilot's Nov 6, 2026 rerun of try-invoice-mismatch: "5 × $71.00 = $5.00" and
"13 × ($73.84 − $71.00) = 13 × $2.84." with its result dropped). Made-up
figures."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check
from openexecutive.orchestrator.answer_review import CANNOT_TELL
from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    NumberFormat,
    check_figures,
    own_arithmetic,
    sources_for,
)

from .test_answer_check import _reviews, _rewrites, audit_rows  # noqa: F401

INVOICE = ErpnextResult(
    "erpnext__get_record",
    {"doctype": "Purchase Invoice", "name": "PINV-1"},
    {
        "doctype": "Purchase Invoice", "name": "PINV-1",
        "record": {"grand_total": 3001.12, "items": [
            {"item_code": "REEL-100", "qty": 18.0, "rate": 73.84, "amount": 1329.12},
        ]},
    },
)
ORDER = ErpnextResult(
    "erpnext__get_record",
    {"doctype": "Purchase Order", "name": "PO-1"},
    {"doctype": "Purchase Order", "name": "PO-1", "record": {"items": [
        {"item_code": "REEL-100", "qty": 13.0, "rate": 71.0, "amount": 923.0},
    ]}},
)


def _statuses(text: str) -> dict[str, str]:
    fmt = NumberFormat.parse(None)
    return {f.text: f.status for f in check_figures(text, sources_for([INVOICE, ORDER], fmt=fmt), fmt)}


def _worked(text: str, fmt: NumberFormat | None = None) -> tuple[float, ...] | None:
    got = own_arithmetic(text, text.rindex("=") + 2, fmt or NumberFormat.parse(None), frozenset({"USD", "EUR"}))
    return got[0] if got else None


def test_the_arithmetic_before_an_equals_sign_is_worked_out() -> None:
    assert _worked("5 × $71.00 = $355.00") == (355.0,)
    assert _worked("Price: 13 × ($73.84 − $71.00) = $36.92") == pytest.approx((36.92,))
    assert _worked("$1,329.12 − $923.00 = $406.12") == pytest.approx((406.12,))
    assert _worked("5 × USD 71.00 = USD 355.00") == (355.0,)
    # Words, or no operator, are not arithmetic to work out.
    assert _worked("The total is = $5.00") is None
    assert _worked("Quantity: 5 extra reels = $355") is None


def test_a_list_marker_is_no_minus_sign() -> None:
    assert _worked("- $1,329.12 − $923.00 = $406.12") == pytest.approx((406.12,))
    assert _worked("* 5 × $71.00 = $355.00") == (355.0,)
    assert _worked("1. 5 × $71.00 = $355.00") == (355.0,)
    # An expression may not open with an operator.
    assert _worked("Net: −$923.00 + $1,329.12 = $406.12") is None


def test_a_record_name_is_never_worked_out() -> None:
    assert _worked("PUR-ORD-2026-00001 = $923.50") is None
    assert _worked("Order PO-2026-1 = $923.50") is None
    assert _worked("ABC 5 × 3 = 15") is None


def test_both_readings_of_a_number_are_worked_out() -> None:
    # A site that writes 1.234,56: "$71.00" reads 7100 there and 71 plainly.
    eu = NumberFormat.parse("#.###,##")
    assert _worked("5 × $1.329 = $6.645", eu) == pytest.approx((6645.0, 6.645))
    assert _worked("5 × 71,00 € = 355,00 €", eu) == (355.0,)


def test_a_wrong_result_on_a_site_with_another_format_is_left_to_the_records() -> None:
    eu = NumberFormat.parse("#.###,##")
    statuses = {
        f.text: (f.status, f.arithmetic)
        for f in check_figures("Quantity: 5 × $1.329 = $5.00.", sources_for([INVOICE, ORDER], fmt=eu), eu)
    }
    # "$1.329" is 1329 there and 1.329 plainly: two values, none written in.
    assert statuses["$5.00"][1] is None


def test_an_operand_the_records_dont_hold_stops_the_arithmetic() -> None:
    got = {
        f.text: (f.status, f.arithmetic)
        for f in check_figures(
            "Quantity: 5 × $9,999.00 = $5.00.",
            sources_for([INVOICE, ORDER], fmt=NumberFormat.parse(None)),
            NumberFormat.parse(None),
        )
    }
    assert got["$5.00"][1] is None and got["$5.00"][0] != "confirmed"


def test_a_product_the_reply_works_out_is_confirmed_by_its_arithmetic() -> None:
    got = _statuses("Quantity: 5 × $71.00 = $355.00. Price: 13 × ($73.84 − $71.00) = $36.92.")
    assert got["$355.00"] == "confirmed" and got["$36.92"] == "confirmed"


def test_a_wrong_result_is_written_in_from_the_arithmetic_without_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[Any] = []

    async def review(**kw: Any) -> list[Any]:
        called.append(kw)
        return [CANNOT_TELL] * len(kw["flagged"])

    monkeypatch.setattr("openexecutive.orchestrator.answer_review.review", review)
    _rewrites(monkeypatch, None)
    turn = answer_check.AnswerCheckTurn()
    turn.results.extend([INVOICE, ORDER])
    turn._context = {"number_format": "#,###.##"}
    out = asyncio.run(answer_check.check_reply(
        "Quantity: 5 × $71.00 = $5.00 for reels never received.", turn, question="q", model="m",
    ))
    assert out == "Quantity: 5 × $71.00 = $355.00 for reels never received."
    assert called == []

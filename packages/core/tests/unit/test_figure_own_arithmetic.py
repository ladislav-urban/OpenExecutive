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
    return tuple(value for value, _ in got.readings) if got else None


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
    assert _worked("Two lines differ.\n- 5 × $71.00 = $355.00") == (355.0,)
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


def _check(text: str, fmt: NumberFormat | None = None, results: list[ErpnextResult] | None = None) -> dict[str, Any]:
    fmt = fmt or NumberFormat.parse(None)
    return {f.text: f for f in check_figures(text, sources_for(results or [INVOICE, ORDER], fmt=fmt), fmt)}


def test_a_bulleted_product_on_a_later_line_is_written_in() -> None:
    got = _check("Two lines differ.\n- Quantity: 5 × $71.00 = $5.00 never received.")
    assert got["$5.00"].arithmetic == 355.0


def test_a_chain_is_worked_out_when_every_step_agrees() -> None:
    got = _check("Price: 13 × ($73.84 − $71.00) = 13 × $2.84 = $36.92.")
    assert got["$36.92"].status == "confirmed"
    wrong = _check("Price: 13 × ($73.84 − $71.00) = 13 × $2.84 = $3.92.")
    assert wrong["$3.92"].arithmetic == pytest.approx(36.92)
    # A step that disagrees leaves the result to the records.
    broken = _check("Price: 13 × ($73.84 − $71.00) = 13 × $2.48 = $3.92.")
    assert broken["$3.92"].arithmetic is None


def test_a_percentage_is_never_written_from_a_quotient() -> None:
    right = _check("Overbilling: ($1,329.12 − $923.00) ÷ $923.00 = 44% over the order.")
    assert right["44%"].status == "confirmed"
    wrong = _check("Overbilling: ($1,329.12 − $923.00) ÷ $923.00 = 40% over the order.")
    assert wrong["40%"].arithmetic is None


@pytest.mark.parametrize("text", [
    "Period: 2026-10-01 – 2026-10-31 = $12,345.00 billed.",
    "Bucket: 31–60 = $5,000.00 outstanding.",
    "Ref: 2026/00001 = $5.00 due.",
    "Due: 01/10/2026 – 31/10/2026 = $12,345.00 billed.",
    "Total: 2026 − $923.00 = $5.00 due.",
])
def test_dates_ranges_and_ids_are_never_worked_out(text: str) -> None:
    got = _check(text)
    assert all(f.arithmetic is None and f.how != "the reply's own arithmetic" for f in got.values())


def test_a_figure_in_the_wrong_currency_keeps_that_flag() -> None:
    order = ErpnextResult(
        "erpnext__get_record",
        {"doctype": "Purchase Order", "name": "PO-1"},
        {
            "doctype": "Purchase Order", "name": "PO-1",
            "company_currency": "USD", "document_currency": "EUR",
            "record": {"grand_total": 923.0, "base_grand_total": 978.38, "items": [
                {"item_code": "REEL-100", "qty": 13.0, "rate": 71.0, "amount": 923.0},
            ]},
        },
    )
    src = sources_for([order], currencies={"USD": {"symbol": "$"}, "EUR": {"symbol": "€"}})
    (plain,) = check_figures("Total: $923.00 for the order.", src)
    assert plain.status == "wrong_currency"
    got = {f.text: f for f in check_figures("Total: 13 × €71.00 = $923.00 for the order.", src)}
    assert got["$923.00"].status == "wrong_currency" and got["$923.00"].currency_fix == "€"


def test_numbers_read_as_their_figures_were_held() -> None:
    # On a site that writes 1.234,56, "$1.329" holds 1329; "$71.00" only 71.
    eu = NumberFormat.parse("#.###,##")
    invoice = ErpnextResult(
        "erpnext__get_record",
        {"doctype": "Purchase Invoice", "name": "PINV-2"},
        {"doctype": "Purchase Invoice", "name": "PINV-2", "record": {"items": [
            {"item_code": "A", "qty": 1.0, "rate": 1329.0, "amount": 1329.0},
            {"item_code": "B", "qty": 1.0, "rate": 71.0, "amount": 71.0},
        ]}},
    )
    got = _check("Total: 5 × $1.329 + $71.00 = $5.00 due.", eu, [invoice])
    assert got["$5.00"].arithmetic in (None, 6716.0)


@pytest.mark.parametrize("text", [
    "Net: - 923.00 + 1,329.12 + 71.00 = 477.12 owed.",
    "Owed, – 923.00 + 1,329.12 + 71.00 = 477.12 owed.",
    "Net: - $923.00 + $1,329.12 + $71.00 = $477.12 owed.",
])
def test_a_dash_after_a_colon_or_comma_is_a_sign_and_never_dropped(text: str) -> None:
    assert all(f.arithmetic is None for f in _check(text).values())


def test_a_figure_the_records_dont_hold_never_passes_as_a_quantity() -> None:
    wrong = _check("Owed: $500 − $71.00 = $5.00 after the credit.")
    assert wrong["$5.00"].arithmetic is None
    right = _check("Owed: $500 − $71.00 = $429.00 after the credit.")
    assert right["$429.00"].how != "the reply's own arithmetic"


@pytest.mark.parametrize(("text", "fmt", "result", "right"), [
    ("Net: $1,329.12 − $923.00 = $5.00.", None, "$5.00", 406.12),
    ("Net: USD 1,329.12 − USD 923.00 = USD 5.00.", None, "USD 5.00", 406.12),
    ("Net: 5 × 71.00 USD = 5.00 USD.", None, "5.00 USD", 355.0),
    ("Net: 5 × 71,00 € = 5,00 €.", "#.###,##", "5,00 €", 355.0),
])
def test_a_mark_at_the_expressions_edge_still_makes_its_figure_an_operand(
    text: str, fmt: str | None, result: str, right: float,
) -> None:
    got = _check(text, NumberFormat.parse(fmt) if fmt else None)
    assert got[result].arithmetic == pytest.approx(right)

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


def test_the_arithmetic_before_an_equals_sign_is_worked_out() -> None:
    assert own_arithmetic("5 × $71.00 = $355.00", 13) == 355.0
    assert own_arithmetic("Price: 13 × ($73.84 − $71.00) = $36.92", 31) == pytest.approx(36.92)
    assert own_arithmetic("$1,329.12 − $923.00 = $406.12", 22) == pytest.approx(406.12)
    # Words, or no operator, are not arithmetic to work out.
    assert own_arithmetic("The total is = $5.00", 15) is None
    assert own_arithmetic("Quantity: 5 extra reels = $355", 25) is None


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

"""A table column that counts time is not held to ERPNext's figures (the
pilot's Nov 6, 2026 retake, where-is-my-order step 3: Cedar & Pine's "Days
late" cell, 184 days from May 6, was blanked; the six small ones matched
other values by chance). Made-up orders."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check, answer_review
from openexecutive.orchestrator.answer_review import Review
from openexecutive.orchestrator.figure_check import ErpnextResult, find_figures

ORDERS = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Sales Order", "fields": ["name", "customer", "delivery_date", "per_delivered"]},
    {
        "doctype": "Sales Order",
        "rows": [
            {"name": "SO-1", "customer": "Cedar & Pine Interiors", "delivery_date": "2026-05-06", "per_delivered": 90.0},
            {"name": "SO-2", "customer": "Brightline Builders", "delivery_date": "2026-11-04", "per_delivered": 89.0},
        ],
        "truncated": False,
    },
)

TABLE = (
    "Two open orders are past the date we promised, as of Nov 6:\n\n"
    "| Customer | Promised | Days late | Delivered |\n"
    "|---|---|---|---|\n"
    "| Cedar & Pine Interiors | May 6 | 184 | 90% |\n"
    "| Brightline Builders | Nov 4 | 2 | 89% |\n"
)


def test_a_cell_under_a_time_column_is_not_a_figure() -> None:
    texts = [f.text for f in find_figures(TABLE)]
    assert "184" not in texts and "2" not in texts
    assert "90%" in texts and "89%" in texts
    # Any other column's whole number still is (a quantity is held to the reads).
    qty = "| Item | Qty |\n|---|---|\n| LED panel | 162 |\n"
    assert [f.text for f in find_figures(qty)] == ["162"]
    for header in ("Hours", "Weeks overdue", "Age (days)"):
        table = f"| Order | {header} |\n|---|---|\n| SO-1 | 184 |\n"
        assert find_figures(table) == [], header


def test_the_reply_keeps_its_days_late(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    flagged: list[Any] = []

    async def review(**kw: Any) -> list[Review]:
        flagged.extend(kw["flagged"])
        return [answer_review.CANNOT_TELL] * len(kw["flagged"])

    monkeypatch.setattr(answer_review, "review", review)
    turn = answer_check.AnswerCheckTurn()
    turn.results.append(ORDERS)
    turn._context = {"number_format": "#,###.##"}
    assert asyncio.run(answer_check.check_reply(TABLE, turn, question="q", model="m")) == TABLE
    assert flagged == []

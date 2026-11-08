"""A table the answer check would leave with its total row alone goes whole
(the pilot's Nov 6, 2026 retake, denver-branch step 2: the cost table kept
only "Total | | $22,691.60" once its rows' suppliers, read from GL entries'
text, were left out). Made-up project and suppliers."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check, answer_correct, answer_review
from openexecutive.orchestrator.answer_review import Review
from openexecutive.orchestrator.figure_check import ErpnextResult

PROJECT = ErpnextResult(
    "erpnext__get_record",
    {"doctype": "Project", "name": "PROJ-0192"},
    {"doctype": "Project", "name": "PROJ-0192", "currency": "USD",
     "record": {"project_name": "Denver branch opening", "total_purchase_cost": 22200.0,
                "total_costing_amount": 22691.6}},
)
GL = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "GL Entry", "fields": ["account", "debit", "against", "project"]},
    {
        "doctype": "GL Entry", "currency": "USD",
        "rows": [
            {"account": "Furniture and Fixtures", "debit": 18400.0, "against": "Northfield Racking", "project": "PROJ-0192"},
            {"account": "Furniture and Fixtures", "debit": 2350.0, "against": "Front Street Shopfitters", "project": "PROJ-0192"},
            {"account": "Computer Equipment", "debit": 1450.0, "against": "Rivers IT Supply", "project": "PROJ-0192"},
            {"account": "Furniture and Fixtures", "debit": 491.6, "against": "Stores", "project": "PROJ-0192"},
        ],
        "truncated": False,
    },
)

REPLY = (
    "Opening Denver cost $22,691.60, all of it booked as fixed assets.\n\n"
    "| Item | Supplier | Amount |\n"
    "|---|---|---|\n"
    "| Pallet racking | Northfield Racking | $18,400.00 |\n"
    "| Display fixtures | Front Street Shopfitters | $2,350.00 |\n"
    "| Laptop | Rivers IT Supply | $1,450.00 |\n"
    "| **Total** | | **$22,691.60** |\n\n"
    "- **Cash out:** $22,200 (18,400 + 2,350 + 1,450), every bill paid.\n"
    "- **Stock used:** $491.60 from the warehouse, not a purchase."
)


def _check(text: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)

    async def review(**kw: Any) -> list[Review]:
        return [answer_review.CANNOT_TELL] * len(kw["flagged"])

    async def no_rewrite(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(answer_review, "review", review)
    monkeypatch.setattr(answer_check, "_rewrite", no_rewrite)
    turn = answer_check.AnswerCheckTurn()
    turn.results.extend([PROJECT, GL])
    turn._context = {"number_format": "#,###.##"}
    return asyncio.run(answer_check.check_reply(text, turn, question="What did opening it cost us?", model="m"))


def test_a_table_left_with_its_total_alone_goes_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _check(REPLY, monkeypatch)
    assert out != answer_correct.WHOLE_REPLY
    assert "|" not in out and "Total" not in out  # no table holding a total alone
    assert out.startswith("Opening Denver cost $22,691.60")
    assert "- **Cash out:** $22,200 (18,400 + 2,350 + 1,450), every bill paid." in out


def test_a_table_keeping_any_of_its_rows_keeps_its_total(monkeypatch: pytest.MonkeyPatch) -> None:
    # A row whose names ERPNext gives (an account, a Link-field party) stays,
    # and so does the table with its total.
    gl = ErpnextResult(GL.tool, GL.arguments, {**GL.payload, "rows": [
        {**GL.payload["rows"][0], "party": "Northfield Racking"}, *GL.payload["rows"][1:],
    ]})
    reply = REPLY.replace("| Pallet racking | Northfield Racking |", "| Furniture and Fixtures | Northfield Racking |")
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)

    async def review(**kw: Any) -> list[Review]:
        return [answer_review.CANNOT_TELL] * len(kw["flagged"])

    async def no_rewrite(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(answer_review, "review", review)
    monkeypatch.setattr(answer_check, "_rewrite", no_rewrite)
    turn = answer_check.AnswerCheckTurn()
    turn.results.extend([PROJECT, gl])
    turn._context = {"number_format": "#,###.##"}
    out = asyncio.run(answer_check.check_reply(reply, turn, question="q", model="m"))
    assert "| Furniture and Fixtures | Northfield Racking | $18,400.00 |" in out
    assert "**Total**" in out and "Laptop" not in out

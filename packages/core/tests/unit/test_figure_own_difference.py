"""An opening figure the reply's own table total and stated difference
contradict ("costs $X against a BOM of $Y, $D under the BOM", beside a table
whose Total gives Y - D) is written from that total (a demo reshoot). Made-up
names and figures."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check
from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    NumberFormat,
    check_figures,
    sources_for,
)

from .test_answer_check import _reviews, _rewrites

FMT = NumberFormat.parse(None)

# ---------------------------------------------------------------------------
# An opening figure its own table total and stated difference contradict

BOARD = ErpnextResult(
    "erpnext__get_record",
    {"doctype": "BOM", "name": "BOM-PANEL-7"},
    {
        "doctype": "BOM", "name": "BOM-PANEL-7",
        "record": {"total_cost": 180.31, "raw_material_cost": 188.4, "items": [
            {"item_code": "SHEET-2", "amount": 120.0},
            {"item_code": "BOLT-9", "amount": 72.16},
        ]},
    },
)
OTHER_BOM = ErpnextResult(
    "erpnext__get_record",
    {"doctype": "BOM", "name": "BOM-PANEL-6"},
    {"doctype": "BOM", "name": "BOM-PANEL-6", "record": {"total_cost": 174.9}},
)
TABLE = (
    "\n\n| Part | Cost |\n|---|---|\n| SHEET-2 | $120.00 |\n| BOLT-9 | $72.16 |\n| **Total** | **$192.16** |\n"
)


def _figures(text: str) -> dict[str, Any]:
    return {f.text: f for f in check_figures(text, sources_for([BOARD, OTHER_BOM], fmt=FMT), FMT)}


def test_an_opening_figure_its_own_table_contradicts_is_written_from_the_total() -> None:
    text = "Each panel costs $174.90 against a BOM of $188.40, so it comes in $3.76 a board under the BOM." + TABLE
    got = _figures(text)
    assert got["$174.90"].flagged and got["$174.90"].arithmetic == 192.16
    assert not got["$188.40"].flagged


def test_a_difference_that_matches_its_own_two_figures_is_left() -> None:
    text = "Each panel costs $192.16 against a BOM of $188.40, so it comes in $3.76 a board over the BOM." + TABLE
    got = _figures(text)
    assert not any(f.flagged for f in got.values())


def test_ordinary_comparisons_are_left_alone() -> None:
    for text in (
        # No table total to say which figure is off.
        "Each panel costs $174.90 against a BOM of $188.40, $3.76 under the BOM.",
        # "over" a period, not a difference.
        "Each panel costs $174.90 and the BOM $188.40, $3.76 over the next quarter." + TABLE,
        # Under a figure the sentence names next: not this rule's.
        "Each panel costs $174.90 against $188.40, $3.76 under $180.31." + TABLE,
    ):
        got = _figures(text)
        assert all(f.arithmetic is None for f in got.values()), text




def test_the_correction_writes_the_total_in(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "Each panel costs $174.90 against a BOM of $188.40, so it comes in $3.76 a board under the BOM." + TABLE
    fixed = text.replace("$174.90", "$192.16").replace("under", "over")
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    _reviews(monkeypatch)
    calls = _rewrites(monkeypatch, fixed)
    turn = answer_check.AnswerCheckTurn()
    turn.results.extend([BOARD, OTHER_BOM])
    turn._context = {"number_format": "#,###.##"}
    out = asyncio.run(answer_check.check_reply(text, turn, question="What does a panel cost?", model="m"))
    assert out == fixed
    assert calls == ['1. "$174.90" should be "$192.16".']


STAFF = ErpnextResult(
    "erpnext__run_report",
    {"report_name": "Cost Summary"},
    {"report_name": "Cost Summary", "rows": [
        {"line": "Sales", "amount": 84000.0},
        {"line": "Planned costs", "amount": 70000.0},
        {"line": "Labour", "amount": 41000.0},
        {"line": "Lease", "amount": 26000.0},
    ]},
)
STAFF_TABLE = "\n\n| Line | Actual |\n|---|---|\n| Labour | $41,000 |\n| Lease | $26,000 |\n| **Total** | **$67,000** |\n"


def _staff(text: str) -> dict[str, Any]:
    return {f.text: f for f in check_figures(text, sources_for([STAFF], fmt=FMT), FMT)}


def test_a_difference_in_another_sentence_is_not_about_the_figures_before_it() -> None:
    for text in (
        "Sales were $84,000 against costs of $70,000 planned. Costs came in $3,000 under plan.",
        "Sales were $84,000 and a year ago $70,000. Separately, lease ran $3,000 over plan.",
    ):
        got = _staff(text + STAFF_TABLE)
        assert all(f.arithmetic is None for f in got.values()), text


def test_a_difference_must_name_one_of_its_two_figures() -> None:
    # "under plan", but neither figure before it is the plan's.
    got = _staff("Sales were $84,000 and costs $70,000, so lease came in $3,000 under plan." + STAFF_TABLE)
    assert all(f.arithmetic is None for f in got.values())

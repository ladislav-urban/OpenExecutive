# ruff: noqa: F811 — pytest fixtures imported from test_answer_check share their names with the test parameters.
"""When the answer check replaces a whole reply (the pilot's Nov 5, 2026
second retake, OpenExecutive ac2b047b): a cash position whose every figure
passed was blanked for its names, and a 13-week outlook and a "what if Metro
pays late" were blanked for the projections no ERPNext result holds.

Made-up companies, customers and amounts, as in test_answer_check."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check, answer_correct
from openexecutive.orchestrator.answer_review import CANNOT_TELL, Review

from .test_answer_check import _reviews, _rewrites, _turn, audit_rows  # noqa: F401

# A cash position as the pilot's: every figure is ERPNext's, but the names in
# its table are not ones the check can place.
CASH_POSITION = (
    "Receivables come to $48,200.\n\n"
    "| Customer | Outstanding |\n"
    "|---|---|\n"
    "| Nobody Holdings | $12,500 |\n"
    "| Ghost Partners | $35,700 |\n"
)
# A week-by-week outlook: worked out from ERPNext's figures, held by none.
OUTLOOK = (
    "Receivables come to $48,200.\n\n"
    "| Week | Cash |\n"
    "|---|---|\n"
    "| Week 1 | $41,130 |\n"
    "| Week 2 | $37,560 |\n"
    "| Week 3 | $33,270 |\n"
)


def _ask(text: str, question: str, turn: answer_check.AnswerCheckTurn | None = None) -> str:
    return asyncio.run(answer_check.check_reply(text, turn or _turn(), question=question, model="m"))


def test_a_reply_whose_only_failures_are_names_stands(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any],
) -> None:
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    turn = _turn()
    assert _ask(CASH_POSITION, "What's our cash position this month, and what's due in?", turn) == CASH_POSITION
    assert not turn.whole_replaced
    details = audit_rows[0][2]
    assert details["whole_reply"] == 0 and details["kept"] == 1 and details["names_flagged"] == 2


@pytest.mark.parametrize(
    "question",
    [
        "How much cash do we have across all our accounts, and what will it look like week by week for the next 13 weeks?",
        "What if Metro pays as late as it usually does?",
    ],
)
def test_a_projection_stands_under_a_note_with_known_values_written_in(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any], question: str,
) -> None:
    # One figure ERPNext contradicts, with its right value; the rest unknown.
    _reviews(monkeypatch, Review("wrong", 12500.0), *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    turn = _turn()
    text = OUTLOOK.replace("$41,130", "$12,900")
    out = _ask(text, question, turn)
    assert not turn.whole_replaced
    assert out.endswith(answer_check.PROJECTION_NOTE)
    assert "$12,500" in out and "$12,900" not in out and "$37,560" in out
    assert audit_rows[0][2]["kept"] == 1 and audit_rows[0][2]["whole_reply"] == 0


def test_figures_that_fail_outside_a_projection_still_replace_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    turn = _turn()
    assert _ask(OUTLOOK, "How much cash do we have?", turn) == answer_correct.WHOLE_REPLY
    assert turn.whole_replaced


def test_the_projection_test_reads_the_question_not_the_reply() -> None:
    projection = answer_check._PROJECTION_Q
    assert projection.search("what will it look like week by week for the next 13 weeks")
    assert projection.search("What if Metro pays as late as it usually does?")
    assert projection.search("Show me the 13-week cash outlook")
    assert not projection.search("What's our cash position this month, and what's due in?")
    assert not projection.search("Who owes us the most?")
    # Lookups whose figures stay held to their results.
    assert not projection.search("How much have we spent on the Riverside project so far?")
    assert not projection.search("List our open projects with their budgets")
    assert not projection.search("Show me the budget vs actual for project PROJ-0001")
    assert not projection.search("How many projectors are in stock?")
    assert not projection.search("What would be our total overdue receivables right now?")
    assert not projection.search("Sync this to my Outlook")

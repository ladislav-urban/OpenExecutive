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


def _ask(
    text: str, question: str, turn: answer_check.AnswerCheckTurn | None = None, *, typed: bool = True,
) -> str:
    """Checks ``text`` as a reply to ``question``; ``typed``: the person typed
    it on that surface (``typed_question``), else it came from a card, a file
    or an email (``None``)."""
    return asyncio.run(answer_check.check_reply(
        text, turn or _turn(), question=question, model="m", typed_question=question if typed else None,
    ))


def test_a_reply_whose_only_failures_are_names_stands(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any],
) -> None:
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    turn = _turn()
    assert _ask(CASH_POSITION, "What's our cash position this month, and what's due in?", turn) == CASH_POSITION
    assert not turn.whole_replaced
    details = [r for r in audit_rows if r[0] == "answer_check"][0][2]
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


def test_figures_that_fail_outside_a_projection_show_a_dash_in_their_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not a projection: the figures no value is known for go, as "—" in their
    # cells (answer_correct.vague), and the table and reply stay.
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    turn = _turn()
    out = _ask(OUTLOOK, "How much cash do we have?", turn)
    assert not turn.whole_replaced and answer_check.PROJECTION_NOTE not in out
    assert "$41,130" not in out and "| Week 1 | — |" in out and "$48,200" in out


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


def test_correct_keep_never_replaces_a_reply_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    # Decided Nov 5, 2026: figures put right in place, the reply kept.
    monkeypatch.setenv("ERPNEXT_ANSWER_CHECK", "correct_keep")
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    turn = _turn()
    text = "Receivables are $77,777. Most of it is South's."
    assert _ask(text, "Who owes us?", turn) == text
    assert not turn.whole_replaced
    # A known right value is still written in.
    _reviews(monkeypatch, Review("wrong", 48200.0))
    assert _ask(text, "Who owes us?", _turn()) == "Receivables are $48,200. Most of it is South's."
    monkeypatch.setenv("ERPNEXT_ANSWER_CHECK", "correct")
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    assert _ask(text, "Who owes us?", _turn()) == answer_correct.WHOLE_REPLY


@pytest.mark.parametrize(
    "question",
    [
        # A card's headline: an inbound email's subject.
        "Updated forecast for Q1",
        # A web upload's memory line.
        "totals?\n\n(Attached files: Q1 forecast.pdf)",
        # A file's extracted text in a chat app's message.
        "[Attached: Q1 forecast.pdf]\nWhat if Metro pays late?\n\ntotals?",
    ],
)
def test_a_projection_word_not_typed_by_the_person_never_keeps_figures(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any], question: str,
) -> None:
    # The same outlook as above, asked by text the person did not type: its
    # figures no result holds go, as for any lookup.
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    out = _ask(OUTLOOK, question, _turn(), typed=False)
    assert answer_check.PROJECTION_NOTE not in out
    assert "$41,130" not in out and "| Week 1 | — |" in out


def test_only_typed_question_relaxes_and_question_stays_the_models_context(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any],
) -> None:
    seen: list[str] = []

    async def review(*, question: str, flagged: list[Any], **_: Any) -> list[Any]:
        seen.append(question)
        return [CANNOT_TELL] * len(flagged)

    monkeypatch.setattr("openexecutive.orchestrator.answer_review.review", review)
    _rewrites(monkeypatch, None)
    out = asyncio.run(answer_check.check_reply(
        OUTLOOK, _turn(), question="Updated forecast for Q1", model="m", typed_question="totals?",
    ))
    assert answer_check.PROJECTION_NOTE not in out
    assert seen == ["Updated forecast for Q1"]
    assert not answer_check._projection(None) and not answer_check._projection("")
    assert answer_check._projection("What if Metro pays late?")

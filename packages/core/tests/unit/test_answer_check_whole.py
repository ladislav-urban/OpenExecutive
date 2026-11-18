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


OUTLOOK_Q = (
    "How much cash do we have across all our accounts, and what will it look like "
    "week by week for the next 13 weeks?"
)
TIGHTEST_Q = "Which week is the tightest, and what does cash end at that week?"


@pytest.fixture(autouse=True)
def _no_askers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(answer_check, "_last_asked", type(answer_check._last_asked)())


def _asked(own_words: str | None, *, asker: str = "person:7", session: str = "s1", now: float = 0.0) -> bool:
    """One turn's start, as the Executive notes it: whether it follows the
    asker's own projection."""
    turn = _turn()
    answer_check.note_question(turn, session_id=session, asker=asker, own_words=own_words, now=now)
    return turn.follows_projection


def test_a_follow_up_to_an_outlook_stands_under_the_note(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any],
) -> None:
    # Eval erpnext_099 (Nov 18, 2026): asked after a 13-week outlook, the
    # tightest week's balance is the outlook's own; held to ERPNext's results
    # it was left out and the table's "Ends at" cells went blank.
    assert not answer_check._PROJECTION_Q.search(TIGHTEST_Q)
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    _asked(OUTLOOK_Q)
    turn = _turn()
    answer_check.note_question(turn, session_id="s1", asker="person:7", own_words=TIGHTEST_Q, now=60.0)
    assert turn.follows_projection
    out = _ask(OUTLOOK, TIGHTEST_Q, turn)
    assert not turn.whole_replaced
    assert "$41,130" in out and "| Week 1 | — |" not in out
    assert out.endswith(answer_check.PROJECTION_NOTE)


def test_only_the_askers_own_words_count_never_the_page_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # Security review (Nov 18, 2026): the message carries the page block, so
    # a history row can name "Stock Projected Qty", a Lead called "Forecast
    # Systems Ltd" or a filter value "what if". The record reads the
    # person's words alone, so none of it relaxes the next lookup.
    for page in (
        "Open in ERPNext: the Stock Projected Qty report.",
        "Open in ERPNext: Lead Forecast Systems Ltd.",
        "Filters on screen: notes like what if",
        "Home card in focus: Cash forecast for the next 30 days",
    ):
        assert answer_check._PROJECTION_Q.search(page)
        assert not _asked("Which items are short?", session=page)
        assert not _asked("How much cash do we have?", session=page, now=60.0)
    # And the check holds the lookup to its results: dashes, no note.
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    _asked("Which items are short?", session="s2")
    turn = _turn()
    answer_check.note_question(turn, session_id="s2", asker="person:7", own_words="How much cash do we have?", now=1.0)
    out = _ask(OUTLOOK, "How much cash do we have?", turn)
    assert answer_check.PROJECTION_NOTE not in out and "| Week 1 | — |" in out


def test_an_upload_names_no_projection() -> None:
    # The upload turn's own words carry the files' names; the files' text is
    # only in the message. Neither makes the turn a projection.
    assert not _asked("Summarise this.

(Attached files: next 30 days plan.pdf)")
    assert not _asked("How much cash do we have?", now=60.0)


def test_another_persons_projection_in_a_shared_chat_does_not_count() -> None:
    _asked("[Dana]: what if Metro pays late?", asker="person:9")
    assert _asked(OUTLOOK_Q, asker="person:9") is False
    assert not _asked(TIGHTEST_Q, asker="person:7", now=60.0)
    # Dana's own follow-up does.
    assert _asked(TIGHTEST_Q, asker="person:9", now=60.0)


def test_only_the_askers_turn_just_before_counts() -> None:
    _asked(OUTLOOK_Q)
    assert _asked("Who owes us the most?", now=10.0)
    assert not _asked("How much cash do we have?", now=20.0)
    # A turn without the person's words (none given) or with blank ones ends it too.
    _asked(OUTLOOK_Q, now=30.0)
    assert _asked(None, now=40.0)
    assert not _asked(TIGHTEST_Q, now=50.0)
    _asked(OUTLOOK_Q, now=60.0)
    assert _asked("   ", now=70.0)
    assert not _asked(TIGHTEST_Q, now=80.0)


def test_an_outlook_relaxes_nothing_after_the_window() -> None:
    _asked(OUTLOOK_Q)
    assert not _asked(TIGHTEST_Q, now=answer_check.FOLLOW_UP_WINDOW_S + 1.0)
    _asked(OUTLOOK_Q, now=0.0, session="s3")
    assert _asked(TIGHTEST_Q, now=answer_check.FOLLOW_UP_WINDOW_S - 1.0, session="s3")


def test_the_executive_keys_the_record_by_session_and_asker() -> None:
    from openexecutive.orchestrator.executive import _note_check_question
    from openexecutive.orchestrator.session import Session

    session = Session(session_id="k1", erpnext_panel_email="ana@example.test")
    _note_check_question(_turn(), session, None, OUTLOOK_Q)
    assert ("k1", "panel:ana@example.test") in answer_check._last_asked
    # Someone else in the same session: not their projection.
    other = _turn()
    _note_check_question(other, session, 7, TIGHTEST_Q)
    assert not other.follows_projection
    own = _turn()
    _note_check_question(own, session, None, TIGHTEST_Q)
    assert own.follows_projection


def test_the_record_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(answer_check, "_MAX_ASKERS", 3)
    for n in range(10):
        _asked(OUTLOOK_Q, session=f"bound{n}")
    assert len(answer_check._last_asked) == 3


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

"""The two ways to open one Home card by id follow the caller's own Home
(today._cards_for_caller): a host turn's card lookup (chat._host_card) and the
card page (GET /alerts/{id}/card). A card routed to the principal, or a
decision-backed card that is not theirs, is not found for a teammate or a demo
person; the principal and the card's own person still get it.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from openexecutive.alerts import store as alerts_store
from openexecutive.memory import decision_ledger

from .principal_gate_support import app_client, call, make_roster


def _alert(headline: str, routed: int | None, tags: list[str] | None = None) -> int:
    return alerts_store.insert_alert(
        source="test", external_id=headline, severity="high", headline=headline,
        body=f"Body of {headline}", routed_to_person_id=routed, topic_tags=tags,
    )


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from openexecutive.api.routes import today as today_route
    from openexecutive.briefing import narrative_cache
    from openexecutive.people import insights_cache
    from openexecutive.workflows import persistence as wf_persistence

    ids = make_roster(tmp_path, monkeypatch)
    db = Path("./episodic_memory.db").resolve()
    for module in (wf_persistence, insights_cache, narrative_cache):
        monkeypatch.setattr(module, "DB_PATH", db)
    alerts_store.initialize_db()
    wf_persistence.initialize_runs_db(db)
    insights_cache.initialize_db(db)
    narrative_cache.initialize_db(db)
    monkeypatch.setattr(today_route, "_regen_briefing_narrative", AsyncMock())
    monkeypatch.setattr(today_route, "_regen_stale_insights", AsyncMock())
    instance = decision_ledger.create_decision_instance(
        decision_class="meeting_scheduling", department="", originating_session_id=None,
        proposed_payload={"title": "Lunch with Acme"}, idempotency_key=None, gate_mode="propose",
        approver_person_id=ids["principal"], confidence=None,
    )
    return {
        **ids,
        "principals": _alert("Sign the lease", ids["principal"]),
        "bens": _alert("Order Ben's laptop", ids["teammate"]),
        "danas": _alert("Dana's trial quote", ids["demo"]),
        "decision": _alert("Lunch with Acme", None, [f"{decision_ledger.DECISION_INSTANCE_TAG_PREFIX}{instance}"]),
    }


def _host_sees(card: int, person: int) -> bool:
    from openexecutive.api.routes.chat import _host_card

    try:
        _host_card(card, person)
    except HTTPException as exc:
        assert exc.status_code == 404
        return False
    return True


@pytest.mark.parametrize("who", ["teammate", "demo"])
def test_a_host_turn_never_opens_a_card_that_is_not_theirs(board: dict[str, int], who: str) -> None:
    assert not _host_sees(board["principals"], board[who])
    assert not _host_sees(board["decision"], board[who])
    other = "danas" if who == "teammate" else "bens"
    assert not _host_sees(board[other], board[who])


@pytest.mark.parametrize(("who", "card"), [("teammate", "bens"), ("demo", "danas")])
def test_a_host_turn_still_opens_their_own_card(board: dict[str, int], who: str, card: str) -> None:
    assert _host_sees(board[card], board[who])


@pytest.mark.parametrize("who", ["teammate", "demo"])
def test_the_card_page_never_opens_a_card_that_is_not_theirs(
    board: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client = app_client(monkeypatch, signed=True)
    for card in ("principals", "decision"):
        r = call(client, True, who, "GET", f"/alerts/{board[card]}/card")
        assert r.status_code == 404, (card, r.text)
        assert "Body of" not in r.text


@pytest.mark.parametrize(("who", "card"), [("teammate", "bens"), ("demo", "danas"),
                                           ("principal", "principals"), ("principal", "decision")])
def test_the_card_page_opens_for_the_principal_and_the_cards_own_person(
    board: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str, card: str
) -> None:
    client = app_client(monkeypatch, signed=True)
    r = call(client, True, who, "GET", f"/alerts/{board[card]}/card")
    assert r.status_code == 200, r.text


def test_the_principal_opens_any_card_but_a_private_one_through_a_host_turn(board: dict[str, int]) -> None:
    for card in ("principals", "bens", "danas", "decision"):
        assert _host_sees(board[card], board["principal"]), card
    private = _alert("A private matter", board["principal"], ["private:principal"])
    assert not _host_sees(private, board["principal"])


def test_a_caller_who_resolves_to_no_one_opens_no_card_through_a_host_turn(board: dict[str, int]) -> None:
    from openexecutive.api.routes.chat import _host_card

    for card in ("principals", "bens", "danas", "decision"):
        with pytest.raises(HTTPException) as exc:
            _host_card(board[card], None)
        assert exc.value.status_code == 404


HEADLINES = {
    "principals": "Sign the lease",
    "bens": "Order Ben's laptop",
    "danas": "Dana's trial quote",
    "decision": "Lunch with Acme",
}
DIGEST = {
    "principal": {"principals", "bens", "danas", "decision"},
    "teammate": {"bens"},
    "demo": {"danas"},
}


@pytest.mark.parametrize("who", sorted(DIGEST))
def test_the_web_chat_digest_holds_exactly_the_callers_home_cards(
    board: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    from openexecutive.briefing.context import render_and_trust
    from openexecutive.orchestrator.session import Session

    session = Session(session_id=f"web-{who}", from_web_chat=True, caller_person_id=board[who],
                      web_caller_signed_in=True)
    block = render_and_trust(session)
    shown = {name for name, headline in HEADLINES.items() if headline in block}
    assert shown == DIGEST[who]
    assert session.trusted_alert_ids == {board[name] for name in DIGEST[who]}
    # The same cards their own Home shows.
    client = app_client(monkeypatch, signed=True)
    home = call(client, True, who, "GET", "/today").json()
    on_home = {p["alert_id"] for p in home["proposals"]} & {board[n] for n in HEADLINES}
    assert on_home == session.trusted_alert_ids


def test_the_digest_for_a_caller_who_resolves_to_no_one_holds_no_card(board: dict[str, int]) -> None:
    from openexecutive.briefing.context import render_and_trust
    from openexecutive.orchestrator.session import Session

    session = Session(session_id="web-nobody", from_web_chat=True, caller_person_id=None)
    block = render_and_trust(session)
    assert not any(headline in block for headline in HEADLINES.values())
    assert session.trusted_alert_ids == set()

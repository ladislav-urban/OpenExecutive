"""The "Already handled" block a chat turn reads (briefing.context._handled_block)
follows the caller's own Home, as the open half does: a closed card routed to
someone else is not listed for a teammate, a demo person or a caller who
resolves to no one; the principal's lists every closed card."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.orchestrator.session import Session

from .principal_gate_support import make_roster

CLOSED = {
    "principal": "Signed the lease",
    "teammate": "Ordered Ben's laptop",
    "demo": "Sent Dana's quote",
}


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    ids = make_roster(tmp_path, monkeypatch)
    alerts_store.initialize_db()
    for who, headline in CLOSED.items():
        alert_id = alerts_store.insert_alert(
            source="test", external_id=headline, severity="medium", headline=headline, body="",
            routed_to_person_id=ids[who],
        )
        assert alerts_store.set_status(alert_id, "dismissed")
    return ids


def _block(person: int | None) -> str:
    from openexecutive.briefing.context import render_and_trust

    session = Session(session_id=f"web-{person}", from_web_chat=True, caller_person_id=person,
                      web_caller_signed_in=person is not None)
    return render_and_trust(session)


def test_the_principal_reads_every_closed_card(ids: dict[str, int]) -> None:
    block = _block(ids["principal"])
    for headline in CLOSED.values():
        assert headline in block


@pytest.mark.parametrize("who", ["teammate", "demo"])
def test_anyone_else_reads_only_their_own_closed_cards(ids: dict[str, int], who: str) -> None:
    block = _block(ids[who])
    assert CLOSED[who] in block
    for other, headline in CLOSED.items():
        if other != who:
            assert headline not in block


def test_a_caller_who_resolves_to_no_one_reads_no_closed_card(ids: dict[str, int]) -> None:
    block = _block(None)
    assert not any(headline in block for headline in CLOSED.values())


def test_a_teammates_channel_digest_lists_only_their_closed_cards(ids: dict[str, int]) -> None:
    from openexecutive.briefing.context import render_and_trust

    session = Session(session_id="slack-ben", origin_channel="slack", origin_channel_ref="slack:dm:UBEN",
                      caller_person_id=ids["teammate"])
    block = render_and_trust(session, routed_to=ids["teammate"])
    assert CLOSED["teammate"] in block
    assert CLOSED["principal"] not in block and CLOSED["demo"] not in block

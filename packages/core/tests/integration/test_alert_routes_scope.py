"""The /alerts/{id} routes answer a caller about a card only when their own
Home keeps it (alerts._on_callers_home): anyone else gets 404, as for a
missing id, before anything says what kind of card it is. The principal is
unchanged."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.alerts import store as alerts_store

from .principal_gate_support import app_client, call, make_roster


@pytest.fixture
def cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    ids = make_roster(tmp_path, monkeypatch)
    alerts_store.initialize_db()

    def _card(headline: str, routed: int | None, **kw: object) -> int:
        return alerts_store.insert_alert(
            source=str(kw.pop("source", "test")), external_id=headline, severity="medium",
            headline=headline, body="", routed_to_person_id=routed, **kw,  # type: ignore[arg-type]
        )

    return {
        **ids,
        "principals": _card("Sign the lease", ids["principal"]),
        "principals_roster": _card("Who is this?", ids["principal"], source="roster_request"),
        "principals_question": _card("Which date?", ids["principal"], needs="answer", question="Which?"),
        "bens": _card("Order Ben's laptop", ids["teammate"]),
    }


ROUTES = [
    ("POST", "/alerts/{id}/ack", {"status": "dismissed"}),
    ("POST", "/alerts/{id}/approve", {}),
    ("POST", "/alerts/{id}/answer", {"answer": "Tuesday"}),
]


@pytest.mark.parametrize("who", ["teammate", "demo"])
def test_a_card_not_on_their_home_is_not_found_whatever_its_kind(
    cards: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client = app_client(monkeypatch, signed=True)
    for card in ("principals", "principals_roster", "principals_question"):
        for method, path, body in ROUTES:
            r = call(client, True, who, method, path.format(id=cards[card]), body)
            # The same answer as for an id that does not exist, so neither the
            # card nor its kind shows (a demo write may meet the demo gate first).
            missing = call(client, True, who, method, path.format(id=999999), body)
            assert r.status_code == missing.status_code, (card, path, r.text)
            assert r.status_code in (403, 404), (card, path, r.text)
            if r.status_code == 404:
                assert r.json()["detail"] == "Alert not found"
    assert alerts_store.get_alert(cards["principals"]).status != "dismissed"  # type: ignore[union-attr]


def test_a_teammate_still_clears_their_own_card(cards: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client = app_client(monkeypatch, signed=True)
    r = call(client, True, "teammate", "POST", f"/alerts/{cards['bens']}/ack", {"status": "dismissed"})
    assert r.status_code == 200, r.text


def test_the_principal_still_gets_the_cards_kind(cards: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    client = app_client(monkeypatch, signed=True)
    r = call(client, True, "principal", "POST", f"/alerts/{cards['principals_roster']}/ack", {"status": "dismissed"})
    assert r.status_code == 409, r.text
    r = call(client, True, "principal", "POST", f"/alerts/{cards['principals_question']}/approve", {})
    assert r.status_code == 409, r.text
    r = call(client, True, "principal", "POST", f"/alerts/{cards['bens']}/ack", {"status": "dismissed"})
    assert r.status_code == 200, r.text

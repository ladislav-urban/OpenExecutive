"""Who reads and changes the decisions, initiatives and advice under /memories.

Decisions and initiatives are read by the principal and the active team; a
demo person, a signed-in email on no People entry, a call with no caller while
signed callers are on, a suspended teammate and a caller the roster can't
place are refused (403). Past advice is the principal's alone. Every change
or removal of any of the three is the principal's alone.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.memory import episodic

from .principal_gate_support import app_client, call, make_roster

DECISION = "Decided to open the Ohio office"
INITIATIVE = "Warehouse move"
ADVICE = "Raise prices before the renewal round"

READERS = ("principal", "teammate", "operator")
REFUSED = ("demo", "stranger", "unsigned", "suspended", "unreadable")
EVERYONE = (*READERS, *REFUSED)


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    ids = make_roster(tmp_path, monkeypatch)
    episodic.store_decision(domain="strategy", summary=DECISION)
    episodic.store_initiative(INITIATIVE, "active")
    episodic.store_advice("strategy", "Should we raise prices?", ADVICE)
    return ids


def _as(monkeypatch: pytest.MonkeyPatch, ids: dict[str, int], who: str) -> tuple[Any, str]:
    """The client, and who it signs as, for each caller kind (signed callers
    on). A suspended teammate signs as the teammate after their suspension; an
    unreadable roster is the teammate's call while the People read fails."""
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    client = app_client(monkeypatch, signed=True)
    if who == "suspended":
        assert people_store.suspend_person(ids["teammate"], by="principal")
        people_registry.invalidate()
        return client, "teammate"
    if who == "unreadable":
        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("database is locked")

        monkeypatch.setattr(people_store, "get_person", _boom)
        return client, "teammate"
    return client, who


def _ids_of() -> dict[str, int]:
    return {
        "decisions": episodic.list_decisions()[0].id or 0,
        "initiatives": episodic.list_initiatives()[0].id or 0,
        "advice": episodic.list_advice()[0].id or 0,
    }


@pytest.mark.parametrize("who", EVERYONE)
def test_who_reads_decisions_and_initiatives(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client, as_who = _as(monkeypatch, ids, who)
    for path, text, key in (
        ("/memories/decisions", DECISION, "summary"),
        ("/memories/initiatives", INITIATIVE, "title"),
    ):
        r = call(client, True, as_who, "GET", path)
        if who in READERS:
            assert r.status_code == 200, (path, r.text)
            assert [row[key] for row in r.json()] == [text]
        else:
            assert r.status_code == 403, (path, r.text)
            assert text not in r.text


@pytest.mark.parametrize("who", EVERYONE)
def test_only_the_principal_reads_past_advice(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client, as_who = _as(monkeypatch, ids, who)
    r = call(client, True, as_who, "GET", "/memories/advice")
    if who in ("principal", "operator"):
        assert r.status_code == 200, r.text
        assert [row["advice_summary"] for row in r.json()] == [ADVICE]
    else:
        assert r.status_code == 403, r.text
        assert ADVICE not in r.text


def test_the_principal_reads_everything_in_open_mode(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = app_client(monkeypatch, signed=False)
    for path in ("/memories/decisions", "/memories/initiatives", "/memories/advice"):
        assert call(client, False, "principal", "GET", path).status_code == 200, path
    assert call(client, False, "teammate", "GET", "/memories/initiatives").status_code == 200
    assert call(client, False, "teammate", "GET", "/memories/advice").status_code == 403
    assert call(client, False, "stranger", "GET", "/memories/decisions").status_code == 403


BODIES = {
    "decisions": {"summary": "Decided to open the Texas office"},
    "initiatives": {"status": "done"},
    "advice": {"advice_summary": "Hold prices"},
}


@pytest.mark.parametrize("who", ["teammate", *REFUSED])
def test_only_the_principal_changes_or_removes_them(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    rows = _ids_of()
    client, as_who = _as(monkeypatch, ids, who)
    for kind, row_id in rows.items():
        path = f"/memories/{kind}/{row_id}"
        r = call(client, True, as_who, "PATCH", path, BODIES[kind])
        assert r.status_code == 403, (kind, r.text)
        r = call(client, True, as_who, "DELETE", path)
        assert r.status_code == 403, (kind, r.text)
    assert [d.summary for d in episodic.list_decisions()] == [DECISION]
    assert [(i.title, i.status) for i in episodic.list_initiatives()] == [(INITIATIVE, "active")]
    assert [a.advice_summary for a in episodic.list_advice()] == [ADVICE]


@pytest.mark.parametrize("who", ["principal", "operator"])
def test_the_principal_changes_and_removes_them(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    rows = _ids_of()
    client, as_who = _as(monkeypatch, ids, who)
    for kind, row_id in rows.items():
        r = call(client, True, as_who, "PATCH", f"/memories/{kind}/{row_id}", BODIES[kind])
        assert r.status_code == 200, (kind, r.text)
        # An edited decision is kept as a new row that replaces the old one.
        new_id = r.json()["id"]
        r = call(client, True, as_who, "DELETE", f"/memories/{kind}/{new_id}")
        assert r.status_code == 204, (kind, r.text)
    assert episodic.list_decisions() == []
    assert episodic.list_initiatives() == []
    assert episodic.list_advice() == []

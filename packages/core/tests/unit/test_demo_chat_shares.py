"""A demo person shares none of their chats (people.demo): PUT
/sessions/{id}/shares refuses them before any target is looked up, and a share
row left from before lets no one in, lists the chat in no one's shared-with-me
and names no one in GET /sessions/{id}/people."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes import sessions as sessions_route
from openexecutive.memory import episodic, session_store
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

ALEX = {"x-caller-email": "alex@example.com"}  # principal
SABIN = {"x-caller-email": "sabin@example.com"}
RIYA = {"x-caller-email": "riya@example.com"}
DANA = {"x-caller-email": "dana@prospect.example"}  # demo


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    chat_route._sessions.clear()
    chat_route._session_starters.clear()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *_a, **_k: None)
    yield
    chat_route._sessions.clear()
    chat_route._session_starters.clear()


@pytest.fixture()
def people(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    # session_store binds ./episodic_memory.db as a default argument at
    # import, so the test runs where that path is the test's own.
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()
    dana = people_store.upsert_person(full_name="Dana Demo", email="dana@prospect.example", role="Demo user")
    people_store.link_directory(dana, "kc-dana")
    assert people_store.set_directory_demo(dana, True)
    people_registry.invalidate()
    yield {
        "alex": people_store.upsert_person(full_name="Alex Principal", is_principal=True, email="alex@example.com"),
        "sabin": people_store.upsert_person(full_name="Sabin Team", email="sabin@example.com"),
        "riya": people_store.upsert_person(full_name="Riya Team", email="riya@example.com"),
        "dana": dana,
    }
    people_registry.invalidate()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    app = FastAPI()
    app.include_router(chat_route.router)
    app.include_router(sessions_route.router)
    return TestClient(app)


def _chat_of(owner: int, sid: str) -> str:
    session_store.create_session(sid, "Placeholder", "2026-10-01T09:00:00+00:00", caller_person_id=owner)
    session_store.save_message(sid, "user", "question", sender_person_id=owner)
    session_store.save_message(sid, "assistant", "answer")
    return sid


def test_a_demo_owner_is_refused_alike_for_every_target(client: TestClient, people: dict[str, int]) -> None:
    sid = _chat_of(people["dana"], "web-demo")
    answers = [
        client.put(f"/sessions/{sid}/shares", json={"person_ids": ids}, headers=DANA)
        for ids in ([people["sabin"]], [people["alex"]], [987654], [])
    ]
    assert {r.status_code for r in answers} == {403}
    assert len({r.content for r in answers}) == 1
    for name in ("Sabin", "Alex", "Riya"):
        assert name not in answers[0].text
    assert session_store.shared_with(sid) == []


def test_a_share_left_on_a_demo_chat_opens_it_to_no_one(client: TestClient, people: dict[str, int]) -> None:
    sid = _chat_of(people["dana"], "web-demo")
    session_store.set_shares(sid, [people["sabin"], people["alex"]], people["dana"])

    for who in (SABIN, ALEX):
        listed = client.get("/sessions/shared-with-me", headers=who)
        assert listed.status_code == 200
        assert sid not in {row["session_id"] for row in listed.json()}
    assert client.get(f"/sessions/{sid}/messages", headers=SABIN).status_code == 404
    assert client.get(f"/sessions/{sid}/people", headers=SABIN).status_code == 404
    assert not chat_route._shared_member(sid, people["sabin"])


def test_a_demo_owners_chat_names_no_one_else(client: TestClient, people: dict[str, int]) -> None:
    sid = _chat_of(people["dana"], "web-demo")
    session_store.set_shares(sid, [people["sabin"], people["alex"]], people["dana"])

    got = client.get(f"/sessions/{sid}/people", headers=DANA)
    assert got.status_code == 200
    assert got.json() == {"you": people["dana"], "people": []}
    shares = client.get(f"/sessions/{sid}/shares", headers=DANA)
    assert shares.status_code == 200 and shares.json() == []
    messages = client.get(f"/sessions/{sid}/messages", headers=DANA)
    assert messages.status_code == 200
    for name in ("Sabin", "Alex"):
        assert name not in messages.text


def test_a_demo_chat_with_an_unreadable_roster_names_no_one(
    people: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _chat_of(people["dana"], "web-demo")
    session_store.set_shares(sid, [people["sabin"]], people["dana"])
    monkeypatch.setattr("openexecutive.people.demo.demo_state", lambda _pid: None)
    assert chat_route.chat_people(sid) == []
    with pytest.raises(RuntimeError):
        chat_route.chat_people(sid, strict=True)
    assert session_store.list_shared_with_person(people["sabin"]) == []
    assert not chat_route._shared_member(sid, people["sabin"])


def test_the_principal_and_a_teammate_still_share(client: TestClient, people: dict[str, int]) -> None:
    mine = _chat_of(people["alex"], "web-alex")
    got = client.put(f"/sessions/{mine}/shares", json={"person_ids": [people["sabin"]]}, headers=ALEX)
    assert got.status_code == 200
    assert [r["full_name"] for r in got.json()] == ["Sabin Team"]

    theirs = _chat_of(people["sabin"], "web-sabin")
    got = client.put(f"/sessions/{theirs}/shares", json={"person_ids": [people["riya"]]}, headers=SABIN)
    assert got.status_code == 200
    assert [r["full_name"] for r in got.json()] == ["Riya Team"]

    shared = {r["session_id"] for r in client.get("/sessions/shared-with-me", headers=SABIN).json()}
    assert mine in shared
    assert client.get(f"/sessions/{theirs}/messages", headers=RIYA).status_code == 200
    room = client.get(f"/sessions/{theirs}/people", headers=RIYA).json()
    assert [(p["full_name"], p["owner"]) for p in room["people"]] == [("Sabin Team", True), ("Riya Team", False)]
    # Sharing with a demo person stays the 422 it was.
    refused = client.put(f"/sessions/{mine}/shares", json={"person_ids": [people["dana"]]}, headers=ALEX)
    assert refused.status_code == 422


def test_a_demo_owners_export_names_no_one_else(client: TestClient, people: dict[str, int]) -> None:
    sid = _chat_of(people["dana"], "web-demo")
    session_store.set_shares(sid, [people["sabin"], people["alex"]], people["dana"])
    session_store.save_message(sid, "user", "from the principal", sender_person_id=people["alex"])
    session_store.save_message(sid, "user", "from a teammate", sender_person_id=people["sabin"])

    got = client.get(f"/sessions/{sid}/export", headers=DANA)
    assert got.status_code == 200
    text = got.text
    for name in ("Alex", "Sabin"):
        assert name not in text
    # Their words are someone else's, never the demo person's.
    assert "## Someone else\n\nfrom the principal" in text
    assert "## Someone else\n\nfrom a teammate" in text
    assert "## Dana Demo\n\nquestion" in text


def test_an_export_names_everyone_in_a_real_shared_chat(client: TestClient, people: dict[str, int]) -> None:
    sid = _chat_of(people["sabin"], "web-sabin")
    session_store.set_shares(sid, [people["riya"]], people["sabin"])
    session_store.save_message(sid, "user", "from riya", sender_person_id=people["riya"])
    text = client.get(f"/sessions/{sid}/export", headers=RIYA).text
    assert "## Sabin Team\n\nquestion" in text and "## Riya Team\n\nfrom riya" in text


def test_shares_answer_503_while_the_roster_cant_be_read(
    client: TestClient, people: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _chat_of(people["sabin"], "web-sabin")
    session_store.set_shares(sid, [people["riya"]], people["sabin"])
    monkeypatch.setattr("openexecutive.people.demo.demo_state", lambda _pid: None)

    assert client.get(f"/sessions/{sid}/shares", headers=SABIN).status_code == 503
    assert client.put(f"/sessions/{sid}/shares", json={"person_ids": []}, headers=SABIN).status_code == 503
    assert [r["person_id"] for r in session_store.shared_with(sid)] == [people["riya"]]

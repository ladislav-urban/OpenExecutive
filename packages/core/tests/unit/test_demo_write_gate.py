"""A demo person changes nothing of the company's (api/demo_gate.py): every
write outside a short list is refused, whichever route forgets to check."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api import demo_gate
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

DEMO = {"x-caller-email": "dana@prospect.example"}
TEAM = {"x-caller-email": "tom@example.com"}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "test.db"
    for module in (people_store, episodic, dept_store):
        monkeypatch.setattr(module, "DB_PATH", path, raising=False)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    dept_store.initialize_db(path)
    principal = people_store.upsert_person(full_name="Olivia Owner", email="olivia@example.com", is_principal=True)
    assert principal
    people_store.upsert_person(full_name="Tom Team", email="tom@example.com")
    dana = people_store.upsert_person(full_name="Dana Demo", email="dana@prospect.example", role="Demo user")
    people_store.link_directory(dana, "kc-dana")
    assert people_store.set_directory_demo(dana, True)
    people_registry.invalidate()
    dept_registry.invalidate()
    yield path
    people_registry.invalidate()
    dept_registry.invalidate()


def _client() -> TestClient:
    from openexecutive.api.routes import departments

    app = FastAPI()
    app.middleware("http")(demo_gate.demo_write_gate)
    app.include_router(departments.router)

    @app.post("/chat")
    def chat() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def test_a_demo_persons_write_to_the_company_is_refused() -> None:
    client = _client()
    refused = client.post("/departments", json={"title": "Skunkworks"}, headers=DEMO)
    assert refused.status_code == 403 and refused.json()["detail"] == demo_gate.DEMO_REFUSED
    assert client.get("/departments", headers=DEMO).status_code == 200  # reading is fine
    assert client.post("/chat", headers=DEMO).json() == {"ok": True}  # so is asking


def test_the_team_still_writes() -> None:
    assert _client().post("/departments", json={"title": "Skunkworks"}, headers=TEAM).status_code == 201


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/chat", True),
        ("/chat/upload", True),
        ("/sessions/web-1/shares", True),
        ("/alerts/4/ack", True),
        ("/decisions/2/approve", True),
        ("/notifications/settings", True),
        ("/knowledge/search", True),
        ("/memories/history/settings", True),
        ("/people/3/attunement", True),
        ("/people/4/attunement", False),
        ("/webhook/telegram", False),
        ("/auth/ended-sessions", False),
        ("/chatter", False),
        ("/knowledge/builtin", False),
        ("/departments", False),
        ("/watchlist", False),
        ("/agents/executive", False),
        ("/fixtures/load", False),
        ("/documents/upload", False),
        ("/mcp/", False),
    ],
)
def test_which_writes_a_demo_person_may_make(path: str, allowed: bool) -> None:
    assert demo_gate.demo_may_write(path, person_id=3) is allowed


def test_a_roster_that_cant_be_read_refuses_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: never let a write through on the chance the caller isn't
    a demo person."""
    def broken(_email: str) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(demo_gate, "_person", broken)
    client = _client()
    for who in (DEMO, TEAM):
        assert client.post("/departments", json={"title": "Skunkworks"}, headers=who).status_code == 503
    assert client.post("/chat", headers=DEMO).status_code == 200  # the list needs no lookup
    # A request that names no one (the operator, a service) needs none either.
    assert client.post("/departments", json={"title": "Ops"}).status_code == 201


def test_the_gate_runs_after_the_caller_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside every other gate, so it reads the caller the signed-caller gate
    verified (``user_middleware[0]`` runs first)."""
    import json

    from openexecutive.api.main import create_app

    vectors = json.loads((Path(__file__).parent / "caller_assertion_vectors.json").read_text())
    monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", vectors["public_keys"])
    gates = [m.kwargs.get("dispatch") for m in create_app().user_middleware]
    at = gates.index(demo_gate.demo_write_gate)
    assert all(g is None for g in gates[at + 1:])  # only CORS (no dispatch) inside it
    assert at > 0 and all(callable(g) for g in gates[:at])

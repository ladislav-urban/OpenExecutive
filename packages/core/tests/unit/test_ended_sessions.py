"""Sessions the identity provider ended (memory.ended_sessions) and the routes
the web app's server records and reads them through (/auth/ended-sessions)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import auth as auth_route
from openexecutive.memory import ended_sessions, episodic

ISS = "https://sso.example.com/realms/company"


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "ended.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    return path


@pytest.fixture()
def client(db: Path) -> TestClient:
    app = FastAPI()
    app.include_router(auth_route.router)
    return TestClient(app)


def test_a_jti_is_taken_once_and_old_ones_go(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ended_sessions.record(iss=ISS, sid="s1", sub=None, ended_at=100.0, jti="a")
    assert not ended_sessions.record(iss=ISS, sid="s1", sub=None, ended_at=100.0, jti="a")
    assert ended_sessions.recent() == [{"iss": ISS, "sid": "s1", "sub": None, "ended_at": 100.0}]
    # Past KEEP_SECONDS it no longer matters, and the next record prunes it.
    later = time.time() + ended_sessions.KEEP_SECONDS + 10
    monkeypatch.setattr(ended_sessions.time, "time", lambda: later)
    assert ended_sessions.recent() == []
    assert ended_sessions.record(iss=ISS, sid=None, sub="u1", ended_at=later, jti="b")
    assert [r["sub"] for r in ended_sessions.recent()] == ["u1"]


def test_the_web_apps_server_records_and_reads_them(client: TestClient) -> None:
    res = client.post("/auth/ended-sessions", json={"iss": ISS, "sid": "s1", "ended_at": 100, "jti": "j1"})
    assert res.status_code == 204
    assert client.post("/auth/ended-sessions", json={"iss": ISS, "sid": "s1", "ended_at": 100, "jti": "j1"}).status_code == 409
    assert client.post("/auth/ended-sessions", json={"iss": ISS, "ended_at": 100, "jti": "j2"}).status_code == 422
    assert client.get("/auth/ended-sessions").json() == [{"iss": ISS, "sid": "s1", "sub": None, "ended_at": 100.0}]


def test_a_call_made_as_someone_is_refused(client: TestClient) -> None:
    # What the proxy would send for a signed-in person (it also refuses the path).
    me = {"x-caller-email": "alex@example.com"}
    assert client.post("/auth/ended-sessions", json={"iss": ISS, "sub": "u", "ended_at": 1, "jti": "x"}, headers=me).status_code == 404
    assert client.get("/auth/ended-sessions", headers=me).status_code == 404
    assert client.get("/auth/ended-sessions").json() == []

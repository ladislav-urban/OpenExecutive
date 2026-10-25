"""What the principal sees of the directory sync: the People page's
candidates and Link action (GET /people/directory, POST
/people/{id}/directory-link) and Setup status' Identity provider directory
light (api.setup_checks.check_directory)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api import setup_checks
from openexecutive.api.routes import people as people_route
from openexecutive.config import get_settings
from openexecutive.integrations import directory_sync
from openexecutive.people import store as people_store

from .test_directory_sync import Keycloak, env, kc, principal, run  # noqa: F401 — fixtures


@pytest.fixture
def client(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> TestClient:  # noqa: F811
    real = directory_sync.KeycloakReader
    monkeypatch.setattr(
        directory_sync, "KeycloakReader",
        lambda settings, **_: real(settings, transport=httpx.MockTransport(kc.handle)),
    )
    app = FastAPI()
    app.include_router(people_route.router)
    return TestClient(app)


def _check() -> setup_checks.SetupCheck:
    snap = setup_checks.Snapshot(
        settings=get_settings(), now=datetime.now(UTC), local_login=False,
        people=[], principal=None, last_inbound={},
    )
    return setup_checks.check_directory(snap)


# --------------------------------------------------------------------------- #
# People: candidates and Link
# --------------------------------------------------------------------------- #

def test_a_candidate_is_listed_and_linked(kc: Keycloak, principal: int, client: TestClient) -> None:  # noqa: F811
    adviser = people_store.upsert_person(full_name="Adam Adviser", email="dir-adviser@example.com")
    run(kc)
    state = client.get("/people/directory").json()
    assert state["enabled"] is True and state["stopped"] is None
    assert state["candidates"] == [{"person_id": adviser, "email": "dir-adviser@example.com"}]

    resp = client.post(f"/people/{adviser}/directory-link")
    assert resp.status_code == 200 and resp.json()["directory_managed"] is True
    # Linked since the run: no longer a candidate.
    assert client.get("/people/directory").json()["candidates"] == []


def test_only_the_principal_sees_or_links(kc: Keycloak, principal: int, client: TestClient) -> None:  # noqa: F811
    adviser = people_store.upsert_person(full_name="Adam Adviser", email="dir-adviser@example.com")
    people_store.upsert_person(full_name="Ben", email="ben@co.example")
    as_ben = {"x-caller-email": "ben@co.example"}
    assert client.get("/people/directory", headers=as_ben).status_code == 404
    assert client.post(f"/people/{adviser}/directory-link", headers=as_ben).status_code == 403


def test_linking_someone_not_in_the_directory_is_refused(kc: Keycloak, principal: int, client: TestClient) -> None:  # noqa: F811
    stranger = people_store.upsert_person(full_name="Nobody", email="nobody@co.example")
    resp = client.post(f"/people/{stranger}/directory-link")
    assert resp.status_code == 409 and "no active member" in resp.json()["detail"]
    assert not people_store.get_person(stranger).directory_managed


def test_with_the_sync_off_there_is_nothing_to_link(
    principal: int, client: TestClient, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setenv("DIRECTORY_SYNC_ENABLED", "false")
    assert client.get("/people/directory").json() == {
        "enabled": False, "last_run_at": None, "last_ok_at": None, "stopped": None, "candidates": [],
    }
    pid = people_store.upsert_person(full_name="Ben", email="ben@co.example")
    assert client.post(f"/people/{pid}/directory-link").status_code == 409


# --------------------------------------------------------------------------- #
# Setup status
# --------------------------------------------------------------------------- #

def test_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTORY_SYNC_ENABLED", "false")
    assert _check().state == "off"


def test_not_run_yet() -> None:
    check = _check()
    assert check.state == "warn" and "hasn't run yet" in check.summary


def test_new_people_and_candidates_wait_for_the_principal(kc: Keycloak, principal: int) -> None:  # noqa: F811
    people_store.upsert_person(full_name="Adam Adviser", email="dir-adviser@example.com")
    run(kc)
    check = _check()
    assert check.state == "warn" and check.link == "/people"
    assert "1 person new from the directory with no role yet" in check.summary
    assert "1 person on People to link" in check.summary


def test_all_in_order(kc: Keycloak, principal: int) -> None:  # noqa: F811
    run(kc)
    for p in people_store.list_directory_managed():
        if not p.is_principal and p.id is not None:
            people_store.update_person(p.id, role="Staff")
    check = _check()
    assert check.state == "ok" and "3 people managed" in check.summary


def test_a_stopped_run_is_an_error(kc: Keycloak, principal: int) -> None:  # noqa: F811
    run(kc)
    kc.fail_members = True
    run(kc)
    check = _check()
    assert check.state == "error" and "500" in check.summary


def test_a_stalled_sync_is_a_warning(kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    run(kc)
    last: dict[str, Any] = directory_sync.status() or {}
    stale = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    monkeypatch.setattr(directory_sync, "status", lambda: {**last, "last_ok_at": stale})
    check = _check()
    assert check.state == "warn" and "still have access" in check.summary


def test_the_principal_out_of_the_group_waits_for_them(kc: Keycloak, principal: int) -> None:  # noqa: F811
    run(kc)
    kc.user("owner")["enabled"] = False
    run(kc)
    assert "you aren't an active member" in _check().summary


def test_candidates_survive_a_stopped_run(kc: Keycloak, principal: int) -> None:  # noqa: F811
    people_store.upsert_person(full_name="Adam Adviser", email="dir-adviser@example.com")
    run(kc)
    kc.fail_members = True
    run(kc)
    assert directory_sync.status()["candidates"] == ["dir-adviser@example.com"]

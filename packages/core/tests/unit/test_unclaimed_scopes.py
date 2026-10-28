"""Approval scopes are the principal's to grant.

While nobody is the principal, anyone signed in may edit People
(api/routes/people.py, _require_roster_owner), so the API refuses scopes on
anyone but the principal then, and claiming the install strips every scope
other rows hold, in the claiming write's own transaction (people/store.py).
Once claimed, the principal may grant any scope, WILDCARD included.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import people as people_route
from openexecutive.people import registry, store
from openexecutive.people.models import AuthorityScope

SPEND = AuthorityScope("spend_lt_2k")
WILD = AuthorityScope.WILDCARD


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "people.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    store.initialize_db(path)
    registry.invalidate()
    return path


def _planted(db: Path) -> int:
    """A teammate someone gave scopes while nobody was the principal."""
    pid = store.upsert_person(full_name="Mallory", email="m@example.com", db_path=db)
    store.set_authority_scope(pid, [WILD, SPEND], db_path=db)
    return pid


def _scopes(pid: int, db: Path) -> list[AuthorityScope]:
    person = store.get_person(pid, db)
    assert person is not None
    return person.authority_scope


@pytest.mark.parametrize("claim", ["insert", "update", "claim_principal", "add_principal", "transfer"])
def test_claiming_the_install_strips_scopes_planted_before(db: Path, claim: str) -> None:
    planted = _planted(db)
    if claim == "insert":
        owner = store.upsert_person(full_name="Pat", email="pat@example.com", is_principal=True, db_path=db)
    elif claim == "add_principal":
        added = store.add_principal(full_name="Pat", role="", email="pat@example.com", db_path=db)
        assert added is not None
        owner = added
    else:
        owner = store.upsert_person(full_name="Pat", email="pat@example.com", db_path=db)
        if claim == "update":
            store.upsert_person(person_id=owner, full_name="Pat", email="pat@example.com", is_principal=True, db_path=db)
        elif claim == "claim_principal":
            assert store.claim_principal(owner, "pat@example.com", db)
        else:
            assert store.transfer_principal(owner, db) == []
    assert _scopes(planted, db) == []
    if claim != "update" and claim != "insert":
        assert WILD in _scopes(owner, db)


def test_once_claimed_the_principal_may_grant_any_scope(db: Path) -> None:
    owner = store.upsert_person(full_name="Pat", email="pat@example.com", is_principal=True, db_path=db)
    teammate = store.upsert_person(full_name="Ann", email="ann@example.com", db_path=db)
    store.set_authority_scope(teammate, [WILD], db_path=db)
    # Rewriting the principal's own row is no claim: the teammate keeps it.
    store.upsert_person(person_id=owner, full_name="Pat Owner", email="pat@example.com", is_principal=True, db_path=db)
    assert _scopes(teammate, db) == [WILD]
    # Nor is moving the seat between owners: only the old principal loses WILDCARD.
    store.transfer_principal(teammate, db)
    assert WILD in _scopes(teammate, db)


@pytest.fixture
def client(db: Path) -> TestClient:
    app = FastAPI()
    app.include_router(people_route.router)
    return TestClient(app)


SIGNED_IN = {"x-caller-email": "someone@example.com"}  # signed in, nobody is the principal yet


def test_while_unclaimed_the_api_refuses_scopes_on_anyone_but_the_principal(client: TestClient, db: Path) -> None:
    refused = client.post("/people", json={"full_name": "Mallory", "authority_scope": ["wildcard"]}, headers=SIGNED_IN)
    assert refused.status_code == 403
    added = client.post("/people", json={"full_name": "Mallory"}, headers=SIGNED_IN)
    assert added.status_code == 201
    patched = client.patch(f"/people/{added.json()['id']}", json={"authority_scope": ["spend_lt_2k"]}, headers=SIGNED_IN)
    assert patched.status_code == 403
    assert _scopes(added.json()["id"], db) == []


def test_a_transfer_to_the_current_principal_claims_nothing(db: Path) -> None:
    """A setup re-run keeping the owner (open to anyone signed in) transfers to
    the principal there already is: no claim, so the team keeps its scopes."""
    owner = store.upsert_person(full_name="Pat", email="pat@example.com", is_principal=True, db_path=db)
    teammate = store.upsert_person(full_name="Ann", email="ann@example.com", db_path=db)
    store.set_authority_scope(teammate, [SPEND], db_path=db)
    assert store.transfer_principal(owner, db) == []
    assert _scopes(teammate, db) == [SPEND]


def test_a_wizard_save_that_claims_the_install_grants_no_one_else_scopes(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.onboarding import profile_builder

    records = [
        {"full_name": "Pat", "is_principal": True, "authority_scope": ["wildcard"]},
        {"full_name": "Mallory", "role": "Advisor", "authority_scope": ["wildcard"]},
    ]
    monkeypatch.setattr(profile_builder, "build_people_from_answers", lambda answers: [dict(r) for r in records])
    profile_builder._save_wizard_people({})
    by_name = {p.full_name: p for p in store.list_people(db_path=db)}
    assert by_name["Pat"].is_principal and WILD in by_name["Pat"].authority_scope
    assert by_name["Mallory"].authority_scope == []

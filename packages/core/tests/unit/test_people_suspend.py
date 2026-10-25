"""Suspended People (people.store.suspend_person / restore_person): a
reversible "off". A suspended teammate can't sign in, isn't a caller, an
approver, an email recipient or a chat speaker, and their open loops and
working style wait unchanged until they are restored."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import auth as auth_route
from openexecutive.api.routes import people as people_route
from openexecutive.attunement import open_loops, style
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope


@pytest.fixture(autouse=True)
def audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    db = tmp_path / "test.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    rows: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: rows.append((event_type, kw.get("details") or {})),
    )
    yield rows
    people_registry.invalidate()


@pytest.fixture
def team() -> SimpleNamespace:
    principal = people_store.upsert_person(
        full_name="Pat Principal", is_principal=True, email="pat@co.example"
    )
    sara = people_store.upsert_person(
        full_name="Sara Kim", email="sara@co.example", slack_user_id="U_SARA",
        telegram_chat_id="555", discord_user_id="D_SARA",
    )
    people_store.set_authority_scope(sara, [AuthorityScope.SPEND_LT_2K])
    people_store.set_person_emails(sara, ["s.kim@co.example"])
    return SimpleNamespace(principal=principal, sara=sara)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(people_route.router)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Out of every access decision
# --------------------------------------------------------------------------- #

def test_a_suspended_teammate_is_out_of_every_access_read(team: SimpleNamespace) -> None:
    assert people_store.suspend_person(team.sara, by="principal") is True

    assert people_store.find_person_by_email("sara@co.example") is None
    assert people_store.find_person_by_address("s.kim@co.example") is None
    assert people_store.find_person_by_slack_id("U_SARA") is None
    assert people_store.find_person_by_discord_id("D_SARA") is None
    # Telegram's roster gate: a miss sends them to roster intake like a stranger.
    assert people_store.find_person_by_telegram_chat_id("555") is None
    assert people_store.find_approvers(AuthorityScope.SPEND_LT_2K) == []
    assert [p.id for p in people_store.list_people()] == [team.principal]
    assert [p.id for p in people_registry.list_people(force_refresh=True)] == [team.principal]
    # Sign-in: off the allow-list the UI checks on every gated request.
    assert [e.email for e in auth_route.allowed_emails()] == ["pat@co.example"]


def test_a_suspended_teammate_cant_be_emailed(team: SimpleNamespace) -> None:
    from openexecutive.orchestrator.mcp_gateway import _roster_allow_set

    assert "sara@co.example" in _roster_allow_set()
    people_store.suspend_person(team.sara, by="principal")
    allow = _roster_allow_set()
    assert "sara@co.example" not in allow
    assert "s.kim@co.example" not in allow


def test_they_keep_their_addresses_while_suspended(team: SimpleNamespace) -> None:
    """Nothing is freed: no one else can take their address meanwhile."""
    people_store.suspend_person(team.sara, by="principal")
    assert people_store.address_holder("sara@co.example") == team.sara
    assert people_store.alias_holder("s.kim@co.example") == team.sara
    other = people_store.upsert_person(full_name="Someone Else")
    with pytest.raises(people_store.AddressInUseError):
        people_store.set_person_emails(other, ["s.kim@co.example"])


def test_the_people_page_can_still_list_them(team: SimpleNamespace) -> None:
    people_store.suspend_person(team.sara, by="principal")
    [sara] = [p for p in people_store.list_people(include_suspended=True) if p.id == team.sara]
    assert sara.suspended and sara.suspended_by == "principal" and sara.suspended_at
    assert not sara.active and not sara.archived


# --------------------------------------------------------------------------- #
# Restoring brings everything back
# --------------------------------------------------------------------------- #

def _open(owner: int) -> int:
    loop_id = open_loops.open_loop(
        owner_person_id=owner, description="Sara Kim committed to: send the Q3 numbers",
        due_at=datetime.now(UTC) + timedelta(days=3), db_path=episodic.DB_PATH,
    )
    assert loop_id is not None
    return loop_id


def test_restoring_brings_back_their_loops_and_working_style(team: SimpleNamespace) -> None:
    loop_id = _open(team.sara)
    style.save_profile(
        team.sara, [style.StyleRule("Keep it short", style.BASIS_CONVERSATION)],
        locked=True, updated_by="principal",
    )
    before = open_loops.get_open_loop(loop_id)

    people_store.suspend_person(team.sara, by="principal")
    assert open_loops.get_open_loop(loop_id) == before
    assert [r.text for r in style.get_profile(team.sara).rules] == ["Keep it short"]

    assert people_store.restore_person(team.sara) is True
    sara = people_store.get_person(team.sara)
    assert sara is not None and sara.active and sara.suspended_by is None
    assert open_loops.get_open_loop(loop_id) == before
    profile = style.get_profile(team.sara)
    assert [r.text for r in profile.rules] == ["Keep it short"] and profile.locked
    assert people_store.find_person_by_email("sara@co.example").id == team.sara  # type: ignore[union-attr]
    assert [p.id for p in people_store.find_approvers(AuthorityScope.SPEND_LT_2K)] == [team.sara]
    assert people_store.alias_holder("s.kim@co.example") == team.sara


def test_their_loops_are_paused_not_expired(team: SimpleNamespace) -> None:
    loop_id = _open(team.sara)
    people_store.suspend_person(team.sara, by="principal")
    later = datetime.now(UTC) + timedelta(days=90)
    assert open_loops.expire_open_loops(later, ttl_days=30, db_path=episodic.DB_PATH) == 0
    assert open_loops.get_open_loop(loop_id) is not None


def test_a_restore_restarts_the_expiry_clock(team: SimpleNamespace) -> None:
    loop_id = _open(team.sara)
    with people_store._get_conn() as conn:
        conn.execute(
            "UPDATE scheduled_actions SET created_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(days=60)).isoformat(), loop_id),
        )
    people_store.suspend_person(team.sara, by="principal")
    people_store.restore_person(team.sara)
    # Opened 60 days ago, past a 30-day TTL, but restored just now: a fresh 30 days.
    assert open_loops.expire_open_loops(datetime.now(UTC), ttl_days=30, db_path=episodic.DB_PATH) == 0
    soon = datetime.now(UTC) + timedelta(days=20)
    assert open_loops.expire_open_loops(soon, ttl_days=30, db_path=episodic.DB_PATH) == 0
    later = datetime.now(UTC) + timedelta(days=31)
    assert open_loops.expire_open_loops(later, ttl_days=30, db_path=episodic.DB_PATH) == 1
    assert open_loops.get_open_loop(loop_id) is None


def test_no_new_loop_is_assigned_to_them(team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.config import get_settings

    monkeypatch.setattr(get_settings(), "attunement_enabled", True)
    monkeypatch.setattr(get_settings(), "attunement_open_loops_enabled", True)
    people_store.suspend_person(team.sara, by="principal")
    result = open_loops.assign_open_loop(
        owner_person_id=team.sara, text="Send the deck", assigned_by_person_id=team.principal
    )
    assert result.reason == "owner_suspended"


# --------------------------------------------------------------------------- #
# Store rules
# --------------------------------------------------------------------------- #

def test_the_principal_cant_be_suspended(team: SimpleNamespace) -> None:
    with pytest.raises(people_store.PrincipalSuspendError):
        people_store.suspend_person(team.principal, by="principal")


def test_suspending_twice_or_an_archived_person_changes_nothing(team: SimpleNamespace) -> None:
    assert people_store.suspend_person(team.sara, by="directory") is True
    assert people_store.suspend_person(team.sara, by="principal") is False
    assert people_store.get_person(team.sara).suspended_by == "directory"  # type: ignore[union-attr]
    gone = people_store.upsert_person(full_name="Gone")
    people_store.archive_person(gone)
    assert people_store.suspend_person(gone, by="principal") is False
    assert people_store.restore_person(gone) is False


def test_the_directory_never_lifts_the_principals_suspension(team: SimpleNamespace) -> None:
    people_store.suspend_person(team.sara, by="principal")
    assert people_store.restore_person(team.sara, only_by="directory") is False
    assert people_store.get_person(team.sara).suspended  # type: ignore[union-attr]
    assert people_store.restore_person(team.sara) is True


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

def test_suspend_and_restore_routes(
    team: SimpleNamespace, client: TestClient, audited: list[tuple[str, dict]]
) -> None:
    resp = client.post(f"/people/{team.sara}/suspend")
    assert resp.status_code == 200 and resp.json()["suspended"] is True
    assert resp.json()["suspended_by"] == "principal"
    assert [p["id"] for p in client.get("/people").json()] == [team.principal]
    listed = client.get("/people", params={"include_suspended": "true"}).json()
    assert {p["id"] for p in listed} == {team.principal, team.sara}

    resp = client.post(f"/people/{team.sara}/restore")
    assert resp.status_code == 200 and resp.json()["suspended"] is False
    assert [(t, d["by"]) for t, d in audited if t.startswith("person_")] == [
        ("person_suspended", "principal"), ("person_restored", "principal"),
    ]


def test_the_principal_cant_be_suspended_over_http(team: SimpleNamespace, client: TestClient) -> None:
    assert client.post(f"/people/{team.principal}/suspend").status_code == 422


def test_only_the_principal_suspends(team: SimpleNamespace, client: TestClient) -> None:
    as_sara = {"x-caller-email": "sara@co.example"}
    assert client.post(f"/people/{team.sara}/suspend", headers=as_sara).status_code == 403
    people_store.suspend_person(team.sara, by="principal")
    # A teammate listing People never learns who is suspended.
    other = people_store.upsert_person(full_name="Ben Ortiz", email="ben@co.example")
    listed = client.get(
        "/people", params={"include_suspended": "true"}, headers={"x-caller-email": "ben@co.example"}
    ).json()
    assert {p["id"] for p in listed} == {team.principal, other}


def test_a_directory_managed_name_and_email_are_read_only(team: SimpleNamespace, client: TestClient) -> None:
    with people_store._get_conn() as conn:
        conn.execute(
            "UPDATE people SET directory_managed = 1, directory_subject = 'kc-1' WHERE id = ?",
            (team.sara,),
        )
    assert client.patch(f"/people/{team.sara}", json={"full_name": "Sarah Kim"}).status_code == 409
    assert client.patch(f"/people/{team.sara}", json={"email": "x@co.example"}).status_code == 409
    # The rest is OpenExecutive's, and unchanged values pass.
    resp = client.patch(
        f"/people/{team.sara}", json={"role": "CFO", "full_name": "Sara Kim"}
    )
    assert resp.status_code == 200 and resp.json()["role"] == "CFO"
    assert resp.json()["directory_managed"] is True
    # The provider's id never leaves the server.
    assert "directory_subject" not in resp.json()


# --------------------------------------------------------------------------- #
# Security review (Oct 25, 2026)
# --------------------------------------------------------------------------- #

def test_a_teammate_never_learns_who_is_suspended(team: SimpleNamespace, client: TestClient) -> None:
    import asyncio
    import json

    from openexecutive.orchestrator.people_tools import handle_list_people

    ben = people_store.upsert_person(full_name="Ben Ortiz", email="ben@co.example")
    people_store.suspend_person(team.sara, by="principal")
    as_ben = {"x-caller-email": "ben@co.example"}
    listed = client.get("/people", params={"include_archived": "true"}, headers=as_ben).json()
    assert {p["id"] for p in listed} == {team.principal, ben}
    assert client.get(f"/people/{team.sara}", headers=as_ben).status_code == 404
    # The principal sees them, archived listing included.
    assert client.get(f"/people/{team.sara}").json()["suspended"] is True
    assert team.sara in {p["id"] for p in client.get("/people", params={"include_archived": "true"}).json()}
    # Off the principal's own verified turn, the chat tool doesn't show them either.
    out = json.loads(asyncio.run(handle_list_people({"include_archived": True})))
    assert team.sara not in {p["person_id"] for p in out["people"]}


def test_a_suspended_persons_chat_ids_stay_theirs(team: SimpleNamespace) -> None:
    from openexecutive.people import roster_requests

    people_store.suspend_person(team.sara, by="principal")
    assert people_store.chat_id_holder("telegram_chat_id", "555") == team.sara
    # A roster request from their account can't hand it to a new row.
    assert roster_requests._chat_holder("telegram", "555") == team.sara
    assert roster_requests._chat_holder("slack", "U_SARA") == team.sara
    with pytest.raises(ValueError):
        people_store.chat_id_holder("email", "x")


def test_a_suspended_person_is_never_made_the_principal(team: SimpleNamespace) -> None:
    from openexecutive.onboarding.commit import save_onboarding_people
    from openexecutive.onboarding.interview import PersonDraft

    people_store.suspend_person(team.sara, by="principal")
    with pytest.raises(people_store.PrincipalSuspendError):
        people_store.upsert_person(person_id=team.sara, full_name="Sara Kim", is_principal=True)
    # Onboarding naming them the owner brings them back first, so there is
    # always an active principal.
    save_onboarding_people([PersonDraft(full_name="Sara Kim", is_principal=True)])
    principal = people_store.find_principal_person()
    assert principal is not None and principal.id == team.sara and principal.active


def test_a_suspended_persons_address_cant_be_given_to_someone_else(
    team: SimpleNamespace, client: TestClient
) -> None:
    people_store.suspend_person(team.sara, by="principal")
    assert client.post("/people", json={"full_name": "Sara 2", "email": "sara@co.example"}).status_code == 409
    ben = people_store.upsert_person(full_name="Ben Ortiz", email="ben@co.example")
    assert client.patch(f"/people/{ben}", json={"email": "sara@co.example"}).status_code == 409

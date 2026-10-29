"""A demo person sees no other demo person, and nobody's email or chat ids but
their own: the People routes, Home's team list and the chat's org block
(people/demo.py)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "test.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(dept_store, "DB_PATH", path, raising=False)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    dept_registry.invalidate()
    yield path
    people_registry.invalidate()
    dept_registry.invalidate()


def _demo(name: str, email: str) -> int:
    pid = people_store.upsert_person(full_name=name, email=email, role="Demo user")
    people_store.link_directory(pid, f"kc-{email}")
    assert people_store.set_directory_demo(pid, True)
    return pid


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(
        full_name="Olivia Owner", email="olivia@example.com", is_principal=True,
        slack_user_id="U-OLIVIA",
    )
    people_store.set_authority_scope(principal, [AuthorityScope.WILDCARD])
    teammate = people_store.upsert_person(full_name="Tom Team", email="tom@example.com", telegram_chat_id="777")
    dana = _demo("Dana Demo", "dana@prospect.example")
    pete = _demo("Pete Prospect", "pete@other.example")
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate, dana=dana, pete=pete)


def _client() -> TestClient:
    from openexecutive.api.routes import people as people_route

    app = FastAPI()
    app.include_router(people_route.router)
    return TestClient(app)


def _as(email: str) -> dict[str, str]:
    return {"x-caller-email": email}


def test_a_demo_person_lists_no_other_demo_person_and_no_ones_reach(roster: SimpleNamespace) -> None:
    rows = _client().get("/people", headers=_as("dana@prospect.example")).json()
    by_id = {r["id"]: r for r in rows}
    assert roster.pete not in by_id
    assert by_id[roster.dana]["email"] == "dana@prospect.example"  # their own stays
    for pid in (roster.principal, roster.teammate):
        row = by_id[pid]
        assert row["email"] is None and row["email_aliases"] == []
        assert row["slack_user_id"] is None and row["telegram_chat_id"] is None
        assert row["full_name"]  # still named, so the team is there to see


def test_a_demo_person_gets_a_404_for_another_demo_person(roster: SimpleNamespace) -> None:
    client = _client()
    assert client.get(f"/people/{roster.pete}", headers=_as("dana@prospect.example")).status_code == 404
    own = client.get(f"/people/{roster.dana}", headers=_as("dana@prospect.example")).json()
    assert own["email"] == "dana@prospect.example"
    principal = client.get(f"/people/{roster.principal}", headers=_as("dana@prospect.example")).json()
    assert principal["email"] is None and principal["slack_user_id"] is None


def test_approvers_by_scope_show_a_demo_person_no_address(roster: SimpleNamespace) -> None:
    rows = _client().get("/people/by-scope/spend_lt_2k", headers=_as("dana@prospect.example")).json()
    assert [r["id"] for r in rows] == [roster.principal]
    assert rows[0]["email"] is None


def test_the_team_and_the_principal_still_see_everyone(roster: SimpleNamespace) -> None:
    client = _client()
    for caller in ("olivia@example.com", "tom@example.com"):
        rows = {r["id"]: r for r in client.get("/people", headers=_as(caller)).json()}
        assert {roster.dana, roster.pete} <= set(rows)
        assert rows[roster.pete]["email"] == "pete@other.example"


def _home(people: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(people=people, awaiting=[], in_flight=[], handled_overnight=[], proposals=[])


def test_homes_team_list_hides_other_demo_people(roster: SimpleNamespace) -> None:
    from openexecutive.api.routes import today

    response = _home([SimpleNamespace(id=pid) for pid in vars(roster).values()])
    today._hide_other_demo_people(response, caller=roster.dana, principal=False)  # type: ignore[arg-type]
    assert {p.id for p in response.people} == {roster.principal, roster.teammate, roster.dana}
    everyone = _home([SimpleNamespace(id=pid) for pid in vars(roster).values()])
    today._hide_other_demo_people(everyone, caller=roster.teammate, principal=False)  # type: ignore[arg-type]
    assert len(everyone.people) == 4


def test_a_demo_turns_org_block_names_no_demo_person_and_no_reach(roster: SimpleNamespace) -> None:
    from openexecutive.departments.prompt_block import render_org_block

    team = render_org_block()
    assert "pete@other.example" in team and "olivia@example.com" in team

    demo = render_org_block(demo_viewer=True)
    assert "Olivia Owner" in demo and "Tom Team" in demo
    for hidden in ("Dana Demo", "Pete Prospect", "@", "U-OLIVIA", "777"):
        assert hidden not in demo
    # One stable variant for every demo person, so the 5m cache stays warm.
    assert render_org_block(demo_viewer=True) == demo
    # Never the principal's contacts, even if a caller asked for them.
    assert render_org_block(include_contacts=True, demo_viewer=True) == demo


def test_the_chat_passes_the_demo_flag_into_the_system_prompt() -> None:
    import inspect

    from openexecutive.orchestrator import executive

    source = inspect.getsource(executive)
    assert source.count("demo_viewer=demo_turn(session)") == 2


def test_homes_other_lists_name_no_other_demo_person(roster: SimpleNamespace) -> None:
    from openexecutive.api.routes import today

    response = SimpleNamespace(
        people=[SimpleNamespace(id=roster.teammate, full_name="Tom Team")],
        awaiting=[SimpleNamespace(person_id=roster.pete), SimpleNamespace(person_id=roster.teammate)],
        in_flight=[
            SimpleNamespace(target="Pete Prospect"), SimpleNamespace(target="Tom Team"),
            SimpleNamespace(target="vendor@example.com"),
        ],
        handled_overnight=[SimpleNamespace(target="Pete Prospect"), SimpleNamespace(target=None)],
        proposals=[SimpleNamespace(routed_to_person_id=roster.pete), SimpleNamespace(routed_to_person_id=roster.dana)],
    )
    today._hide_other_demo_people(response, caller=roster.dana, principal=False)  # type: ignore[arg-type]
    assert [a.person_id for a in response.awaiting] == [roster.teammate]
    assert [i.target for i in response.in_flight] == ["Tom Team", None]  # no raw address
    assert [h.target for h in response.handled_overnight] == [None]
    assert [c.routed_to_person_id for c in response.proposals] == [roster.dana]  # not Pete's own card


def test_setup_status_is_refused_to_a_demo_person(roster: SimpleNamespace) -> None:
    from openexecutive.api.routes import setup_status

    app = FastAPI()
    app.include_router(setup_status.router)
    assert TestClient(app).get("/setup/status", headers=_as("dana@prospect.example")).status_code == 403


def test_a_demo_person_cant_snooze_reopen_or_review_the_companys_cards(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.alerts import store as alerts_store
    from openexecutive.api.routes import alerts as alerts_route

    monkeypatch.setattr(alerts_store, "DB_PATH", episodic.DB_PATH)
    alerts_store.initialize_db(episodic.DB_PATH)
    shared = alerts_store.insert_alert(
        source="demo", external_id="ar-1", headline="Overdue receivables",
        body="Three invoices are past due.", severity="medium",
    )
    app = FastAPI()
    app.include_router(alerts_route.router)
    client = TestClient(app)
    dana = _as("dana@prospect.example")
    later = "2099-01-01T09:00:00+00:00"
    assert client.post(f"/alerts/{shared}/snooze", json={"until": later}, headers=dana).status_code == 403
    assert client.post(f"/alerts/{shared}/ack", json={"status": "dismissed"}, headers=dana).status_code == 403
    # Nor mark it read, which takes it off the live board.
    assert client.post(f"/alerts/{shared}/ack", json={"status": "read"}, headers=dana).status_code == 403
    assert client.post(f"/alerts/{shared}/reopen", headers=dana).status_code == 403
    assert client.post("/alerts/review", headers=dana).status_code == 403
    # A teammate may still snooze it, as before.
    assert client.post(f"/alerts/{shared}/snooze", json={"until": later}, headers=_as("tom@example.com")).status_code != 403

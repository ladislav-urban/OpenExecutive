"""A demo person sees no other demo person, and nobody's email or chat ids but
their own: the People routes, Home's team list and the chat's org block
(people/demo.py)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    # The team by name; the principal by role only.
    assert by_id[roster.teammate]["full_name"] == "Tom Team"
    assert by_id[roster.principal]["full_name"] == "The principal"


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
    assert "The principal" in demo and "Tom Team" in demo
    for hidden in ("Olivia Owner", "Dana Demo", "Pete Prospect", "@", "U-OLIVIA", "777"):
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


def test_right_now_never_names_another_demo_person_either(roster: SimpleNamespace) -> None:
    """Home's Right now card shows the header split into its lead, points and
    move (upstream's Home, merged Nov 2026): when the header names another
    demo person, none of the split is shown either."""
    from openexecutive.api.routes import today

    def home(text: str) -> SimpleNamespace:
        return SimpleNamespace(
            people=[], awaiting=[], in_flight=[], handled_overnight=[], proposals=[],
            narrative=text, narrative_lead=text, narrative_points=[SimpleNamespace(text=text)],
            narrative_move="Call them.",
        )

    named = home("Pete Prospect asked about the Q3 margin.")
    today._hide_other_demo_people(named, caller=roster.dana, principal=False)  # type: ignore[arg-type]
    assert (named.narrative, named.narrative_lead, named.narrative_points, named.narrative_move) == (
        None, None, [], None,
    )
    plain = home("The Q3 margin fell two points.")
    today._hide_other_demo_people(plain, caller=roster.dana, principal=False)  # type: ignore[arg-type]
    assert plain.narrative_lead == "The Q3 margin fell two points." and plain.narrative_move == "Call them."
    # A fresh list each time, never one shared between responses.
    other = home("Pete Prospect again.")
    today._hide_other_demo_people(other, caller=roster.dana, principal=False)  # type: ignore[arg-type]
    assert other.narrative_points == [] and other.narrative_points is not named.narrative_points


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


def _teammate_fact() -> object:
    from openexecutive.memory.facts import Fact

    return Fact(
        id=1, kind="fact", subject="Runway", statement="Runway is 4 months.",
        created_at="2026-10-28T09:00:00+00:00", recorded_by_role="teammate", recorded_by_name="Sam Lee",
    )


def test_pulse_shows_a_demo_person_no_one_who_said_a_fact(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import episodic as episodic_route

    monkeypatch.setattr(episodic_route, "list_facts", lambda **_k: [_teammate_fact()])
    app = FastAPI()
    app.include_router(episodic_route.router)
    client = TestClient(app)
    demo = client.get("/memories/facts", headers=_as("dana@prospect.example")).json()["facts"]
    assert demo[0]["statement"] == "Runway is 4 months." and demo[0]["recorded_by_name"] == ""
    team = client.get("/memories/facts", headers=_as("tom@example.com")).json()["facts"]
    assert team[0]["recorded_by_name"] == "Sam Lee"


def test_a_demo_persons_home_header_is_written_from_unattributed_facts(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import today
    from openexecutive.briefing import narrative
    from openexecutive.memory import facts

    seen: list[str | None] = []
    monkeypatch.setattr(
        facts, "render_facts_for_prompt", lambda attributed=True: "runway" + (" (per Sam Lee)" if attributed else ""),
    )
    monkeypatch.setattr(narrative, "render_briefing_context", lambda **k: seen.append(k["standing_facts"]) or "ctx")
    monkeypatch.setattr(today, "_nothing_needs_attention", lambda *_a: False)
    monkeypatch.setattr(today, "_narrative_activity", lambda *_a: [])
    monkeypatch.setattr(today, "_with_due_soon", lambda data, *_a: data)
    for pid in (roster.dana, roster.teammate):
        today._narrative_context({}, SimpleNamespace(id=pid), {"name": "x"}, reader=None)  # type: ignore[arg-type]
    assert seen == ["runway", None]  # None: the renderer loads them, attributed, as before


# ── The principal by role only (owner's decision, Oct 30, 2026) ──────────────


def test_a_demo_turn_never_reads_the_principals_name(roster: SimpleNamespace) -> None:
    from openexecutive.departments.prompt_block import render_org_block

    people_store.update_person(roster.teammate, reports_to_person_id=roster.principal)
    people_registry.invalidate()
    team = render_org_block()
    assert "Olivia Owner" in team and "reports to: Olivia Owner" in team
    for mode in ("team", "solo"):
        demo = render_org_block(mode, demo_viewer=True)
        assert "Olivia Owner" not in demo and "Olivia" not in demo, mode
        assert "The principal" in demo, mode
        assert render_org_block(mode, demo_viewer=True) == demo  # one constant variant
    assert "reports to: The principal" in render_org_block(demo_viewer=True)
    assert "Olivia Owner" in render_org_block("solo")


def test_people_and_approvers_name_the_principal_by_role_to_a_demo_person(roster: SimpleNamespace) -> None:
    client = _client()
    dana = _as("dana@prospect.example")
    assert client.get(f"/people/{roster.principal}", headers=dana).json()["full_name"] == "The principal"
    approvers = client.get("/people/by-scope/spend_lt_2k", headers=dana).json()
    assert [a["full_name"] for a in approvers] == ["The principal"]
    # Everyone else still sees the name.
    team = {r["id"]: r for r in client.get("/people", headers=_as("tom@example.com")).json()}
    assert team[roster.principal]["full_name"] == "Olivia Owner"


def test_the_mcp_servers_people_name_the_principal_by_role(roster: SimpleNamespace) -> None:
    from openexecutive.people.demo import shown_to_demo_viewer

    people = people_store.list_people()
    principal = next(p for p in people if p.is_principal)
    demo = next(p for p in people if p.demo)
    shown = {p.id: p.full_name for p in shown_to_demo_viewer(people, demo.id)}
    assert shown[principal.id] == "The principal"
    assert shown[demo.id] == demo.full_name  # their own name stays


def test_homes_lists_name_the_principal_by_role_to_a_demo_person(roster: SimpleNamespace) -> None:
    from openexecutive.api.routes import today

    def home() -> SimpleNamespace:
        return SimpleNamespace(
            people=[
                SimpleNamespace(id=roster.principal, full_name="Olivia Owner", is_principal=True),
                SimpleNamespace(id=roster.teammate, full_name="Tom Team", is_principal=False),
            ],
            awaiting=[SimpleNamespace(person_id=roster.principal, full_name="Olivia Owner")],
            in_flight=[SimpleNamespace(target="Olivia Owner", intent="Remind Olivia Owner about the quote")],
            handled_overnight=[
                SimpleNamespace(target="Olivia Owner", headline=None, summary="Routed the card to Olivia Owner")
            ],
            proposals=[],
        )

    response = home()
    today._hide_other_demo_people(response, caller=roster.dana, principal=False)  # type: ignore[arg-type]
    assert [p.full_name for p in response.people] == ["The principal", "Tom Team"]
    assert response.awaiting[0].full_name == "The principal"
    assert response.in_flight[0].target == "The principal"
    assert response.in_flight[0].intent == "Remind The principal about the quote"
    assert response.handled_overnight[0].summary == "Routed the card to The principal"
    teammate_view = home()
    today._hide_other_demo_people(teammate_view, caller=roster.teammate, principal=False)  # type: ignore[arg-type]
    assert teammate_view.people[0].full_name == "Olivia Owner"


def test_the_eval_marks_a_demo_turn_and_fails_on_the_principals_name(roster: SimpleNamespace) -> None:
    from openexecutive.evals.scenarios import (
        forbidden_words_in,
        load_scenarios,
        scenario_turn_surface,
        validate_scenario_yaml,
    )

    scenario = next(s for s in load_scenarios() if s["id"] == "demo_principal_001")
    fields, _ = scenario_turn_surface(scenario)
    assert fields == {"demo_caller": True}
    assert forbidden_words_in(scenario, "Olivia Owner approves it.") == ["Olivia Owner", "Olivia"]
    assert forbidden_words_in(scenario, "Diane Whitaker, the founder, approves anything above $10,000.") == []
    assert forbidden_words_in(scenario, "The principal approves it.") == []
    with pytest.raises(ValueError, match="demo_viewer"):
        validate_scenario_yaml("id: x\ndomain: operations\nquery: q\ndemo_viewer: yes please\n")


def test_the_scrubber_replaces_the_full_and_first_name_in_any_text() -> None:
    from openexecutive.people.demo import principal_name_pattern, scrub_principal_name

    pattern = principal_name_pattern(["Olivia Owner"])
    nested = {"a": ["Routed to Olivia Owner", SimpleNamespace(note="Ask Olivia first")], "b": "Olivias stays"}
    scrub_principal_name(nested, pattern)
    assert nested["a"][0] == "Routed to The principal"
    assert nested["a"][1].note == "Ask The principal first"
    assert nested["b"] == "Olivias stays"  # whole words only
    assert principal_name_pattern([]) is None


def test_the_activity_feed_names_the_principal_by_role_to_a_demo_person(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import today

    def feed(_limit: int) -> today.ActivityResponse:
        return today.ActivityResponse.model_validate({
            "items": [
                {"kind": "nudge", "actor": "executive", "summary": "Follow up with Olivia Owner about the quote", "at": "2026-10-30T09:00:00Z",
                 "target": "Olivia Owner", "department": None},
                {"kind": "nudge", "actor": "executive", "summary": "Remind Pete Prospect", "at": "2026-10-30T08:00:00Z", "target": "Pete Prospect", "department": None},
            ]
        })

    monkeypatch.setattr(today, "_build_activity", feed)
    app = FastAPI()
    app.include_router(today.router)
    client = TestClient(app)
    items = client.get("/today/activity", headers=_as("dana@prospect.example")).json()["items"]
    assert [i["summary"] for i in items] == ["Follow up with The principal about the quote"]
    assert items[0]["target"] == "The principal"
    team = client.get("/today/activity", headers=_as("tom@example.com")).json()["items"]
    assert len(team) == 2 and "Olivia Owner" in team[0]["summary"]


def test_a_demo_turns_context_names_the_principal_by_role_on_every_surface(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspect

    from openexecutive.orchestrator import executive

    # The knowledge, past decisions, alerts digest, facts, lessons and page.
    texts = ("Olivia Owner signs above $10k", "olivia@example.com wrote", None, "Ask olivia", "x", "y")
    assert executive._for_demo_turn(*texts) == (
        "The principal signs above $10k", "The principal wrote", None, "Ask The principal", "x", "y",
    )
    # In both turn paths (any surface: the web chat, the panel, MCP, chat apps).
    source = inspect.getsource(executive.Executive)
    assert source.count("= _for_demo_turn(") == 2

    def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("roster unreadable")

    monkeypatch.setattr(people_store, "list_people", broken)
    assert executive._for_demo_turn("Olivia Owner", None) == ("", None)  # fails closed


def test_the_scrubber_covers_any_case_and_the_principals_email(roster: SimpleNamespace) -> None:
    from openexecutive.people.demo import principal_name_pattern, scrub_principal_name

    pattern = principal_name_pattern()
    text = "OLIVIA OWNER (olivia@example.com) and olivia; not Olivias or bob.olivia.x"
    assert scrub_principal_name(text, pattern) == (
        "The principal (The principal) and The principal; not Olivias or bob.olivia.x"
    )


def test_a_demo_persons_audit_rows_name_the_principal_by_role(roster: SimpleNamespace) -> None:
    from openexecutive.api.routes import audit as audit_route
    from openexecutive.audit.logger import AuditLogger

    logger = AuditLogger(db_path=people_store.DB_PATH)
    row = logger.log(
        "tool_invocation", "Read Olivia Owner's quote", details={"result_preview": "owner: olivia@example.com"},
        full={"result": "Created by Olivia Owner"}, private_to_person=roster.dana,
    )
    app = FastAPI()
    app.include_router(audit_route.router)
    app.state.audit = logger
    client = TestClient(app)
    dana = _as("dana@prospect.example")
    rows = client.get("/audit/logs", headers=dana).json()["items"]
    assert [r["summary"] for r in rows] == ["Read The principal's quote"]
    assert rows[0]["details"]["result_preview"] == "owner: The principal"
    one = client.get(f"/audit/logs/{row}", headers=dana).json()
    assert one["full"] == {"result": "Created by The principal"}
    # A search can't confirm the name is in a row.
    assert client.get("/audit/logs", params={"q": "Olivia"}, headers=dana).json()["items"] == []


def test_no_one_shares_a_chat_with_a_demo_person(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    from openexecutive.api.routes import chat, sessions

    source = inspect.getsource(sessions.put_session_shares)
    assert "or person.demo:" in source
    # And a share made before that was refused stays shut to them.
    from openexecutive.memory import session_store

    monkeypatch.setattr(session_store, "is_shared_with", lambda *_a, **_k: True)
    monkeypatch.setattr(chat, "_mail_private", lambda *_a: False)
    assert not chat._shared_member("s-old", roster.dana)
    assert chat._shared_member("s-old", roster.teammate)


def test_a_teammate_who_shares_the_first_name_keeps_it_on_home(roster: SimpleNamespace) -> None:
    from openexecutive.api.routes import today

    namesake = people_store.upsert_person(full_name="Olivia Chen", email="ochen@example.com")
    people_registry.invalidate()
    response = SimpleNamespace(
        people=[
            SimpleNamespace(id=roster.principal, full_name="Olivia Owner", is_principal=True),
            SimpleNamespace(id=namesake, full_name="Olivia Chen", is_principal=False),
        ],
        awaiting=[], in_flight=[], handled_overnight=[], proposals=[],
    )
    today._hide_other_demo_people(response, caller=roster.dana, principal=False)  # type: ignore[arg-type]
    assert [p.full_name for p in response.people] == ["The principal", "Olivia Chen"]


def _tool_round(name: str) -> SimpleNamespace:
    from unittest.mock import MagicMock

    block = MagicMock()
    block.type = "tool_use"
    block.id = "toolu_demo_1"
    block.name = name
    block.input = {}
    block.model_dump.return_value = {"type": "tool_use", "id": "toolu_demo_1", "name": name, "input": {}}
    return SimpleNamespace(content=[block], stop_reason="tool_use")


def _stream_of(final_msg: object) -> Any:
    from unittest.mock import AsyncMock, MagicMock

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)

    async def _no_deltas() -> Any:
        if False:
            yield None  # pragma: no cover

    cm.__aiter__ = lambda self=cm: _no_deltas()
    cm.get_final_message = AsyncMock(return_value=final_msg)
    return cm


def _tool_results_after_one_call(monkeypatch: pytest.MonkeyPatch, *, demo: bool, unread: bool = False) -> list[str]:
    """Drive two model rounds: the model calls a tool whose result names the
    principal (its name is the tool's, which the result echoes), then
    answers. What the second round was sent as tool results comes back."""
    import asyncio

    from openexecutive.orchestrator import executive as executive_mod
    from openexecutive.orchestrator.session import Session
    from openexecutive.people.demo import principal_name_pattern

    if unread:
        def broken() -> None:
            raise RuntimeError("people.db locked")

        monkeypatch.setattr(executive_mod, "principal_name_pattern", broken)
    else:
        monkeypatch.setattr(executive_mod, "principal_name_pattern", lambda: principal_name_pattern(["Olivia Owner"]))
    rounds = [_tool_round("Olivia Owner"), SimpleNamespace(content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn")]
    sent: list[list[dict[str, Any]]] = []

    def fake_stream(**kwargs: Any) -> Any:
        sent.append(kwargs["messages"])
        return _stream_of(rounds[len(sent) - 1])

    monkeypatch.setattr(executive_mod, "get_provider", lambda *a, **k: SimpleNamespace(messages_stream=fake_stream))
    asyncio.run(executive_mod.Executive().chat("who approves a $12,000 order?", Session(session_id="demo-t", demo_caller=demo)))
    assert len(sent) == 2
    return [
        str(block.get("content"))
        for message in sent[1]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def test_a_demo_turns_tool_results_name_the_principal_by_role(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _tool_results_after_one_call(monkeypatch, demo=True)
    assert results and all("Olivia" not in r for r in results)
    assert any("The principal" in r for r in results)


def test_anyone_elses_tool_results_are_left_as_they_are(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _tool_results_after_one_call(monkeypatch, demo=False)
    assert any("Olivia Owner" in r for r in results)


def test_a_demo_turn_withholds_tool_results_when_the_roster_cant_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.people.demo import DEMO_PRINCIPAL_WITHHELD

    results = _tool_results_after_one_call(monkeypatch, demo=True, unread=True)
    assert results == [DEMO_PRINCIPAL_WITHHELD]


def test_a_demo_persons_header_reader_names_the_principal_by_role(roster: SimpleNamespace) -> None:
    """The model is never handed the principal's name to write into a demo
    person's header: "the principal" stays as it is, not a slip, and isn't
    turned into the name (or a surname the scrub wouldn't catch)."""
    from openexecutive.api.routes import today
    from openexecutive.briefing import reader as reader_mod

    demo = today._narrative_reader(f"person:{roster.dana}", SimpleNamespace(id=roster.dana))  # type: ignore[arg-type]
    team = today._narrative_reader(f"person:{roster.teammate}", SimpleNamespace(id=roster.teammate))  # type: ignore[arg-type]
    assert demo is not None and demo.owner_by_role()
    assert team is not None and team.owner == "Olivia Owner"
    text = "The principal asked for the Metro invoice."
    assert reader_mod.slips(text, demo) == []
    assert reader_mod.fix_slips(text, demo) == text
    assert reader_mod.fix_slips(text, team) == "Olivia Owner asked for the Metro invoice."


def test_a_demo_persons_header_never_reads_another_demo_persons_activity(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import today
    from openexecutive.briefing import narrative

    rows = [
        {"kind": "advice_given", "summary": "Advised Pete Prospect on volume pricing", "department": "sales"},
        {"kind": "decision", "summary": "Moved the Metro follow-up to Friday", "department": "sales"},
    ]
    seen: list[list[dict[str, str]]] = []
    monkeypatch.setattr(narrative, "render_briefing_context", lambda **k: seen.append(k["activity"]) or "ctx")
    monkeypatch.setattr(today, "_nothing_needs_attention", lambda *_a: False)
    monkeypatch.setattr(today, "_narrative_activity", lambda *_a: list(rows))
    monkeypatch.setattr(today, "_with_due_soon", lambda data, *_a: data)
    for pid in (roster.dana, roster.teammate):
        today._narrative_context({}, SimpleNamespace(id=pid), {"name": "x"}, reader=None)  # type: ignore[arg-type]
    assert [r["summary"] for r in seen[0]] == ["Moved the Metro follow-up to Friday"]
    assert len(seen[1]) == 2  # a teammate's is unchanged


def test_a_header_naming_another_demo_person_is_not_shown(roster: SimpleNamespace) -> None:
    from openexecutive.api.routes import today

    def home(text: str) -> SimpleNamespace:
        return SimpleNamespace(people=[], awaiting=[], in_flight=[], handled_overnight=[], proposals=[], narrative=text)

    named = home("Pete Prospect asked about Metro.")
    today._hide_other_demo_people(named, caller=roster.dana, principal=False)  # type: ignore[arg-type]
    assert named.narrative is None
    plain = home("Olivia Owner wants the Metro invoice chased.")
    today._hide_other_demo_people(plain, caller=roster.dana, principal=False)  # type: ignore[arg-type]
    assert plain.narrative == "The principal wants the Metro invoice chased."

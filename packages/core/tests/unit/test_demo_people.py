"""Demo people (people/demo.py): never an approver, sent nothing, a turn that
only reads and advises, and a cap on questions a day, in the web chat and in
ERPNext's panel."""
from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.memory import episodic
from openexecutive.orchestrator import executive, outbound_guard, schedule_tools
from openexecutive.orchestrator.session import Session
from openexecutive.people import demo as people_demo
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "test.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


def _demo(name: str = "Dana Demo", email: str = "dana@example.com", **kw: object) -> int:
    pid = people_store.upsert_person(full_name=name, email=email, role="Demo user", **kw)  # type: ignore[arg-type]
    people_store.link_directory(pid, f"kc-{email}")
    assert people_store.set_directory_demo(pid, True)
    return pid


def test_the_mark_reads_back_and_only_a_managed_person_takes_it() -> None:
    pid = _demo()
    person = people_store.get_person(pid)
    assert person is not None and person.demo
    unmanaged = people_store.upsert_person(full_name="Hand Made", email="hand@example.com")
    assert not people_store.set_directory_demo(unmanaged, True)
    assert not people_store.get_person(unmanaged).demo  # type: ignore[union-attr]
    # Unlinked from the directory, the mark goes too.
    people_store.unlink_directory(pid)
    assert not people_store.get_person(pid).demo  # type: ignore[union-attr]


def test_a_demo_person_never_approves_whatever_their_scopes() -> None:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="o@example.com")
    people_store.set_authority_scope(principal, [AuthorityScope.WILDCARD])
    demo = _demo()
    people_store.set_authority_scope(demo, [AuthorityScope.SPEND_LT_2K, AuthorityScope.WILDCARD])
    approvers = people_store.find_approvers(AuthorityScope.SPEND_LT_2K)
    assert [p.id for p in approvers] == [principal]


def test_questions_a_day_are_capped(db: Path) -> None:
    pid = _demo()
    assert people_demo.take_question(pid, 2, day="2026-10-25")
    assert people_demo.take_question(pid, 2, day="2026-10-25")
    assert not people_demo.take_question(pid, 2, day="2026-10-25")
    assert people_demo.questions_today(pid, day="2026-10-25") == 2
    # A new day starts again; 0 means no cap.
    assert people_demo.take_question(pid, 2, day="2026-10-26")
    assert all(people_demo.take_question(pid, 0, day="2026-10-25") for _ in range(5))


def test_all_demo_people_together_are_capped_too(db: Path) -> None:
    dana, vic = _demo(), _demo("Vic Visitor", "vic@example.com")
    day = "2026-11-03"
    assert people_demo.take(dana, 50, 3, day=day).ok
    assert people_demo.take(vic, 50, 3, day=day).ok
    assert people_demo.take(vic, 50, 3, day=day).ok
    refused = people_demo.take(dana, 50, 3, day=day)
    assert refused == people_demo.Taken(False, site=True, first_site_refusal=True)
    # Only the first refusal of the day tells the principal.
    assert people_demo.take(vic, 50, 3, day=day) == people_demo.Taken(False, site=True)
    # A refused question counts toward neither cap.
    assert people_demo.questions_today(dana, day=day) == 1
    assert people_demo.questions_today(vic, day=day) == 2
    # A new day starts again; 0 means no site cap.
    assert people_demo.take(dana, 50, 3, day="2026-11-04").ok
    assert all(people_demo.take(dana, 50, 0, day=day).ok for _ in range(5))


def test_a_person_past_their_own_cap_takes_nothing_from_the_site(db: Path) -> None:
    dana, vic = _demo(), _demo("Vic Visitor", "vic@example.com")
    day = "2026-11-03"
    assert people_demo.take(dana, 1, 2, day=day).ok
    for _ in range(3):
        assert people_demo.take(dana, 1, 2, day=day) == people_demo.Taken(False)
    # Dana's refusals left the site's second question for Vic.
    assert people_demo.take(vic, 1, 2, day=day).ok


def test_the_site_cap_refuses_politely_audits_once_and_tells_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from fastapi import HTTPException

    import openexecutive.audit as audit
    from openexecutive.api.routes import chat
    from openexecutive.scheduler import runner

    events: list[tuple[str, dict[str, object]]] = []
    told: list[tuple[str, str]] = []

    def _log(event_type: str, _summary: str, **kw: object) -> None:
        events.append((event_type, kw.get("details")))  # type: ignore[arg-type]

    async def _deliver(text: str, *, label: str = "Update") -> runner.PrincipalDelivery:
        told.append((label, text))
        return runner.PrincipalDelivery(True, "slack", "sent", "slack_dm")

    monkeypatch.setattr(audit, "log_event", _log)
    monkeypatch.setattr(runner, "_deliver_to_principal", _deliver)
    monkeypatch.setenv("DEMO_DAILY_QUESTIONS_TOTAL", "1")
    dana, vic = _demo(), _demo("Vic Visitor", "vic@example.com")

    async def _ask() -> None:
        await chat._take_demo_questions(dana, 1)
        for _ in range(2):
            with pytest.raises(HTTPException) as refused:
                await chat._take_demo_questions(vic, 1)
            assert refused.value.status_code == 429
            assert refused.value.detail == people_demo.SITE_LIMIT_REACHED
        await asyncio.gather(*people_demo._notices)

    asyncio.run(_ask())
    assert [e for e, _ in events] == ["demo_site_limit"]
    assert events[0][1]["limit"] == 1  # type: ignore[index]
    assert told == [("Demo limit", people_demo.site_limit_notice(1))]
    assert "DEMO_DAILY_QUESTIONS_TOTAL" in told[0][1]
    # The principal and teammates are never counted or refused.
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="o@example.com")
    teammate = people_store.upsert_person(full_name="Tom Team", email="tom@example.com")
    asyncio.run(chat._take_demo_questions(principal, 5))
    asyncio.run(chat._take_demo_questions(teammate, 5))


def test_an_add_or_upload_the_route_refuses_spends_no_question(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from openexecutive.api.main import create_app
    from openexecutive.api.routes import chat

    spent: list[int] = []

    async def _count(person_id: int | None, n: int) -> None:
        spent.append(n)

    monkeypatch.setattr(chat, "_take_demo_questions", _count)
    monkeypatch.setattr(chat, "_MAX_BYTES_PER_FILE", 4)
    client = TestClient(create_app())
    # No live turn: 404, nothing counted.
    added = client.post("/chat/add", json={"client_turn_id": "no-such-turn", "message_id": "message-1", "message": "."})
    assert added.status_code == 404
    # A file too large: 413, nothing counted.
    upload = client.post(
        "/chat/upload", data={"message": "see"}, files={"files": ("a.txt", b"too big", "text/plain")}
    )
    assert upload.status_code == 413
    assert spent == []


def test_every_demo_question_goes_through_admit() -> None:
    from openexecutive.api.routes import chat
    from openexecutive.integrations import erpnext_panel

    for module in (chat, erpnext_panel):
        source = inspect.getsource(module)
        assert "people_demo.admit(" in source, module.__name__
        assert "people_demo.take_question(" not in source, module.__name__


def test_a_count_that_cant_be_written_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("disk")

    monkeypatch.setattr(people_store, "_get_conn", broken)
    assert not people_demo.take_question(1, 50)
    assert people_demo.take(1, 0, 500) == people_demo.Taken(False)


def test_a_roster_that_cant_be_read_is_said_so_in_the_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from fastapi import HTTPException

    from openexecutive.api.routes import chat

    monkeypatch.setattr(people_demo, "demo_state", lambda _pid: None)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(chat._take_demo_questions(7, 1))
    assert refused.value.status_code == 503 and "can't be read" in refused.value.detail
    assert 'raise HTTPException(status_code=503, detail="People can\'t be read just now.' in inspect.getsource(chat)


def test_who_is_demo_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _demo()
    other = people_store.upsert_person(full_name="Tom Team", email="tom@example.com")
    assert people_demo.is_demo_person(pid) and not people_demo.is_demo_person(other)
    assert not people_demo.is_demo_person(None)

    def broken(_pid: int) -> None:
        raise RuntimeError("roster")

    monkeypatch.setattr(people_store, "get_person", broken)
    assert people_demo.is_demo_person(other)


def test_the_executive_sends_a_demo_person_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _demo(email="dana@example.com", slack_user_id="U-DANA")
    people_store.upsert_person(full_name="Tom Team", email="tom@example.com", slack_user_id="U-TOM")
    for channel, ref in (("email", "dana@example.com"), ("email", "dana@example.com|thread-1"), ("slack_dm", "U-DANA")):
        reason = outbound_guard.check_outbound_allowed(channel, ref, "Hello")
        assert reason is not None and "demo user" in reason, (channel, ref)
    assert outbound_guard.check_outbound_allowed("slack_dm", "U-TOM", "Hello") is None


def test_no_mail_reaches_a_demo_person() -> None:
    from openexecutive.orchestrator import mcp_gateway as gw

    _demo(email="dana@example.com")
    people_store.upsert_person(full_name="Tom Team", email="tom@example.com")
    allow = gw._roster_allow_set()
    assert "tom@example.com" in allow and "dana@example.com" not in allow


def test_a_demo_persons_mail_gets_no_turn_and_no_reply() -> None:
    from openexecutive.integrations import email_poller

    source = inspect.getsource(email_poller._handle_mail)
    skip = source.index("if roster_sender is not None and getattr(roster_sender, \"demo\", False) is True:")
    # Before any turn or reply is prepared, and the mail is marked handled.
    assert skip < source.index("sender_in_roster = roster_sender is not None")
    assert "return \"left a demo person's mail unanswered\"" in source


def test_every_surface_holds_a_demo_speaker_to_the_narrow_tools() -> None:
    source = inspect.getsource(executive)
    marked = "session.demo_caller = session.demo_caller or await asyncio.to_thread(is_demo_person, speaker)"
    # Both chat paths (the stream and committee review) mark it after binding
    # the speaker, Google Chat's gate sender included.
    assert source.count("session.caller_person_id = person_id\n" + " " * 8 + "# A demo person's turn") == 2
    assert source.count(marked) == 2
    assert source.count('getattr(session, "gate_sender_person_id", None)') == 2


def test_a_demo_person_approves_none_of_anyone_elses_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from openexecutive.api.routes import chat, decisions

    demo = _demo()
    monkeypatch.setattr(chat, "_resolve_caller_person_id", lambda _r: demo)
    card = SimpleNamespace(id=1, decision_class="meeting_scheduling", approver_person_id=demo)
    with pytest.raises(HTTPException) as refused:
        decisions._resolver(card, request=None)  # type: ignore[arg-type]
    assert refused.value.status_code == 403
    # Their own card (one only its person answers) stays theirs.
    monkeypatch.setattr(decisions, "_approver_only", lambda _i: True)
    assert decisions._resolver(card, request=None) == demo  # type: ignore[arg-type]

    from openexecutive.api.routes import alerts

    assert "if not _caller_is_principal(request) and is_demo_person(approver):" in inspect.getsource(alerts)


def test_added_messages_and_files_count_as_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from fastapi import HTTPException

    from openexecutive.api.routes import chat

    demo = _demo()
    monkeypatch.setenv("DEMO_DAILY_QUESTIONS", "3")
    asyncio.run(chat._take_demo_questions(demo, 2))
    with pytest.raises(HTTPException) as refused:
        asyncio.run(chat._take_demo_questions(demo, 2))
    assert refused.value.status_code == 429
    assert people_demo.questions_today(demo) == 3
    # Nobody else is counted.
    other = people_store.upsert_person(full_name="Tom Team", email="tom@example.com")
    asyncio.run(chat._take_demo_questions(other, 10))
    source = inspect.getsource(chat)
    assert "await _take_demo_questions(_resolve_caller_person_id(request), 1)" in source
    assert "await _take_demo_questions(_resolve_caller_person_id(request), len(files))" in source


def test_a_demo_turn_is_told_why_a_tool_isnt_there() -> None:
    error = json.loads(schedule_tools.erpnext_panel_withheld_error("send_email", demo=True))["error"]
    assert "demo" in error and "Do not retry" in error
    # Nov 1, F2: "Say what you would do instead" drew "no tool could do it…
    # I worked it by hand" into a reply that asked for nothing to be done.
    assert "If the person asked for this to be done" in error
    assert "don't mention tools or what you can't run" in error
    assert "ERPNext's panel" in json.loads(schedule_tools.erpnext_panel_withheld_error("send_email"))["error"]


def test_a_demo_turn_gets_the_panels_narrow_tools() -> None:
    assert people_demo.demo_turn(Session(session_id="s", demo_caller=True))
    assert not people_demo.demo_turn(Session(session_id="s"))
    assert not people_demo.demo_turn(None)
    source = inspect.getsource(executive)
    # The panel's tool list, its dispatch guard and no web search all key off panel_turn.
    assert "panel_turn = erpnext_panel_turn(current_session.get()) or demo_speaker" in source
    assert 'and (not panel_turn or t["name"] in panel_tools)' in source
    assert "web_search_tool = None if panel_turn else build_web_search_tool()" in source


def test_the_web_chat_marks_the_turn_and_refuses_past_the_cap() -> None:
    from openexecutive.api.routes import chat

    source = inspect.getsource(chat)
    assert "demo_known = await asyncio.to_thread(people_demo.demo_state, caller_person_id)" in source
    assert "if demo_caller and (refused := await people_demo.admit(caller_person_id)) is not None:" in source
    assert "raise HTTPException(status_code=429, detail=refused)" in source
    assert "session.demo_caller = demo_caller" in source


# ── ERPNext's panel ────────────────────────────────────────────────────────


def test_the_panel_caps_a_demo_persons_questions_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.caller import JtiLedger
    from openexecutive.integrations import erpnext_panel
    from openexecutive.integrations.erpnext_panel import ASSERTION_HEADER, PATH

    from .test_erpnext_panel import ASKER, _Signer

    signer = _Signer()
    monkeypatch.setenv("ERPNEXT_PANEL_PUBLIC_KEYS", signer.public_keys)
    monkeypatch.setenv("DEMO_DAILY_QUESTIONS", "1")
    monkeypatch.setattr(erpnext_panel, "_ledger", JtiLedger())
    monkeypatch.setattr(erpnext_panel, "_rate", erpnext_panel._RateLimiter(10, 3600))
    monkeypatch.setattr(erpnext_panel, "_ready", lambda: None)
    pid = _demo(email=ASKER)
    monkeypatch.setattr(erpnext_panel, "_person_for", lambda email: SimpleNamespace(id=pid, full_name="D", demo=True))

    async def answer(*_a: object) -> str:
        return "Fine."

    monkeypatch.setattr(erpnext_panel, "_answer", answer)
    app = FastAPI()
    app.include_router(erpnext_panel.router)
    client = TestClient(app)

    def ask(conversation: str) -> object:
        body = json.dumps({"message": "hi", "conversation": conversation}).encode()
        return client.post(
            PATH, content=body,
            headers={"content-type": "application/json", ASSERTION_HEADER: signer.sign(body, sub=ASKER)},
        )

    assert ask("c1").status_code == 200  # type: ignore[attr-defined]
    refused = ask("c2")
    assert refused.status_code == 429 and refused.json()["error"] == "daily_limit"  # type: ignore[attr-defined]


def test_a_demo_persons_panel_turn_is_a_demo_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel names its asker by email only, so it marks the turn itself:
    otherwise the org block lists the whole team with their addresses."""
    import asyncio

    from openexecutive.integrations import erpnext_panel
    from openexecutive.onboarding import profile_builder
    from openexecutive.orchestrator import executive as executive_mod

    seen: list[bool] = []

    class _Executive:
        def __init__(self, **_k: object) -> None:
            pass

        async def chat(self, *, session: Session, **_k: object) -> str:
            seen.append(people_demo.demo_turn(session))
            return "Fine."

    monkeypatch.setattr(executive_mod, "Executive", _Executive)
    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: SimpleNamespace(is_empty=lambda: True))
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda **_k: "")
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    demo = _demo(email="dana@prospect.example")
    teammate = people_store.upsert_person(full_name="Tom Team", email="tom@example.com")
    for pid, email in ((demo, "dana@prospect.example"), (teammate, "tom@example.com")):
        person = people_store.get_person(pid)
        asyncio.run(erpnext_panel._answer(person, email, "c1", "Who is on the team?", None))
    assert seen == [True, False]


def test_a_demo_person_has_no_hourly_cap_in_the_panel_only_the_days(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.caller import JtiLedger
    from openexecutive.integrations import erpnext_panel
    from openexecutive.integrations.erpnext_panel import ASSERTION_HEADER, PATH

    from .test_erpnext_panel import ASKER, _Signer

    signer = _Signer()
    monkeypatch.setenv("ERPNEXT_PANEL_PUBLIC_KEYS", signer.public_keys)
    monkeypatch.setenv("DEMO_DAILY_QUESTIONS", "3")
    monkeypatch.setattr(erpnext_panel, "_ledger", JtiLedger())
    monkeypatch.setattr(erpnext_panel, "_rate", erpnext_panel._RateLimiter(1, 3600))
    monkeypatch.setattr(erpnext_panel, "_ready", lambda: None)
    pid = _demo(email=ASKER)
    demo = {"value": True}
    monkeypatch.setattr(
        erpnext_panel, "_person_for", lambda email: SimpleNamespace(id=pid, full_name="D", demo=demo["value"])
    )

    async def answer(*_a: object) -> str:
        return "Fine."

    monkeypatch.setattr(erpnext_panel, "_answer", answer)
    app = FastAPI()
    app.include_router(erpnext_panel.router)
    client = TestClient(app)

    def ask(conversation: str) -> object:
        body = json.dumps({"message": "hi", "conversation": conversation}).encode()
        return client.post(
            PATH, content=body,
            headers={"content-type": "application/json", ASSERTION_HEADER: signer.sign(body, sub=ASKER)},
        )

    # An hourly limit of 1, a daily cap of 3: the demo person gets 3, then the day's 429.
    assert [ask(f"c{i}").status_code for i in range(3)] == [200, 200, 200]  # type: ignore[attr-defined]
    assert ask("c3").json()["error"] == "daily_limit"  # type: ignore[attr-defined]
    # Anyone else still meets the hour's limit.
    demo["value"] = False
    monkeypatch.setattr(erpnext_panel, "_rate", erpnext_panel._RateLimiter(1, 3600))
    assert ask("t1").status_code == 200  # type: ignore[attr-defined]
    assert ask("t2").json()["error"] == "rate_limited"  # type: ignore[attr-defined]

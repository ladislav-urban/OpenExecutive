"""The turn's cost cap and what a cap does to a turn (audit.cost_caps,
orchestrator/executive.py): S7 on top of the person, job and month caps.

A turn is everything one message causes (or one unattended run). At any cap
it stops with a plain sentence after what it already wrote, never a silent
cut and never an empty reply. The reply checks are kept off the turn cap
only, and a check a cap refuses holds the reply rather than passing it.
"""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.audit import cost_caps
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.providers.capped import CappedProvider

from ._agent_loop_fakes import FakeStream, FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock

PACKAGE = Path(cost_caps.__file__).resolve().parents[1]
STOP = cost_caps.TURN_STOP_REPLY
BEFORE = "I will check actual stock in every warehouse first, then compare it with the reorder level."


class _Streaming(FakeStream):
    """Streams each text block as a delta before the final message, as the
    API does."""

    def __init__(self, final_msg: FinalMsg) -> None:
        super().__init__(final_msg)
        self._deltas = [
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=b.text))
            for b in final_msg.content
            if getattr(b, "type", "") == "text"
        ]

    async def __anext__(self) -> Any:
        if not self._deltas:
            raise StopAsyncIteration
        return self._deltas.pop(0)


class _Backend(ScriptedProvider):
    def messages_stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        return _Streaming(self._final_msgs.pop(0))


@pytest.fixture()
def caps(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Only the turn cap on, every call's worst case 1 USD, no DB, and each
    refusal's audit row kept here."""
    state = SimpleNamespace(turn=1.5, rows=[])
    monkeypatch.setattr(cost_caps, "caps", lambda: cost_caps.Caps(0, 0, 0, turn=state.turn))
    monkeypatch.setattr(cost_caps, "worst_case", lambda model, kwargs: 1.0)
    monkeypatch.setattr(cost_caps, "_write", lambda *a, **k: None)
    monkeypatch.setattr(cost_caps, "_tell_principal", lambda exc, now: None)

    def _log(event_type: str, summary: str, **kw: Any) -> None:
        state.rows.append({"event_type": event_type, "summary": summary, **kw})

    monkeypatch.setattr("openexecutive.audit.log_event", _log)
    return state


def _turn_reply(rounds: list[FinalMsg], *, session: Session | None = None, collecting: bool = False) -> str:
    """The text one turn of ``rounds`` yields, every call held to the caps."""
    return _turn(rounds, session=session, collecting=collecting)[0]


def _turn(
    rounds: list[FinalMsg], *, session: Session | None = None, collecting: bool = False,
) -> tuple[str, int]:
    """The turn's text, and how many rounds the backend was asked for."""
    from openexecutive.orchestrator import executive as executive_mod

    backend = _Backend(rounds)
    provider = CappedProvider(backend)  # type: ignore[arg-type]
    text: list[str] = []

    async def go() -> None:
        token = executive_mod._COLLECTING.set(collecting)
        try:
            with (
                patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
                patch("openexecutive.orchestrator.executive.audit_log", lambda *_a, **_k: None),
                set_session(session or Session()),
                cost_caps.bind_turn(fresh=True),
            ):
                async for item in Executive()._stream_agent_loop(
                    system_blocks=[], messages=[{"role": "user", "content": "x"}],
                    model="claude-test", workspace_mode="team", principal_role_tag="", turn_id="t-1",
                ):
                    if isinstance(item, str) and item != Executive._THINKING:
                        text.append(item)
        finally:
            executive_mod._COLLECTING.reset(token)

    asyncio.run(go())
    return "".join(text), len(backend.calls)


# --------------------------------------------------------------------------- #
# The turn's accumulator
# --------------------------------------------------------------------------- #


def test_the_turn_cap_counts_every_call_of_one_turn_across_threads(caps: SimpleNamespace) -> None:
    caps.turn = 3.5
    model = "claude-test"

    async def go() -> list[Any]:
        with cost_caps.bind_turn(fresh=True) as turn:
            # Specialists reserve together, each in a worker thread.
            got = await asyncio.gather(
                *(asyncio.to_thread(cost_caps.reserve, model, {}, tell=False) for _ in range(5)),
                return_exceptions=True,
            )
            assert turn.spent == 3.0
        return got

    got = asyncio.run(go())
    refused = [g for g in got if isinstance(g, cost_caps.CostCapReached)]
    assert len(refused) == 2 and {r.scope for r in refused} == {"turn"}
    assert all(r.worst_case == 1.0 for r in refused)


def test_a_check_and_add_is_one_step_under_the_lock() -> None:
    turn = cost_caps.TurnSpend()
    taken: list[bool] = []
    start = threading.Barrier(8)

    def take() -> None:
        start.wait()
        taken.append(turn.take(1.0, 4.0)[0])

    threads = [threading.Thread(target=take) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert taken.count(True) == 4 and turn.spent == 4.0


def test_settling_and_releasing_put_the_turn_right(caps: SimpleNamespace) -> None:
    with cost_caps.bind_turn(fresh=True) as turn:
        first = cost_caps.reserve("claude-test", {}, tell=False)
        assert turn.spent == 1.0
        message = SimpleNamespace(usage=SimpleNamespace(
            input_tokens=10, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ))
        with patch.object(cost_caps, "charge", lambda model, counts: 0.25):
            cost_caps.settle(first, "claude-test", message)
        assert turn.spent == pytest.approx(0.25)
        second = cost_caps.reserve("claude-test", {}, tell=False)
        cost_caps.release(second)
        assert turn.spent == pytest.approx(0.25)


def test_a_closed_turn_and_a_spawned_task_count_against_no_turn(caps: SimpleNamespace) -> None:
    caps.turn = 1.5
    with cost_caps.bind_turn(fresh=True) as turn:
        cost_caps.reserve("claude-test", {}, tell=False)
        with cost_caps.unbind_turn():
            # A background task the turn spawned: never refused by it.
            for _ in range(3):
                cost_caps.reserve("claude-test", {}, tell=False)
        assert turn.spent == 1.0
    assert turn.closed
    # Charged after the turn ended: counts against no turn.
    turn.adjust(5.0)
    assert turn.spent == 1.0
    assert cost_caps.attribution()["turn_id"] is None


def test_one_message_is_one_turn_however_many_layers_bind_it() -> None:
    with cost_caps.bind_turn(fresh=True) as outer:
        with cost_caps.bind_turn() as inner:
            assert inner is outer
        assert not outer.closed
        with cost_caps.bind_turn(fresh=True) as run:
            assert run is not outer
        assert run.closed and cost_caps.current_turn() is outer
    assert cost_caps.current_turn() is None


def test_the_turn_id_is_in_each_calls_attribution(caps: SimpleNamespace) -> None:
    with cost_caps.bind_turn(fresh=True) as turn:
        assert cost_caps.attribution()["turn_id"] == turn.id
        with cost_caps._check_exempt():
            # A reply check is charged to no turn.
            assert cost_caps.attribution()["turn_id"] is None


def test_the_reply_checks_are_kept_off_the_turn_cap_only(caps: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    caps.turn = 1.5
    with cost_caps.bind_turn(fresh=True) as turn:
        cost_caps.reserve("claude-test", {}, tell=False)
        with cost_caps._check_exempt():
            for _ in range(3):
                cost_caps.reserve("claude-test", {}, tell=False)
        assert turn.spent == 1.0
        with pytest.raises(cost_caps.CostCapReached):
            cost_caps.reserve("claude-test", {}, tell=False)
    # The month's cap still holds a check.
    monkeypatch.setattr(cost_caps, "caps", lambda: cost_caps.Caps(0, 0, 0.5, turn=1.5))
    monkeypatch.setattr(cost_caps, "_spent", lambda *a, **k: 0.0)
    with cost_caps.bind_turn(fresh=True), cost_caps._check_exempt(), pytest.raises(cost_caps.CostCapReached) as refused:
        cost_caps.reserve("claude-test", {}, tell=False)
    assert refused.value.scope == "total"


def test_only_the_three_reply_checks_use_the_exemption() -> None:
    users = sorted(
        str(path.relative_to(PACKAGE)).replace("\\", "/")
        for path in PACKAGE.rglob("*.py")
        if "_check_exempt" in path.read_text(encoding="utf-8")
    )
    assert users == [
        "audit/cost_caps.py",
        "briefing/grounding.py",
        "orchestrator/answer_check.py",
        "orchestrator/claim_check.py",
    ]


# --------------------------------------------------------------------------- #
# The stop
# --------------------------------------------------------------------------- #


def test_a_turn_capped_after_a_round_keeps_what_it_wrote_and_says_it_stopped(caps: SimpleNamespace) -> None:
    reply = _turn_reply([
        FinalMsg([TextBlock(BEFORE), ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([TextBlock("Never sent.")], "end_turn"),
    ])
    assert reply == f"{BEFORE}\n\n{STOP}"


def test_a_turn_capped_before_it_wrote_anything_is_the_sentence_alone(caps: SimpleNamespace) -> None:
    caps.turn = 0.5
    assert _turn_reply([FinalMsg([TextBlock("Never sent.")], "end_turn")]) == STOP


def test_the_turn_cap_holds_across_rounds(caps: SimpleNamespace) -> None:
    caps.turn = 2.5
    reply = _turn_reply([
        FinalMsg([ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([ToolUseBlock("tu2", "no_such_tool", {})], "tool_use"),
        FinalMsg([TextBlock("Never sent.")], "end_turn"),
    ])
    assert reply == STOP
    assert [r["details"]["scope"] for r in caps.rows if r["event_type"] == "cost_cap_reached"] == ["turn"]


def test_a_capped_specialist_ends_the_round_before_any_result_reaches_the_model(
    caps: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.orchestrator import executive as executive_mod

    async def capped_specialists(calls: list[Any], **_k: Any) -> list[str]:
        raise cost_caps.CostCapReached("turn", "t", 1.5, 1.0, datetime.now(UTC))

    monkeypatch.setattr(executive_mod, "route_parallel", capped_specialists)
    caps.turn = 10.0
    backend_rounds = [
        FinalMsg([TextBlock(BEFORE), ToolUseBlock("tu1", "consult_specialist", {"specialist": "cfo", "query": "q"})],
                 "tool_use"),
        FinalMsg([TextBlock("Never asked.")], "end_turn"),
    ]
    reply, asked = _turn(backend_rounds)
    assert reply == f"{BEFORE}\n\n{STOP}"
    assert asked == 1  # the second round was never asked


def test_route_parallel_raises_a_specialists_cap_rather_than_a_partial_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import router

    async def specialist(*, specialist_name: str, **_k: Any) -> str:
        if specialist_name == "cfo":
            raise cost_caps.CostCapReached("person", "4", 1.0, 1.0, datetime.now(UTC))
        return "An answer."

    async def nothing(*_a: Any, **_k: Any) -> str:
        return ""

    monkeypatch.setattr(router, "route_to_specialist", specialist)
    monkeypatch.setattr(router, "_retrieve_for_call", nothing)
    monkeypatch.setattr(router, "_retrieve_failures_for_call", nothing)
    monkeypatch.setattr(router, "_prefetch_department_for_call", nothing)
    calls = [{"specialist": "cfo", "query": "q"}, {"specialist": "cmo", "query": "q"}]
    with pytest.raises(cost_caps.CostCapReached):
        asyncio.run(router.route_parallel(calls, company_stage="growth", principal_role=""))


def test_a_capped_tool_is_never_a_tool_error_for_the_model(caps: SimpleNamespace) -> None:
    from openexecutive.orchestrator import executive as executive_mod

    async def capped_tool(_input: dict[str, Any]) -> str:
        raise cost_caps.CostCapReached("job", "watch:x", 1.0, 1.0, datetime.now(UTC))

    caps.turn = 10.0
    handlers = {**executive_mod._ALL_SKILL_HANDLERS, "capped_probe": capped_tool}
    with patch.object(executive_mod, "_ALL_SKILL_HANDLERS", handlers):
        reply, asked = _turn([
            FinalMsg([ToolUseBlock("tu1", "capped_probe", {})], "tool_use"),
            FinalMsg([TextBlock("Never asked.")], "end_turn"),
        ])
    assert reply == cost_caps.CostCapReached("job", "watch:x", 1.0, 1.0, datetime.now(UTC)).reply()
    assert asked == 1


def test_someone_off_people_hears_only_the_general_sentence(caps: SimpleNamespace) -> None:
    from openexecutive.providers.refusal import reply_text

    stranger = Session(origin_channel="email")
    stranger.sender_off_team = True  # type: ignore[attr-defined]
    reply = _turn_reply([
        FinalMsg([TextBlock(BEFORE), ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([TextBlock("Never sent.")], "end_turn"),
    ], session=stranger)
    assert reply == f"{BEFORE}\n\n{reply_text()}"
    assert "spend limit" not in reply


def test_the_refusals_audit_row_names_no_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    path = tmp_path / "caps.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_store.initialize_db(path)
    sara = people_store.upsert_person(full_name="Sara Teammate", email="sara@co.com", db_path=path)
    monkeypatch.setattr(cost_caps, "caps", lambda: cost_caps.Caps(1.0, 0, 0))
    monkeypatch.setattr(cost_caps, "_spent", lambda *a, **k: 0.9)
    monkeypatch.setattr(cost_caps, "worst_case", lambda model, kwargs: 0.2)
    monkeypatch.setattr(cost_caps, "_tell_principal", lambda exc, now: None)
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda event, summary, **kw: rows.append(
        {"event": event, "summary": summary, **kw}
    ))
    with set_session(Session(session_id="s-1", caller_person_id=sara)), pytest.raises(cost_caps.CostCapReached):
        cost_caps.reserve("claude-test", {})
    (row,) = rows
    assert row["event"] == "cost_cap_reached" and row["actor"] == "cost_cap" and row["principal_only"] is True
    details = row["details"]
    assert details["source"] == "cost_cap" and details["scope"] == "person" and details["key"] == str(sara)
    assert details["cap_usd"] == 1.0 and details["spent_usd"] == 0.9 and details["worst_case_usd"] == 0.2
    assert details["session_id"] == "s-1" and details["resets_at"].endswith("+00:00")
    dumped = json.dumps(row)
    assert "Sara" not in dumped and "sara@co.com" not in dumped


def test_the_principals_channel_message_is_a_fixed_template() -> None:
    exc = cost_caps.CostCapReached("person", "7", 1.0, 1.2, datetime.now(UTC), name="Sara Teammate")
    text = cost_caps.channel_text(exc)
    assert "Sara" not in text and "1.2" not in text
    assert text == "Someone on People reached today's usage limit. The details are on your Home page."


def test_a_turn_cap_makes_no_card(monkeypatch: pytest.MonkeyPatch) -> None:
    filed: list[Any] = []
    monkeypatch.setattr(cost_caps, "_alert_principal", lambda exc, now: filed.append(exc))
    cost_caps._tell_principal(cost_caps.CostCapReached("turn", "t", 1.0, 1.0, datetime.now(UTC)), None)
    assert filed == []


# --------------------------------------------------------------------------- #
# Held, never passed: a check a cap refused
# --------------------------------------------------------------------------- #


def test_a_capped_claim_check_withholds_the_held_tail(caps: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import claim_check

    async def capped(model: str, turn: str) -> dict[str, Any]:
        raise cost_caps.CostCapReached("person", "4", 1.0, 1.0, datetime.now(UTC))

    caps.turn = 10.0
    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "true")
    monkeypatch.setattr(claim_check, "_call_model", capped)
    # The short last reply ("Done, I removed it.") is held for the check, and
    # withheld when the check can't run; what streamed before stays.
    reply = _turn_reply([
        FinalMsg([TextBlock(BEFORE), ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([TextBlock("Done, I removed it.")], "end_turn"),
    ])
    person_sentence = "You've reached today's usage limit. It resets at midnight."
    assert reply == f"{BEFORE}\n\n{person_sentence}"
    assert "removed" not in reply


def test_a_claim_check_that_ran_lets_the_held_tail_through(caps: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import claim_check

    async def fine(model: str, turn: str) -> dict[str, Any]:
        return {"unbacked": False, "claim": ""}

    caps.turn = 10.0
    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "true")
    monkeypatch.setattr(claim_check, "_call_model", fine)
    assert _turn_reply([FinalMsg([TextBlock("Done, I removed it.")], "end_turn")]) == "Done, I removed it."


def test_a_capped_answer_check_holds_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import answer_check

    async def capped(*_a: Any, **_k: Any) -> str:
        raise cost_caps.CostCapReached("total", "all", 1.0, 1.0, datetime.now(UTC))

    monkeypatch.setattr(answer_check, "_check", capped)
    monkeypatch.setattr(answer_check, "enabled", lambda: True)
    turn = answer_check.AnswerCheckTurn()
    turn.results.append(object())  # type: ignore[arg-type]
    with pytest.raises(cost_caps.CostCapReached):
        asyncio.run(answer_check.check_reply("North owes $12,900.", turn, model="m"))


def test_a_collected_reply_whose_check_was_capped_is_the_sentence_alone() -> None:
    from openexecutive.orchestrator import executive as executive_mod

    cap = cost_caps.CostCapReached("total", "all", 1.0, 1.0, datetime.now(UTC))
    with cost_caps.bind_turn(fresh=True):
        token = executive_mod._COLLECTING.set(True)
        try:
            reply, event = Executive._capped_check("North owes $12,900.", cap, "s")
        finally:
            executive_mod._COLLECTING.reset(token)
        assert cost_caps.turn_stopped()
    assert reply == cap.reply() and "12,900" not in reply
    assert event == {"type": "reply_replaced", "content": cap.reply(), "session_id": "s"}


def test_a_streamed_reply_whose_check_was_capped_keeps_what_was_shown() -> None:
    cap = cost_caps.CostCapReached("total", "all", 1.0, 1.0, datetime.now(UTC))
    with cost_caps.bind_turn(fresh=True):
        reply, event = Executive._capped_check("North owes $12,900.", cap, "s")
    # The figures stay on screen, so what follows says they weren't checked.
    note = f"{cost_caps.UNCHECKED_FIGURES} {cap.reply()}"
    assert reply == f"North owes $12,900.\n\n{note}" and event == f"\n\n{note}"


@pytest.mark.asyncio
async def test_a_capped_grounding_repair_holds_the_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.briefing import grounding as g

    async def capped(*_a: Any, **_k: Any) -> str:
        raise cost_caps.CostCapReached("job", "scheduled:principal_brief_morning", 1.0, 1.0, datetime.now(UTC))

    monkeypatch.setattr(g, "grounding_mode", lambda: "enforce")
    monkeypatch.setattr(g, "_repair", capped)
    monkeypatch.setattr(g, "profile_sources", list)
    monkeypatch.setattr(g, "org_sources", list)
    monkeypatch.setattr(g, "roster", list)
    monkeypatch.setattr(g, "_audit", lambda *a, **k: None)
    with pytest.raises(cost_caps.CostCapReached):
        await g.ground_brief("- Marcus Lee flagged the lease.\n", context="Nothing.", kind="k", system="SYS")


# --------------------------------------------------------------------------- #
# Every way out: one reply, ending with the stop sentence
# --------------------------------------------------------------------------- #


def _chat(stream: Any, **kw: Any) -> str:
    executive = Executive()
    executive.stream_chat = stream  # type: ignore[method-assign]
    return asyncio.run(executive.chat(user_message="x", session=Session(), **kw))


def test_executive_chat_returns_one_reply_ending_with_the_stop_sentence() -> None:
    seen: list[Any] = []

    async def stream(**_kw: Any) -> Any:
        seen.append(cost_caps.current_turn())
        yield BEFORE
        cost_caps.mark_stopped(STOP)
        yield f"\n\n{STOP}"

    assert _chat(stream) == f"{BEFORE}\n\n{STOP}"
    assert seen[0] is not None and seen[0].closed


def test_executive_chat_keeps_a_stopped_turns_reply_when_a_later_step_fails() -> None:
    async def stream(**_kw: Any) -> Any:
        yield BEFORE
        cost_caps.mark_stopped(STOP)
        yield f"\n\n{STOP}"
        raise RuntimeError("after the stop")

    assert _chat(stream) == f"{BEFORE}\n\n{STOP}"

    async def unstopped(**_kw: Any) -> Any:
        yield BEFORE
        raise RuntimeError("no stop")

    with pytest.raises(RuntimeError):
        _chat(unstopped)


@pytest.mark.parametrize(("module", "function"), [
    ("integrations/slack_bot.py", "_handle_message"),
    ("integrations/telegram_bot.py", "_process_and_reply"),
    ("integrations/discord_bot.py", "_handle_message"),
    ("integrations/google_chat.py", "_process_and_reply"),
    ("integrations/email_poller.py", "_run_executive"),
    ("integrations/erpnext_panel.py", "_answer"),
    ("integrations/erpnext_a2a.py", "_answer"),
    ("mcp_server/server.py", "_ask_executive"),
])
def test_every_channel_handler_is_one_turn(module: str, function: str) -> None:
    tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == function]
    assert any(ast.unparse(d) == "cost_caps.as_turn" for d in fn.decorator_list)


def test_as_turn_binds_a_fresh_turn_and_closes_it() -> None:
    seen: list[Any] = []

    @cost_caps.as_turn
    async def handler(x: int) -> int:
        seen.append(cost_caps.current_turn())
        return x + 1

    async def go() -> int:
        with cost_caps.bind_turn(fresh=True) as outer:
            out = await handler(1)
            assert seen[0] is not outer
        return out

    assert asyncio.run(go()) == 2 and seen[0].closed


def test_the_other_entry_paths_bind_a_turn() -> None:
    # The web chat route binds one for the whole stream, Executive.stream_chat
    # one when nothing is bound (the CLI), the scheduler one per action and a
    # watch job one per run.
    route = (PACKAGE / "api/routes/chat.py").read_text(encoding="utf-8")
    assert "cost_caps.bind_turn(fresh=True)" in route
    executive = (PACKAGE / "orchestrator/executive.py").read_text(encoding="utf-8")
    assert executive.count("cost_caps.ensure_turn()") == 2
    scheduler = (PACKAGE / "scheduler/runner.py").read_text(encoding="utf-8")
    assert "cost_caps.run_as_turn(_execute_action(row, gateway))" in scheduler
    # Every periodic sweep runs under _sweep (its job and one turn).
    assert scheduler.count('with _sweep("') == 9
    # The other unattended and asked-for runs: one turn each.
    for module, binding in [
        ("api/routes/delegation.py", "cost_caps.run_as_turn(inbox.scan_person(person))"),
        ("orchestrator/take_the_lead.py", "cost_caps.bind_turn(fresh=True)"),
        ("delegation/action_cards.py", "cost_caps.bind_turn(fresh=True)"),
        ("alerts/pipeline.py", "cost_caps.bind_turn(fresh=True)"),
    ]:
        assert binding in (PACKAGE / module).read_text(encoding="utf-8"), module
    # The CLI closes a turn per message.
    assert (PACKAGE / "cli.py").read_text(encoding="utf-8").count("with cost_caps.bind_turn(fresh=True):") == 2

    async def seen() -> Any:
        return cost_caps.current_turn()

    async def go() -> Any:
        return await cost_caps.run_as_turn(seen())

    turn = asyncio.run(go())
    assert turn is not None and turn.closed


def test_stream_chat_binds_a_turn_when_none_is() -> None:
    async def go() -> Any:
        cost_caps.ensure_turn()
        return cost_caps.current_turn()

    assert asyncio.run(go()) is not None
    assert cost_caps.current_turn() is None


# --------------------------------------------------------------------------- #
# Watch runs
# --------------------------------------------------------------------------- #


def test_a_watch_run_a_cap_stopped_ends_capped_and_names_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.watch import runner as watch_runner
    from openexecutive.watch.jobs import _example

    turns: list[Any] = []

    async def capped_run(job: Any, day: str, reads: list[Any]) -> Any:
        turns.append(cost_caps.current_turn())
        assert cost_caps.current_job() == f"watch:{job.id}"
        raise cost_caps.CostCapReached("job", f"watch:{job.id}", 1.0, 1.0, datetime.now(UTC) + timedelta(days=1))

    notices: list[str] = []
    finished: list[Any] = []
    monkeypatch.setattr(watch_runner, "_run", capped_run)
    monkeypatch.setattr(watch_runner, "_finish", lambda job_id, day, outcome, reads, **k: finished.append(outcome))
    monkeypatch.setattr(watch_runner, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(watch_runner, "_take_failure_notice", lambda *a, **k: True)
    monkeypatch.setattr(watch_runner, "_fail_notice", lambda job, day, reason, read: notices.append(reason))
    outcome = asyncio.run(watch_runner.run_job(_example.JOB, "2026-11-17"))
    assert outcome.status == "capped" and outcome.error == "cost_cap_job"
    assert finished == [outcome] and notices == ["cost_cap_job"]
    assert "spend limit" in watch_runner.REASONS["cost_cap_job"]
    assert turns[0] is not None and turns[0].closed


def test_reason_lines_lets_a_cap_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.watch import registry
    from openexecutive.watch import runner as watch_runner
    from openexecutive.watch.jobs import _example

    async def capped(*_a: Any, **_k: Any) -> Any:
        raise cost_caps.CostCapReached("turn", "t", 1.0, 1.0, datetime.now(UTC))

    monkeypatch.setattr(watch_runner, "_ask", capped)
    job = dataclasses.replace(_example.JOB, reason_schema=registry.ReasonSchema("Why."))
    spec = registry.CardSpec(key="k", summary="A finding.", values=(), facts={"a": "b"})
    with pytest.raises(cost_caps.CostCapReached):
        asyncio.run(watch_runner.reason_lines(job, [spec]))
    per_finding = dataclasses.replace(
        job, reason_schema=registry.ReasonSchema("Why.", per_finding=True, max_findings=2),
    )
    with pytest.raises(cost_caps.CostCapReached):
        asyncio.run(watch_runner.reason_lines(per_finding, [spec]))


# --------------------------------------------------------------------------- #
# GET /audit/usage
# --------------------------------------------------------------------------- #


def test_the_usage_summary_counts_capped_calls_by_day_and_scope(tmp_path: Path) -> None:
    from openexecutive.audit.logger import AuditLogger

    audit = AuditLogger(tmp_path / "audit.db")
    for scope in ("turn", "turn", "person"):
        audit.log("cost_cap_reached", "capped", actor="cost_cap", details={"scope": scope}, principal_only=True)
    audit.log("cache_event", "a call", details={"model": "m", "input_tokens": 1})
    summary = audit.usage_summary()
    day = summary["by_day"][0]["day"]
    assert summary["capped_calls"] == [
        {"day": day, "scope": "person", "calls": 1}, {"day": day, "scope": "turn", "calls": 2},
    ]
    # The rows are the principal's alone.
    assert audit.usage_summary(include_private=False)["capped_calls"] == []


# --------------------------------------------------------------------------- #
# Security review (Nov 17): the fixes
# --------------------------------------------------------------------------- #


class _Snap:
    """A stream whose message snapshot is as the SDK keeps it."""

    def __init__(self, stop_reason: str | None) -> None:
        self.current_message_snapshot = SimpleNamespace(
            stop_reason=stop_reason,
            usage=SimpleNamespace(input_tokens=10, output_tokens=1, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0),
        )

    async def __aenter__(self) -> _Snap:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None


@pytest.mark.parametrize(("stop_reason", "settled_on_snapshot"), [(None, False), ("end_turn", True)])
def test_a_stream_closed_before_it_finished_keeps_its_worst_case(
    caps: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, stop_reason: str | None, settled_on_snapshot: bool,
) -> None:
    # message_start and some deltas, then Stop: output_tokens is still the
    # message_start count, so the snapshot would count next to nothing.
    settled: list[Any] = []
    real_settle = cost_caps.settle
    monkeypatch.setattr(cost_caps, "settle", lambda res, model, message: (
        settled.append(message), real_settle(res, model, message),
    ))
    monkeypatch.setattr(cost_caps, "charge", lambda model, counts: 0.01)

    class _Inner:
        def messages_stream(self, **_kw: Any) -> _Snap:
            return _Snap(stop_reason)

    provider = CappedProvider(_Inner())  # type: ignore[arg-type]
    caps.turn = 10.0

    async def go() -> float:
        with cost_caps.bind_turn(fresh=True) as turn:
            async with provider.messages_stream(model="claude-test", max_tokens=10, messages=[]):
                pass
            return turn.spent

    spent = asyncio.run(go())
    assert (settled[0] is not None) is settled_on_snapshot
    assert spent == (pytest.approx(0.01) if settled_on_snapshot else 1.0)


def test_a_committee_draft_a_cap_stopped_still_passes_the_answer_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import executive as executive_mod

    async def corrected(text: str, turn: Any, **_k: Any) -> str:
        return text.replace("12,900", "12,500")

    monkeypatch.setattr(executive_mod.answer_check, "check_reply", corrected)

    async def go() -> str:
        with cost_caps.bind_turn(fresh=True):
            cost_caps.mark_stopped(STOP)
            return await Executive._checked_stopped_draft(
                f"North owes $12,900.\n\n{STOP}", object(), question="q", model="m",
            )

    assert asyncio.run(go()) == f"North owes $12,500.\n\n{STOP}"


def test_a_committee_draft_whose_check_a_cap_refused_is_held_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import executive as executive_mod

    async def capped(text: str, turn: Any, **_k: Any) -> str:
        raise cost_caps.CostCapReached("total", "all", 1.0, 1.0, datetime.now(UTC))

    monkeypatch.setattr(executive_mod.answer_check, "check_reply", capped)

    async def go() -> str:
        with cost_caps.bind_turn(fresh=True):
            cost_caps.mark_stopped(STOP)
            return await Executive._checked_stopped_draft(
                f"North owes $12,900.\n\n{STOP}", object(), question="q", model="m",
            )

    assert asyncio.run(go()) == STOP


@pytest.mark.asyncio
async def test_a_response_gate_a_cap_refused_stays_silent() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from openexecutive.integrations.response_gate import should_respond

    provider = MagicMock()
    provider.messages_create = AsyncMock(
        side_effect=cost_caps.CostCapReached("person", "outside:slack", 1.0, 1.0, datetime.now(UTC)),
    )
    settings = MagicMock(utility_fast_timeout_s=10.0)
    with (
        patch("openexecutive.providers.get_provider", return_value=provider),
        patch("openexecutive.config.get_settings", return_value=settings),
    ):
        d = await should_respond(
            user_text="anything", author_display_name="Alice", history=[{"role": "user", "content": "x"}],
            bot_display_name="Exec", channel="slack",
        )
    assert d.allow is False and d.reason == "cost_cap"


def test_a_scheduled_actions_job_is_its_creators_own() -> None:
    from openexecutive.scheduler.runner import _job_id

    row = SimpleNamespace(kind="followup", department=None, created_by_person_id=7)
    assert _job_id(row) == "scheduled:followup:7"  # type: ignore[arg-type]
    row = SimpleNamespace(kind="dept_cadence", department="sales", created_by_person_id=None)
    assert _job_id(row) == "scheduled:dept_cadence:sales"  # type: ignore[arg-type]


def test_a_refusals_text_names_only_its_scope() -> None:
    exc = cost_caps.CostCapReached("person", "7", 1.0, 1.23, datetime.now(UTC), name="Sara Teammate")
    assert str(exc) == "cost cap reached: person"
    assert exc.key == "7" and exc.spent == 1.23


def test_a_sweep_is_one_turn_for_the_work_it_starts() -> None:
    from openexecutive.scheduler.runner import _sweep

    async def go() -> tuple[Any, Any]:
        seen: list[Any] = []

        async def work() -> None:
            seen.append(cost_caps.current_turn())

        with _sweep("scan-inbox"):
            assert cost_caps.current_job() == "scan-inbox"
            task = asyncio.create_task(work())
        await task
        return seen[0], cost_caps.current_turn()

    inside, after = asyncio.run(go())
    assert inside is not None and not inside.closed and after is None


def test_a_voice_note_at_a_cap_tells_an_outsider_only_the_general_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.providers import transcription
    from openexecutive.providers.refusal import reply_text

    def capped(model: str, kwargs: dict[str, Any]) -> None:
        raise cost_caps.CostCapReached("total", "all", 1.0, 1.0, datetime.now(UTC))

    monkeypatch.setattr(cost_caps, "check", capped)
    where = SimpleNamespace(model="whisper", api_key="", via_openrouter=False, base_url="http://x")
    monkeypatch.setattr(transcription, "target", lambda: where)
    stranger = Session(origin_channel="telegram")
    stranger.sender_off_team = True  # type: ignore[attr-defined]
    with set_session(stranger), pytest.raises(transcription.TranscriptionError) as failed:
        asyncio.run(transcription.transcribe(b"OggS", "note.ogg"))
    assert str(failed.value) == reply_text()


# --------------------------------------------------------------------------- #
# Security re-review (Nov 17)
# --------------------------------------------------------------------------- #

PERSON_SENTENCE = "You've reached today's usage limit. It resets at midnight."


def _committee_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, check: Any, loop: Any = None,
) -> tuple[list[Any], Session, list[tuple[str, dict[str, Any]]]]:
    """One committee turn whose draft is written (``loop``, else a plain
    draft), whose revision a person cap refuses, and whose answer check is
    ``check``."""
    from openexecutive.memory import episodic
    from openexecutive.orchestrator import committee as committee_mod
    from openexecutive.orchestrator import executive as executive_mod

    path = tmp_path / "e.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)

    async def draft_loop(self: Any, *_a: Any, **_k: Any) -> Any:
        yield "North owes $12,900."

    async def no_critiques(self: Any, **_k: Any) -> list[Any]:
        return []

    class _Refused:
        async def __aenter__(self) -> Any:
            raise cost_caps.CostCapReached("person", "4", 1.0, 1.0, datetime.now(UTC))

        async def __aexit__(self, *_a: Any) -> None:
            return None

    rows: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(executive_mod.Executive, "_stream_agent_loop", loop or draft_loop)
    monkeypatch.setattr(committee_mod.Committee, "review", no_critiques)
    monkeypatch.setattr(executive_mod, "get_provider", lambda model: SimpleNamespace(
        messages_stream=lambda **_k: _Refused(),
    ))
    monkeypatch.setattr(executive_mod.answer_check, "check_reply", check)
    monkeypatch.setattr(executive_mod, "audit_log", lambda event, summary, **kw: rows.append((event, kw)))
    session = Session()
    out: list[Any] = []

    async def go() -> None:
        with set_session(session), cost_caps.bind_turn(fresh=True):
            async for item in Executive().stream_chat_with_committee(
                user_message="Who owes us?", session=session, peer_memory_context="", standing_facts="",
            ):
                out.append(item)

    asyncio.run(go())
    return out, session, rows


def test_a_capped_revision_whose_check_is_capped_shows_nothing_of_the_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def capped(*_a: Any, **_k: Any) -> str:
        raise cost_caps.CostCapReached("person", "4", 1.0, 1.0, datetime.now(UTC))

    out, session, rows = _committee_turn(monkeypatch, tmp_path, check=capped)
    shown = [i for i in out if isinstance(i, str) and i != Executive._THINKING]
    assert shown == [PERSON_SENTENCE]
    assert not any("12,900" in json.dumps(i, default=str) for i in out)
    assert session.get_recent_history()[-1]["content"] == PERSON_SENTENCE
    assert not any("12,900" in json.dumps(kw.get("full"), default=str) for event, kw in rows if event == "chat_turn")


def test_a_capped_revision_shows_the_checked_draft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def corrected(text: str, *_a: Any, **_k: Any) -> str:
        return text.replace("12,900", "12,500")

    out, _, _ = _committee_turn(monkeypatch, tmp_path, check=corrected)
    shown = [i for i in out if isinstance(i, str) and i != Executive._THINKING]
    assert shown == [f"North owes $12,500.\n\n{PERSON_SENTENCE}"]


def test_a_committee_draft_a_cap_stopped_writes_its_audit_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def stopped_loop(self: Any, *_a: Any, **_k: Any) -> Any:
        yield "North owes $12,500."
        cost_caps.mark_stopped(STOP)
        yield f"\n\n{STOP}"

    async def unchanged(text: str, *_a: Any, **_k: Any) -> str:
        return text

    out, _, rows = _committee_turn(monkeypatch, tmp_path, check=unchanged, loop=stopped_loop)
    shown = [i for i in out if isinstance(i, str) and i != Executive._THINKING]
    assert shown == [f"North owes $12,500.\n\n{STOP}"]
    kinds = {event: kw for event, kw in rows if event in ("chat_turn", "committee_review")}
    assert kinds["chat_turn"]["details"]["stopped_by_cap"] is True
    assert kinds["chat_turn"]["details"]["direction"] == "out"
    assert kinds["committee_review"]["details"]["skipped"] == "cost_cap"
    assert "usd" not in json.dumps(kinds, default=str).lower()


def test_the_inbox_check_counts_for_its_person_and_job_and_each_scan_is_a_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = (PACKAGE / "api/routes/delegation.py").read_text(encoding="utf-8")
    assert 'cost_caps.bind_job("scan-inbox"), cost_caps.bind_spender(person_id)' in route
    from openexecutive.delegation import inbox

    seen: list[Any] = []

    async def scan(person: Any, now: Any = None) -> None:
        seen.append((cost_caps.current_job(), cost_caps._spender.get(), cost_caps.current_turn()))

    monkeypatch.setattr(inbox, "scan_person", scan)
    monkeypatch.setattr("openexecutive.people.store.get_person", lambda pid: SimpleNamespace(id=pid, active=True))

    async def due() -> None:
        with cost_caps.bind_job("scan-inbox"), cost_caps.bind_turn(fresh=True, close=False):
            await inbox._scan_due([3, 4], datetime.now(UTC))

    asyncio.run(due())
    # Each due person is spent for, and is a turn of their own.
    assert [(job, spender) for job, spender, _ in seen] == [("scan-inbox", "3"), ("scan-inbox", "4")]
    assert seen[0][2] is not seen[1][2] and seen[0][2].closed


def test_an_openrouter_stream_with_no_usage_chunk_keeps_its_worst_case(caps: SimpleNamespace) -> None:
    from openexecutive.providers.translator import StreamAccumulator

    class _Stream:
        def __init__(self, usage: dict[str, Any]) -> None:
            self._accumulator = StreamAccumulator()
            self._accumulator._usage = dict(usage)
            self._finalized = self._accumulator.finalize()

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    def spent(usage: dict[str, Any]) -> float:
        class _Inner:
            def messages_stream(self, **_kw: Any) -> Any:
                return _Stream(usage)

        provider = CappedProvider(_Inner())  # type: ignore[arg-type]

        async def go() -> float:
            with cost_caps.bind_turn(fresh=True) as turn:
                async with provider.messages_stream(model="claude-test", max_tokens=10, messages=[]):
                    pass
                return turn.spent

        return asyncio.run(go())

    caps.turn = 10.0
    with patch.object(cost_caps, "charge", lambda model, counts: 0.02):
        assert spent({}) == 1.0  # no usage chunk: the worst case stays
        assert spent({"prompt_tokens": 10, "completion_tokens": 5}) == pytest.approx(0.02)

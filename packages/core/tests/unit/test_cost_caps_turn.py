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
    assert reply == f"North owes $12,900.\n\n{cap.reply()}" and event == f"\n\n{cap.reply()}"


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

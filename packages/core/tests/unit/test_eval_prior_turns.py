"""An eval scenario's ``prior_turns``: the conversation a follow-up question
follows, seeded into the session (or, for a ``live: true`` turn, run first)
and shown to the judge (openexecutive.evals.scenarios, evals.runner)."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from openexecutive.evals import runner as runner_module
from openexecutive.evals import scenarios
from openexecutive.evals.scenarios import (
    load_scenarios,
    parse_prior_turns,
    prior_turns_section,
    scenario_prior_turns,
    seed_prior_turns,
    validate_scenario_yaml,
    with_prior_replies,
)
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.session import Session

FOLLOW_UP: dict[str, Any] = {
    "id": "x",
    "query": "Which of these receipts should that invoice have matched?",
    "prior_turns": [
        {"user": "Any invoices that don't match?", "assistant": "One: ACC-PINV-1 has no receipt."},
        {"page": {"doctype": "Purchase Invoice", "name": "ACC-PINV-1"}, "user": "What's wrong?", "assistant": "No PO."},
    ],
}


def test_prior_turns_seed_the_history_in_order_with_their_pages() -> None:
    session = Session()
    seed_prior_turns(session, FOLLOW_UP)
    history = session.conversation_history
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "Any invoices that don't match?"
    assert history[1]["content"] == "One: ACC-PINV-1 has no receipt."
    # A turn's page goes before its message, as the panel sends it.
    assert history[2]["content"].endswith("What's wrong?") and "ACC-PINV-1" in history[2]["content"]
    assert history[2]["content"] != "What's wrong?"


def test_the_judge_sees_the_turns_and_every_other_prompt_is_unchanged() -> None:
    section = prior_turns_section(FOLLOW_UP)
    assert "EARLIER IN THIS CONVERSATION" in section
    assert "ASSISTANT: One: ACC-PINV-1 has no receipt." in section
    assert prior_turns_section({"id": "y", "query": "q"}) == ""
    assert scenario_prior_turns({"id": "y", "query": "q"}) == []


@pytest.mark.parametrize(
    "bad",
    [
        "prior_turns: []",
        "prior_turns: {user: a, assistant: b}",
        "prior_turns:\n  - {user: a}",
        "prior_turns:\n  - {user: a, assistant: ''}",
        "prior_turns:\n  - {user: a, assistant: b, speaker: c}",
        # A live turn: `live` is a bool, it has no assistant; a fixed one needs one.
        "prior_turns:\n  - {user: a, live: 'yes'}",
        "prior_turns:\n  - {user: a, live: true, assistant: b}",
        "prior_turns:\n  - {user: a, live: false}",
        "prior_turns:\n  - {user: '', live: true}",
    ],
)
def test_a_malformed_prior_turns_fails_validation(bad: str) -> None:
    with pytest.raises(ValueError, match="prior_turns"):
        validate_scenario_yaml(f"id: x\nquery: q\n{bad}\n")


def test_the_follow_up_scenarios_load_and_validate() -> None:
    import yaml

    shipped = {s["id"]: s for s in load_scenarios()}
    for sid in ("erpnext_063", "erpnext_064", "erpnext_065"):
        s = shipped[sid]
        assert len(parse_prior_turns(s)) >= 1, sid
        validate_scenario_yaml(yaml.safe_dump(s))
    assert scenarios.scenario_surface(shipped["erpnext_063"]) == "erpnext_panel"


# ── live: true ─────────────────────────────────────────────────────────────

LIVE: dict[str, Any] = {
    "id": "l",
    "domain": "mcp",
    "query": "Which week is the tightest?",
    "surface": "erpnext_panel",
    "page": {"report": "Accounts Receivable"},
    "product_name": "Dynocortex",
    "advice_policy": "explain",
    "prior_turns": [
        {"page": {"report": "Accounts Receivable"}, "user": "What does cash look like for 13 weeks?", "live": True},
    ],
}
FIRST = "Week 7 is the lowest: cash ends at 41,200."


def test_a_live_turn_validates_and_has_no_reply_until_it_runs() -> None:
    import yaml

    validate_scenario_yaml(yaml.safe_dump(LIVE))
    (turn,) = parse_prior_turns(LIVE)
    assert turn.reply is None and turn.typed == "What does cash look like for 13 weeks?"
    assert turn.message.endswith(turn.typed) and turn.message != turn.typed  # its page block first
    with pytest.raises(ValueError, match="live turn"):
        scenario_prior_turns(LIVE)
    with pytest.raises(ValueError, match="live turn"):
        seed_prior_turns(Session(), LIVE)
    ran = with_prior_replies(LIVE, [FIRST])
    assert "live" not in ran["prior_turns"][0] and ran["prior_turns"][0]["page"] == LIVE["prior_turns"][0]["page"]
    assert f"ASSISTANT: {FIRST}" in prior_turns_section(ran)
    assert LIVE["prior_turns"][0].get("assistant") is None  # the scenario itself is unchanged


class _Turns:
    """Executive.chat as the tests need it: each call answers the next reply
    and keeps the turn in the session's history, as stream_chat does."""

    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> str:
        from openexecutive.integrations import erpnext_panel
        from openexecutive.prompts.advice_policy import advice_policy_override
        from openexecutive.utils.product_name import configured_product_name

        session = kwargs["session"]
        self.calls.append({
            **kwargs,
            "history": [dict(m) for m in session.conversation_history],
            "product": configured_product_name(),
            "policy": advice_policy_override.get(),
        })
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        # Each turn opens a view beside the panel; only the question's counts.
        views = erpnext_panel._turn_views.get()
        if views is not None:
            views.views.append({"kind": f"view{len(self.calls)}"})
        session.add_user_message(kwargs["user_message"])
        session.add_assistant_message(reply)
        return reply


def test_a_live_turn_runs_first_and_its_reply_is_the_history_the_question_follows() -> None:
    from openexecutive.integrations.erpnext_panel import page_block

    executive = _Turns(FIRST, "Week 7, ending at 41,200.")
    turn = asyncio.run(runner_module.run_chat_turn(executive, LIVE))
    first, question = executive.calls
    # The live turn: the same session, surface, page and overrides, and its
    # own text as the answer check's typed question.
    assert first["user_message"] == f"{page_block(LIVE['page'])}\n\nWhat does cash look like for 13 weeks?"
    assert first["typed_question"] == "What does cash look like for 13 weeks?"
    assert first["session"] is question["session"]
    assert first["channel_context_block"] == question["channel_context_block"] != ""
    assert first["product"] == question["product"] == "Dynocortex"
    assert first["policy"] == question["policy"] == "explain"
    assert first["history"] == []
    # The question follows the real reply.
    assert [m["role"] for m in question["history"]] == ["user", "assistant"]
    assert question["history"][1]["content"] == FIRST
    assert question["typed_question"] == LIVE["query"]
    assert turn.response == "Week 7, ending at 41,200." and turn.prior_replies == [FIRST]
    # Only the question's views and specialists are the turn's.
    assert [v["kind"] for v in turn.views] == ["view2"]
    assert first["debug_collector"] is None and question["debug_collector"] is not None
    # The judge sees the conversation as it ran.
    assert f"ASSISTANT: {FIRST}" in prior_turns_section(turn.judged_scenario(LIVE))


def test_fixed_and_live_turns_keep_their_order() -> None:
    scenario = {
        "id": "m",
        "query": "q",
        "prior_turns": [{"user": "a", "assistant": "fixed"}, {"user": "b", "live": True}],
    }
    executive = _Turns("live reply", "answer")
    turn = asyncio.run(runner_module.run_chat_turn(executive, scenario))
    assert [m["content"] for m in executive.calls[1]["history"]] == ["a", "fixed", "b", "live reply"]
    assert turn.prior_replies == ["fixed", "live reply"]


@pytest.mark.parametrize(
    ("first", "why"),
    [(RuntimeError("tool down"), "RuntimeError: tool down"), ("   ", "it answered nothing")],
)
def test_a_live_turn_that_errors_or_answers_nothing_fails_the_scenario(first: Any, why: str) -> None:
    executive = _Turns(first, "never asked")
    with pytest.raises(RuntimeError, match="live prior turn 1 failed") as raised:
        asyncio.run(runner_module.run_chat_turn(executive, LIVE))
    assert why in str(raised.value)
    assert len(executive.calls) == 1  # the question never runs


def test_the_package_runner_judges_and_checks_the_final_turn_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    executive = _Turns("Cash dips in week 7. Cash dips in week 7. ERPNext says so.", "Week 7, at 41,200.")

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        return await executive.chat(**kwargs)

    async def fake_judge(scenario: dict[str, Any], response: str) -> dict[str, Any]:
        seen["section"] = prior_turns_section(scenario)
        return {"overall": 5}

    monkeypatch.setattr(Executive, "__init__", lambda self, mcp_gateway=None: None)
    monkeypatch.setattr(Executive, "chat", fake_chat)
    monkeypatch.setattr(runner_module, "judge_chat", fake_judge)
    scenario = {
        **LIVE,
        "forbidden_reply_words": ["ERPNext"],
        "forbid_repeated_text": True,
        "forbidden_view_kinds": ["view1"],
    }
    queue: asyncio.Queue[Any] = asyncio.Queue()
    asyncio.run(runner_module._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None)(0, scenario))
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    (done,) = [e for e in events if e["type"] == "scenario_done"]
    # The live turn's forbidden word and repeated sentence aren't held
    # against the question's reply; the view it opened doesn't count either.
    assert "forbidden_reply_words" not in done["scores"] and "repeated_text" not in done["scores"]
    assert done["scores"]["views"] == {"kinds": ["view2"], "missing": [], "forbidden": [], "ok": True}
    assert done["passed"] is True
    assert done["response"] == "Week 7, at 41,200."
    assert "ASSISTANT: Cash dips in week 7." in seen["section"]


def test_the_package_runner_reports_a_failed_live_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    executive = _Turns("")

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        return await executive.chat(**kwargs)

    monkeypatch.setattr(Executive, "__init__", lambda self, mcp_gateway=None: None)
    monkeypatch.setattr(Executive, "chat", fake_chat)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    asyncio.run(runner_module._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None)(0, LIVE))
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    (error,) = [e for e in events if e["type"] == "scenario_error"]
    assert "live prior turn 1 failed" in error["error"]


def _run_evals() -> ModuleType:
    script = Path(__file__).resolve().parents[4] / "evals" / "run_evals.py"
    spec = importlib.util.spec_from_file_location("run_evals_prior_turns", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_evals_runs_the_live_turn_and_its_judge_sees_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    run_evals = _run_evals()
    seen: dict[str, Any] = {}

    async def judge(scenario: dict[str, Any], response: str, drafts: Any = None) -> dict[str, Any]:
        seen["section"] = prior_turns_section(scenario)
        return {"overall": 5}

    monkeypatch.setattr(run_evals, "judge_response", judge)
    executive = _Turns(FIRST, "Week 7, ending at 41,200.")
    result = asyncio.run(run_evals.evaluate_chat(LIVE, executive))
    assert result["passed"] is True and result["response"] == "Week 7, ending at 41,200."
    assert executive.calls[1]["history"][1]["content"] == FIRST
    assert f"ASSISTANT: {FIRST}" in seen["section"]


def test_the_cash_outlook_follow_ups_run_their_first_turn_live() -> None:
    shipped = {s["id"]: s for s in load_scenarios()}
    for sid in ("erpnext_099", "erpnext_100"):
        (turn,) = parse_prior_turns(shipped[sid])
        assert turn.reply is None, sid
        assert turn.typed == shipped["erpnext_094"]["prior_turns"][0]["user"], sid


def test_the_unbilled_work_follow_up_runs_both_turns_live_and_names_no_project_id() -> None:
    # A project id is renumbered at every demo refresh and differs per site:
    # the scripted turns named one the follow-up's read then couldn't find.
    import re

    s = {x["id"]: x for x in load_scenarios()}["erpnext_064"]
    assert [t.reply for t in parse_prior_turns(s)] == [None, None]
    assert not re.search(r"PROJ-\d", str(s["prior_turns"]) + s["query"])


def test_the_unbilled_hours_follow_up_runs_its_first_turn_live_and_names_no_project_id() -> None:
    # erpnext_064's defect too: its scripted step 1 named PROJ-0154.
    import re

    s = {x["id"]: x for x in load_scenarios()}["erpnext_065"]
    assert [t.reply for t in parse_prior_turns(s)] == [None]
    assert not re.search(r"PROJ-\d", str(s["prior_turns"]) + s["query"] + s["description"])

"""Async generator that runs eval scenarios in parallel and yields progress events.

Scenarios run with bounded concurrency (default 5, override via ``EVAL_CONCURRENCY``).
Events arrive in the order tasks finish — not in scenario order. The UI keys
state by ``scenario_id`` so this is fine.

Yielded event shapes:
  {"type": "suite_start",     "kind": str, "total": int}
  {"type": "scenario_start",  "index": int, "total": int, "scenario_id": str, "description": str}
  {"type": "scenario_done",   "index": int, "total": int, "scenario_id": str, "passed": bool, "scores": dict,
                              # plus kind-specific payload:
                              #   chat:     "query", "response"
                              #   workflow: "workflow_name", "workflow_inputs", "artifact"
                              #   triage:   "event", "decision"
                              #   inbox:    "outcome" (verdict, drafted, reply)
                              }
  {"type": "scenario_error",  "index": int, "total": int, "scenario_id": str, "error": str}
  {"type": "suite_done",      "kind": str, "passed": int, "total": int}
  {"type": "suite_canceled",  "kind": str, "passed": int, "total": int}

A chat, mcp or workflow scenario may set ``workspace_mode: solo`` (or
``team``) to run as that workspace mode without touching the install-wide
setting — scenarios run concurrently on one Executive. Chat puts it on the
scenario's ``Session.workspace_mode``; a workflow runs with a session carrying
it bound as the current session, which is where workflows read the mode.

A scenario may likewise set a ``principal_role`` mapping (``role_kind``,
``role_title``, ``reports_to``, ``remit``, ``measured_on`` — the workspace
settings' role fields) to play a principal with that role. It goes on the
session (``Session.principal_role``) the same way, never into the
install-wide settings row, which concurrent scenarios would share.

A chat scenario may set ``voice_persona`` (a voice slug such as
``supportive``) to answer in that voice. It goes on
``Session.voice_persona_slug``, never the Executive's install-wide override.

A chat scenario may set a ``delegation`` block (Act as me: the asker, their
threads) to run with ``ghostwrite_email`` offered against an in-memory
mailbox (``scenarios.scenario_delegation``); the drafts it saves go to the
judge alongside the reply.

A chat or mcp scenario may set ``surface: erpnext_panel`` to run as a turn
from ERPNext's "Ask the Dynocortex" panel, as ``integrations.erpnext_panel``
runs one: the session is marked as the panel's (``erpnext_panel_email``,
``origin_channel``, ``read_erpnext``), so only ERPNext reads and specialists
are offered (and ERPNext drafts, when the install offers them), and the
panel's channel block (``erpnext_panel.context_block``) goes in.

An ``inbox`` scenario (``type: inbox``) runs the inbox watcher's two model
calls on one email (``delegation.inbox.reply_for``): whether it drafts at all
must match ``expect``, and a draft is judged by ``judge_inbox``. Nothing
reaches Gmail and nothing is stored.

A ``supplier`` scenario (``type: supplier``) runs the narrow agent that
answers a supplier's agent over A2A (``integrations.erpnext_a2a_agent``) on
one message, its tools answering from the scenario's documents as
oe_connector would (``supplier_tools``), and ``judge_supplier`` scores the
reply. Nothing reaches ERPNext.

A ``watch_reason`` scenario (``type: watch_reason``) runs a watch job's model
"reason" line on the real fast model through the job's own reason step
(``watch.runner.reason_lines``), on findings the job's own code builds from
the scenario's figures and team update (``scenarios.scenario_watch_reason``).
``judge_watch_reason`` scores the lines that reach the notice. Nothing reaches
ERPNext and no notice is left.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import asdict, dataclass, field
from typing import Any

from openexecutive.evals.judges import (
    judge_chat,
    judge_inbox,
    judge_supplier,
    judge_triage,
    judge_watch_reason,
    judge_workflow,
)
from openexecutive.evals.scenarios import (
    LIVE_PRINCIPAL_NAME_SAID,
    WATCH_REASON,
    SupplierCase,
    load_scenarios,
    parse_prior_turns,
    principal_name_in,
    reply_check_failures,
    routing_check,
    scenario_decided_cards,
    scenario_delegation,
    scenario_inbox,
    scenario_principal_role,
    scenario_query,
    scenario_supplier,
    scenario_turn_surface,
    scenario_voice_persona,
    scenario_watch_reason,
    view_kinds_check,
    with_prior_replies,
)
from openexecutive.workflows.gate import ensure_workflow_event

logger = logging.getLogger(__name__)

_PASS_THRESHOLD = 3.5
_CONCURRENCY = max(1, int(os.environ.get("EVAL_CONCURRENCY", "5")))

# A `RunOne` runs a single scenario at a given index and puts events into the
# provided queue. Each kind builds one of these closures with its own context.
RunOne = Callable[[int, dict[str, Any]], Coroutine[Any, Any, None]]


async def run_scenarios(
    *,
    kind: str,
    scenario_id: str | None = None,
    store: Any = None,
    cancel_event: asyncio.Event | None = None,
    scenarios: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Async generator that streams progress events as scenarios run in parallel.

    If ``cancel_event`` is provided and set during the run, all in-flight
    scenario tasks are cancelled and the generator yields ``suite_canceled``
    instead of ``suite_done``.

    ``scenarios``, when given, is the list to run instead of loading it
    again: ``POST /evals/runs`` passes the list it checked, so an edit to a
    user scenario in between cannot swap in one that was not checked.
    """
    if scenarios is None:
        scenarios = load_scenarios(kind=kind, scenario_id=scenario_id)
    total = len(scenarios)
    passed = [0]  # single-item list — safe under asyncio without locks

    yield {"type": "suite_start", "kind": kind, "total": total}

    if total == 0:
        yield {"type": "suite_done", "kind": kind, "passed": 0, "total": 0}
        return

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    sem = asyncio.Semaphore(_CONCURRENCY)

    run_one = _make_run_one(
        kind=kind,
        store=store,
        sem=sem,
        queue=queue,
        passed=passed,
        total=total,
        cancel_event=cancel_event,
    )

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(run_one(i, s)) for i, s in enumerate(scenarios)
    ]

    async def watcher() -> None:
        # Optional companion task that cancels every in-flight scenario the
        # moment the external cancel event fires. Without this the suite
        # would have to wait for the slowest in-flight scenario (~60s) to
        # finish naturally before the user's Stop click takes effect.
        cancel_watcher_task: asyncio.Task[None] | None = None
        if cancel_event is not None:
            async def cancel_watcher() -> None:
                await cancel_event.wait()
                for t in tasks:
                    if not t.done():
                        t.cancel()
            cancel_watcher_task = asyncio.create_task(cancel_watcher())

        # `return_exceptions=True` so a per-task bug (or CancelledError)
        # can't deadlock the generator — each run_one already wraps its
        # work in try/except.
        await asyncio.gather(*tasks, return_exceptions=True)

        if cancel_watcher_task is not None and not cancel_watcher_task.done():
            cancel_watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cancel_watcher_task

        await queue.put(None)

    watcher_task = asyncio.create_task(watcher())

    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        await watcher_task

    if cancel_event is not None and cancel_event.is_set():
        yield {"type": "suite_canceled", "kind": kind, "passed": passed[0], "total": total}
    else:
        yield {"type": "suite_done", "kind": kind, "passed": passed[0], "total": total}


def _make_run_one(
    *,
    kind: str,
    store: Any,
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
) -> RunOne:
    """Returns a `run_one(i, scenario)` coroutine for the requested kind."""
    if kind == "triage":
        return _make_triage_runner(sem, queue, passed, total, cancel_event)
    if kind == "inbox":
        return _make_outcome_runner(run_inbox_scenario, sem, queue, passed, total, cancel_event)
    if kind == "supplier":
        return _make_outcome_runner(run_supplier_scenario, sem, queue, passed, total, cancel_event)
    if kind == WATCH_REASON:
        return _make_outcome_runner(run_watch_reason_scenario, sem, queue, passed, total, cancel_event)
    if kind == "workflow":
        return _make_workflow_runner(store, sem, queue, passed, total, cancel_event)
    # chat (default) and mcp both go through Executive.chat; mcp with the
    # API's running gateway, so its scenarios can reach their tools.
    return _make_chat_runner(sem, queue, passed, total, cancel_event, with_mcp=kind == "mcp")


def _is_canceled(cancel_event: asyncio.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def scenario_workspace_mode(scenario: dict[str, Any]) -> str | None:
    """The scenario's ``workspace_mode`` ("solo" / "team"), or None to run
    under the install's own setting. Raises ValueError for any other value, so
    a typo fails the scenario instead of silently running in the wrong mode."""
    mode = scenario.get("workspace_mode")
    if mode is None:
        return None
    if mode not in ("solo", "team"):
        raise ValueError(f"workspace_mode must be 'solo' or 'team', got {mode!r}")
    return str(mode)


def scenario_standing_facts(scenario: dict[str, Any]) -> str:
    """The ``<standing_facts>`` body for a scenario: its ``standing_facts``
    lines under the shared block header, or "" when it has none. Never the
    install's own facts store, so a scenario runs the same on every machine."""
    from openexecutive.memory.facts import FACTS_BLOCK_HEADER

    lines = str(scenario.get("standing_facts") or "").strip()
    return f"{FACTS_BLOCK_HEADER}\n{lines}" if lines else ""


def _make_triage_runner(
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
) -> RunOne:
    from openexecutive.agents.triage import TriageAgent
    from openexecutive.alerts.models import AlertEvent

    async def run_one(i: int, scenario: dict[str, Any]) -> None:
        if _is_canceled(cancel_event):
            return
        try:
            async with sem:
                if _is_canceled(cancel_event):
                    return
                await queue.put(
                    {
                        "type": "scenario_start",
                        "index": i,
                        "total": total,
                        "scenario_id": scenario["id"],
                        "description": scenario.get("description", ""),
                    }
                )
                try:
                    event = AlertEvent(**scenario["event"])
                    ctx = scenario.get("context", {}) or {}
                    agent = TriageAgent()
                    decision = await agent.triage(
                        event,
                        recent_alerts=ctx.get("recent_alerts", []) or [],
                        mute_patterns=ctx.get("muted_topics", []) or [],
                        active_initiatives=ctx.get("active_initiatives", []) or [],
                    )
                    decision_dict = decision.model_dump(mode="json")
                    scores = await judge_triage(scenario, decision_dict)
                    ok = float(scores.get("overall", 0)) >= _PASS_THRESHOLD
                    if ok:
                        passed[0] += 1
                    await queue.put(
                        {
                            "type": "scenario_done",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "passed": ok,
                            "scores": scores,
                            "event": scenario.get("event"),
                            "decision": decision_dict,
                        }
                    )
                except Exception as exc:
                    logger.exception("eval scenario %s failed", scenario["id"])
                    await queue.put(
                        {
                            "type": "scenario_error",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "error": str(exc),
                        }
                    )
        except asyncio.CancelledError:
            # User clicked Stop. Exit silently — `suite_canceled` will
            # be emitted by the outer generator once all tasks settle.
            return

    return run_one


async def run_inbox_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """One ``type: inbox`` scenario: ``{"outcome", "scores", "passed"}``."""
    from openexecutive.delegation.inbox import addressed_for, handling_relation, reply_for
    from openexecutive.delegation.inbox_classifier import wants_draft

    case = scenario_inbox(scenario)
    if case is None:
        raise ValueError("inbox scenarios require an `inbox` block")
    verdict, reply = await reply_for(
        case.person, case.message, case.thread, relation=case.relation, own={case.person.email}
    )
    handled_as = handling_relation(case.relation, case.message)
    addressed = addressed_for(case.person, case.message, {case.person.email})
    wanted = verdict is not None and wants_draft(verdict, handled_as, addressed)
    drafted = reply is not None and not isinstance(reply, str)
    outcome: dict[str, Any] = {
        "verdict": asdict(verdict) if verdict is not None else None,
        "handled_as": handled_as,
        "drafted": drafted,
        "reply": asdict(reply) if drafted and reply is not None and not isinstance(reply, str) else None,
        "no_reply_because": reply if isinstance(reply, str) else None,
    }
    scores: dict[str, Any]
    if verdict is None:
        # No verdict is a failed call, never a pass for "no draft".
        scores = {"overall": 0, "notes": "the classifier gave no verdict (a failed or malformed call)"}
    elif wanted != case.expect_draft:
        scores = {
            "overall": 1,
            "notes": f"expected {'a draft' if case.expect_draft else 'no draft'}, "
            f"the classifier {'wanted one' if wanted else 'did not want one'} "
            f"({verdict.kind}, {verdict.confidence:.2f}, as {handled_as})",
        }
    elif not wanted:
        scores = {"overall": 5, "notes": "drafted nothing, as expected"}
    elif not drafted:
        scores = {"overall": 1, "notes": f"a draft was wanted but not written ({reply})"}
    else:
        scores = await judge_inbox(scenario, outcome)
    return {
        "outcome": outcome,
        "scores": scores,
        "passed": float(scores.get("overall", 0)) >= _PASS_THRESHOLD,
    }


def supplier_tools(case: SupplierCase) -> Callable[[str, dict[str, Any]], Coroutine[Any, Any, str]]:
    """oe_connector's three supplier tools over the scenario's documents,
    answering as its mcp/suppliers.py does: only this supplier's, and any
    other order number the same as one that doesn't exist."""
    done = ("Completed", "Closed")

    async def call(name: str, arguments: dict[str, Any]) -> str:
        asked = arguments.get("limit")
        limit = asked if isinstance(asked, int) else 50
        if name == "erpnext__supplier_orders":
            rows = [
                {k: v for k, v in o.items() if k != "items"}
                for o in case.orders
                if arguments.get("include_done") is True or o.get("status") not in done
            ]
            return json.dumps({"supplier": case.supplier, "orders": rows[:limit], "truncated": len(rows) > limit})
        if name == "erpnext__supplier_order":
            wanted = str(arguments.get("name") or "").strip()
            order = next((o for o in case.orders if o["name"] == wanted), None)
            if order is None:
                return "No submitted Purchase Order of yours has that number."
            return json.dumps({"supplier": case.supplier, "order": order, "truncated": False})
        if name == "erpnext__supplier_invoices":
            rows = [
                i for i in case.invoices
                if arguments.get("unpaid_only") is not True or i.get("outstanding_amount") not in (0, None)
            ]
            return json.dumps({"supplier": case.supplier, "invoices": rows[:limit], "truncated": len(rows) > limit})
        return f"Unknown tool: {name}"

    return call


async def run_supplier_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """One ``type: supplier`` scenario: the narrow A2A agent answers the
    supplier's agent over the scenario's documents; ``{"outcome", "scores",
    "passed"}``. Nothing reaches ERPNext."""
    from openexecutive.integrations.erpnext_a2a_agent import answer

    case = scenario_supplier(scenario)
    if case is None:
        raise ValueError("supplier scenarios require a `supplier` block")
    result = await answer(case.supplier, case.message, supplier_tools(case))
    outcome = {"reply": result.reply, "tools": result.tools}
    scores = await judge_supplier(scenario, outcome)
    passed = float(scores.get("overall", 0)) >= _PASS_THRESHOLD
    # The same deterministic checks as a chat scenario's, which the judge's
    # score can't outvote: a word the reply must never say (an IBAN repeated
    # back), a sentence said twice, or something it must name left out.
    failures = reply_check_failures(scenario, result.reply)
    scores.update(failures)
    return {"outcome": outcome, "scores": scores, "passed": passed and not failures}


async def run_watch_reason_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """One ``type: watch_reason`` scenario: the job's own reason step
    (``watch.runner.reason_lines``, what ``reason_step`` runs: the job's
    ``ReasonSchema`` and instruction, the runner's constant system prompt,
    the configured fast model, then its cleaning and ``no_digits`` filter)
    on the scenario's findings, built by the job's own code;
    ``{"outcome", "scores", "passed"}``. Nothing is stored and no notice is
    left."""
    from openexecutive.watch.runner import MAX_REASON_CHARS, clean, drop_rule, reason_lines

    case = scenario_watch_reason(scenario)
    if case is None:
        raise ValueError("watch_reason scenarios require a `watch_reason` block")
    kept, dropped = await reason_lines(case.job, [f.spec for f in case.findings])
    outcome = {
        "job": case.job.id,
        "findings": [
            {
                "finding": f.label,
                "facts": dict(f.spec.facts),
                "line": kept.get(f.spec.key),
                # Never shown on a notice; kept cleaned and capped (twice the
                # line limit, so a too-long one still shows it was long).
                "dropped": clean(dropped[f.spec.key], 2 * MAX_REASON_CHARS) if f.spec.key in dropped else None,
                # Which filter rule dropped it (watch.runner.DROP_RULES).
                "dropped_rule": drop_rule(dropped[f.spec.key]) if f.spec.key in dropped else None,
            }
            for f in case.findings
        ],
    }
    scores = await judge_watch_reason(scenario, outcome)
    passed = float(scores.get("overall", 0)) >= _PASS_THRESHOLD
    # Deterministic checks the judge's score can't outvote: a line the
    # scenario says must (or must not) reach the notice, and a word no line
    # on it may say. Only kept lines count: a dropped one is never shown.
    wrong = [
        f.label for f in case.findings
        if f.expect_line is not None and (f.spec.key in kept) != f.expect_line
    ]
    if wrong:
        scores["expect_line_unmet"] = wrong
        passed = False
    # A word no kept line may say, and what the kept lines must name between
    # them. Lines aren't one reply, so a sentence repeated across two isn't
    # checked.
    failures = reply_check_failures(scenario, "\n".join(kept.values()), repeated=False)
    scores.update(failures)
    return {"outcome": outcome, "scores": scores, "passed": passed and not failures}


def _make_outcome_runner(
    run_scenario: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]],
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
) -> RunOne:
    async def run_one(i: int, scenario: dict[str, Any]) -> None:
        if _is_canceled(cancel_event):
            return
        try:
            async with sem:
                if _is_canceled(cancel_event):
                    return
                await queue.put({
                    "type": "scenario_start",
                    "index": i,
                    "total": total,
                    "scenario_id": scenario["id"],
                    "description": scenario.get("description", ""),
                })
                try:
                    result = await run_scenario(scenario)
                    if result["passed"]:
                        passed[0] += 1
                    await queue.put({
                        "type": "scenario_done",
                        "index": i,
                        "total": total,
                        "scenario_id": scenario["id"],
                        "passed": result["passed"],
                        "scores": result["scores"],
                        "outcome": result["outcome"],
                    })
                except Exception as exc:
                    logger.exception("eval scenario %s failed", scenario["id"])
                    await queue.put({
                        "type": "scenario_error",
                        "index": i,
                        "total": total,
                        "scenario_id": scenario["id"],
                        "error": str(exc),
                    })
        except asyncio.CancelledError:
            return

    return run_one


def _make_workflow_runner(
    store: Any,
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
) -> RunOne:
    from openexecutive.orchestrator.schedule_tools import set_session
    from openexecutive.orchestrator.session import Session
    from openexecutive.workflows import WORKFLOW_REGISTRY

    async def run_one(i: int, scenario: dict[str, Any]) -> None:
        if _is_canceled(cancel_event):
            return
        try:
            async with sem:
                if _is_canceled(cancel_event):
                    return
                await queue.put(
                    {
                        "type": "scenario_start",
                        "index": i,
                        "total": total,
                        "scenario_id": scenario["id"],
                        "description": scenario.get("description", ""),
                    }
                )
                try:
                    workflow_name = scenario.get("workflow")
                    workflow = WORKFLOW_REGISTRY.get(workflow_name) if workflow_name else None
                    if workflow is None:
                        raise KeyError(f"unknown workflow: {workflow_name!r}")
                    workflow_inputs = scenario.get("workflow_inputs") or {}
                    inputs = workflow.input_model()(**workflow_inputs)
                    artifact = ""
                    # Workflows read the mode from the current session; this
                    # task's own binding, so concurrent scenarios don't mix.
                    # Bound only when the scenario sets a mode: a bound session
                    # also arms schedule_followup's seen-refs guard, which a
                    # workflow that binds no session of its own would then hit.
                    mode = scenario_workspace_mode(scenario)
                    role = scenario_principal_role(scenario)
                    binding = (
                        set_session(Session(workspace_mode=mode, principal_role=role))
                        if mode is not None or role is not None
                        else contextlib.nullcontext()
                    )
                    with binding:
                        async for ev in workflow.run(inputs, store):
                            ev = ensure_workflow_event(ev, site='evals.runner')
                            if ev.type == "artifact":
                                artifact = ev.content or ""
                            elif ev.type == "error":
                                raise RuntimeError(f"workflow errored: {ev.message}")
                    scores = await judge_workflow(scenario, artifact)
                    ok = float(scores.get("overall", 0)) >= _PASS_THRESHOLD
                    if ok:
                        passed[0] += 1
                    await queue.put(
                        {
                            "type": "scenario_done",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "passed": ok,
                            "scores": scores,
                            "workflow_name": workflow_name,
                            "workflow_inputs": workflow_inputs,
                            "artifact": artifact,
                        }
                    )
                except Exception as exc:
                    logger.exception("eval scenario %s failed", scenario["id"])
                    await queue.put(
                        {
                            "type": "scenario_error",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "error": str(exc),
                        }
                    )
        except asyncio.CancelledError:
            return

    return run_one


# Why an mcp scenario fails when the API has no running gateway (no
# mcp_servers.json, or it failed to start): judged without tools, it would
# measure nothing.
NO_MCP_GATEWAY = "no MCP gateway is running, so this mcp scenario can't reach its tools"


@dataclass
class ChatTurn:
    """One chat or mcp scenario's turn, as ``run_chat_turn`` ran it."""

    query: str
    response: str
    # The specialists it consulted (for ``required_routing`` / ``no_routing``).
    specialists: list[str] = field(default_factory=list)
    # The views a panel turn's show calls opened (``expected_view_kinds``...).
    views: list[dict[str, Any]] = field(default_factory=list)
    # Act as me: the drafts it saved for the asker; None without ``delegation``.
    drafts: list[dict[str, Any]] | None = None
    # The reply each of the scenario's prior turns had, oldest first: a fixed
    # turn's as written, a live turn's as it ran (``with_prior_replies``).
    prior_replies: list[str] = field(default_factory=list)
    # The live prior turns as they ran, oldest first: each one's place in
    # ``prior_turns`` (``turn``, from 1), its ``response`` and that reply's
    # length in characters (``response_length``), as a run's result records
    # the question's. Fixed turns aren't in it.
    live_turns: list[dict[str, Any]] = field(default_factory=list)

    def judged_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]:
        """``scenario`` with its prior turns' replies as they ran, for the
        judge's view of the conversation (``prior_turns_section``)."""
        return with_prior_replies(scenario, self.prior_replies)


# Why a scenario fails when a live prior turn errors or answers nothing:
# the question would follow a conversation that never happened.
LIVE_TURN_FAILED = "live prior turn {n} failed, so the question has no conversation to follow: {why}"


async def run_chat_turn(executive: Any, scenario: dict[str, Any]) -> ChatTurn:
    """Run one chat or mcp scenario's turn on ``executive`` as its route
    would: the scenario's company, product name, advice policy, workspace
    mode, role, voice, Act as me mailbox, surface (the panel's marks and
    channel block), prior turns and decided cards. Both runners use it, this
    module's and the repo's ``evals/run_evals.py``, so a scenario runs (and
    is held to ``chat_checks``) the same way in each.

    A fixed prior turn is seeded into the session's history as written. A
    ``live: true`` one is run first, on the same session and with every
    override above, as the person would have asked it; the real reply is the
    history the question follows. Only the question's turn is returned and
    checked (but for the principal's name, which ``chat_checks`` holds every
    live turn's reply to as well): its specialists and views are its own.
    The live turns' replies are kept in ``ChatTurn.live_turns``. A live
    turn that errors
    or answers nothing fails the scenario (``LIVE_TURN_FAILED``)."""
    from openexecutive.integrations.erpnext_drafts import decided_cards_override
    from openexecutive.integrations.erpnext_panel import TurnViews, collect_views
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.orchestrator.debug_events import DebugCollector
    from openexecutive.orchestrator.session import Session
    from openexecutive.prompts.advice_policy import advice_policy_override
    from openexecutive.utils.product_name import product_name_override

    ctx = scenario.get("company_context", {})
    profile = CompanyProfile(
        name=ctx.get("name", "Eval Company"),
        industry=ctx.get("industry", ""),
        stage=ctx.get("stage", ""),
        headcount=ctx.get("headcount"),
        annual_revenue_arr=ctx.get("arr"),
    )
    if ctx.get("monthly_burn"):
        profile.financials.burn_rate_monthly = ctx["monthly_burn"]
    if ctx.get("runway_months"):
        profile.financials.runway_months = ctx["runway_months"]
    # The scenario's own product name (OE_PRODUCT_NAME) and advice policy
    # (ADVICE_POLICY), for this turn only (scenarios run side by side), set
    # before anything below builds a prompt part from them.
    name_token = product_name_override.set(scenario.get("product_name"))
    policy_token = advice_policy_override.set(scenario.get("advice_policy"))
    # The draft cards its conversation left, as the turn's note is told
    # them: never written to the ledger, and forgotten once the turn is done.
    cards_token = decided_cards_override.set(scenario_decided_cards(scenario))
    try:
        # Act as me: a fresh in-memory mailbox for the asker, whose drafts
        # the judge reads (nothing reaches Google).
        delegation = scenario_delegation(scenario)
        # ERPNext's panel: the marks and channel block its route gives a
        # turn (integrations.erpnext_panel).
        surface, channel_block = scenario_turn_surface(scenario)
        session = Session(
            company_profile=profile,
            workspace_mode=scenario_workspace_mode(scenario),
            principal_role=scenario_principal_role(scenario),
            voice_persona_slug=scenario_voice_persona(scenario),
            delegation_override=delegation,
            **surface,
        )

        async def one_turn(message: str, typed: str, collector: DebugCollector | None) -> tuple[str, TurnViews]:
            # A panel turn keeps the views its show calls opened, as the
            # panel's route collects them (view_kinds_check).
            shown = TurnViews()
            views_ctx = collect_views(shown) if surface.get("erpnext_panel_email") else contextlib.nullcontext()
            with views_ctx:
                response = await executive.chat(
                    user_message=message,
                    # The question as the person typed it, without the page
                    # block scenario_query puts in front: what the panel
                    # passes as the answer check's typed_question.
                    typed_question=typed,
                    session=session,
                    channel_context_block=channel_block,
                    # chat() sets the turn's speaker from this, as the chat
                    # route passes the signed-in person: a `speaker`
                    # scenario's caller, else None.
                    person_id=surface.get("caller_person_id"),
                    debug_collector=collector,
                    # A scenario may supply the <peer_memory> body itself.
                    # Evals run with no person_id, so the Honcho prefetch
                    # never fires; this is the only way to exercise how the
                    # Executive USES peer memory. None keeps chat()'s default.
                    peer_memory_context=scenario.get("peer_memory_context"),
                    # Likewise the <standing_facts> body (memory/facts.py):
                    # the scenario's own lines under the shared header, or
                    # none: an eval never reads the install's facts.
                    standing_facts=scenario_standing_facts(scenario),
                    # The model's own words: the channels' backstop
                    # (connected_systems.in_product_reply) would hide one
                    # that names ERPNext on a named install (erpnext_027).
                    # What code adds to a reply is in the product's words
                    # where it's added.
                    product_words=False,
                )
            return response, shown

        # A follow-up: the turns it follows, in the session's history. A
        # live one goes through chat() like the question, which keeps the
        # turn in the history as the route does.
        prior_replies: list[str] = []
        live_turns: list[dict[str, Any]] = []
        for n, prior in enumerate(parse_prior_turns(scenario), start=1):
            if prior.reply is not None:
                session.add_user_message(prior.message)
                session.add_assistant_message(prior.reply)
                prior_replies.append(prior.reply)
                continue
            try:
                reply, _ = await one_turn(prior.message, prior.typed, None)
            except Exception as exc:
                raise RuntimeError(LIVE_TURN_FAILED.format(n=n, why=f"{type(exc).__name__}: {exc}")) from exc
            if not (reply or "").strip():
                raise RuntimeError(LIVE_TURN_FAILED.format(n=n, why="it answered nothing"))
            prior_replies.append(reply)
            live_turns.append({"turn": n, "response": reply, "response_length": len(reply)})
        query = scenario_query(scenario)
        # Records which specialists the turn consulted (routing_check).
        collector = DebugCollector()
        response, shown = await one_turn(query, scenario["query"], collector)
    finally:
        decided_cards_override.reset(cards_token)
        advice_policy_override.reset(policy_token)
        product_name_override.reset(name_token)
    return ChatTurn(
        query=query,
        response=response,
        specialists=[
            str(e.data.get("specialist", "")) for e in collector.events if e.kind == "specialist_start"
        ],
        views=list(shown.views),
        drafts=[asdict(d) for d in delegation.gmail.drafts] if delegation is not None else None,
        prior_replies=prior_replies,
        live_turns=live_turns,
    )


def chat_checks(scenario: dict[str, Any], turn: ChatTurn, scores: dict[str, Any]) -> bool:
    """Hold a chat or mcp turn to the scenario's deterministic checks, which
    the judge's score can't outvote: ``reply_check_failures`` (a forbidden
    word, a sentence said twice, a ``reply_must_name`` item left out), the
    specialists it had to consult (``routing_check``) and the views a panel
    turn had to open (``view_kinds_check``). Each result goes into
    ``scores`` under its own key; True when every check holds.

    A live prior turn's reply is held to one check too, the principal's
    name (``forbid_principal_name``): a hit fails the scenario as one in the
    final reply does, under the same ``principal_name`` key, naming the
    turn (``LIVE_PRINCIPAL_NAME_SAID``) and never the name. Its other
    checks stay the question's alone."""
    failures = reply_check_failures(scenario, turn.response)
    named = [
        LIVE_PRINCIPAL_NAME_SAID.format(n=live["turn"])
        for live in turn.live_turns
        if principal_name_in(scenario, live["response"])
    ]
    if named:
        failures["principal_name"] = failures.get("principal_name", []) + named
    scores.update(failures)
    ok = not failures
    routing = routing_check(scenario, turn.specialists)
    if routing is not None:
        scores["routing"] = routing
        ok = ok and routing["ok"]
    opened = view_kinds_check(scenario, turn.views)
    if opened is not None:
        scores["views"] = opened
        ok = ok and opened["ok"]
    return ok


def _make_chat_runner(
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
    *,
    with_mcp: bool = False,
) -> RunOne:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway

    # Shared Executive — confirmed safe: no mutable instance state on the
    # hot path; chat()/stream_chat() route all per-call state through Session.
    # An mcp scenario needs the gateway: without one the Executive offers no
    # MCP tools, and the scenario would be judged on a turn that can't reach
    # them. The gateway is read again for each scenario, so a long run follows
    # a gateway the API replaced, and a scenario with none fails instead.
    current_gateway = get_active_gateway() if with_mcp else None
    executive = Executive(mcp_gateway=current_gateway)

    def executive_for_scenario() -> Executive:
        nonlocal current_gateway, executive
        if not with_mcp:
            return executive
        gateway = get_active_gateway()
        if gateway is None:
            raise RuntimeError(NO_MCP_GATEWAY)
        if gateway is not current_gateway:
            current_gateway = gateway
            executive = Executive(mcp_gateway=gateway)
        return executive

    async def run_one(i: int, scenario: dict[str, Any]) -> None:
        if _is_canceled(cancel_event):
            return
        try:
            async with sem:
                if _is_canceled(cancel_event):
                    return
                await queue.put(
                    {
                        "type": "scenario_start",
                        "index": i,
                        "total": total,
                        "scenario_id": scenario["id"],
                        "description": scenario.get("description", ""),
                    }
                )
                try:
                    turn = await run_chat_turn(executive_for_scenario(), scenario)
                    query, response = turn.query, turn.response
                    # The judge sees the conversation as it ran: a live prior
                    # turn's real reply, not the scenario's empty slot.
                    judged = turn.judged_scenario(scenario)
                    if turn.drafts is not None:
                        scores = await judge_chat(judged, response, drafts=turn.drafts)
                    else:
                        scores = await judge_chat(judged, response)
                    held = chat_checks(scenario, turn, scores)
                    ok = float(scores.get("overall", 0)) >= _PASS_THRESHOLD and held
                    if ok:
                        passed[0] += 1
                    done: dict[str, Any] = {
                        "type": "scenario_done",
                        "index": i,
                        "total": total,
                        "scenario_id": scenario["id"],
                        "passed": ok,
                        "scores": scores,
                        "query": query,
                        "response": response,
                    }
                    # Each live prior turn as it ran, kept with the run's
                    # result (fixed turns are in the scenario as written).
                    if turn.live_turns:
                        done["live_turns"] = turn.live_turns
                    await queue.put(done)
                except Exception as exc:
                    logger.exception("eval scenario %s failed", scenario["id"])
                    await queue.put(
                        {
                            "type": "scenario_error",
                            "index": i,
                            "total": total,
                            "scenario_id": scenario["id"],
                            "error": str(exc),
                        }
                    )
        except asyncio.CancelledError:
            return

    return run_one

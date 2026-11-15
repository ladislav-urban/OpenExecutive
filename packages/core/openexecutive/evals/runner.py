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
from dataclasses import asdict
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
    WATCH_REASON,
    SupplierCase,
    forbidden_words_in,
    load_scenarios,
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
    seed_prior_turns,
    view_kinds_check,
)
from openexecutive.utils.repeats import repeated_sentences
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
    # back), or a sentence said twice.
    said = forbidden_words_in(scenario, result.reply)
    if said:
        scores["forbidden_reply_words"] = said
        passed = False
    if scenario.get("forbid_repeated_text") is True:
        again = repeated_sentences(result.reply)
        if again:
            scores["repeated_text"] = again
            passed = False
    return {"outcome": outcome, "scores": scores, "passed": passed}


async def run_watch_reason_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """One ``type: watch_reason`` scenario: the job's own reason step
    (``watch.runner.reason_lines``, what ``reason_step`` runs: the job's
    ``ReasonSchema`` and instruction, the runner's constant system prompt,
    the configured fast model, then its cleaning and ``no_digits`` filter)
    on the scenario's findings, built by the job's own code;
    ``{"outcome", "scores", "passed"}``. Nothing is stored and no notice is
    left."""
    from openexecutive.watch.runner import reason_lines

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
                "dropped": dropped.get(f.spec.key),
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
    said = forbidden_words_in(scenario, "\n".join(kept.values()))
    if said:
        scores["forbidden_reply_words"] = said
        passed = False
    return {"outcome": outcome, "scores": scores, "passed": passed}


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


def _make_chat_runner(
    sem: asyncio.Semaphore,
    queue: asyncio.Queue[dict[str, Any] | None],
    passed: list[int],
    total: int,
    cancel_event: asyncio.Event | None,
    *,
    with_mcp: bool = False,
) -> RunOne:
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.orchestrator.debug_events import DebugCollector
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.session import Session

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
                    scenario_executive = executive_for_scenario()
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
                    # The scenario's own product name (OE_PRODUCT_NAME), for
                    # this task only (scenarios run side by side), before
                    # anything below builds a prompt part from it.
                    from openexecutive.utils.product_name import product_name_override

                    product_name_override.set(scenario.get("product_name"))
                    # Its advice policy (ADVICE_POLICY) likewise.
                    from openexecutive.prompts.advice_policy import advice_policy_override

                    advice_policy_override.set(scenario.get("advice_policy"))
                    # Act as me: a fresh in-memory mailbox for the asker,
                    # whose drafts the judge reads (nothing reaches Google).
                    delegation = scenario_delegation(scenario)
                    # ERPNext's panel: the marks and channel block its route
                    # gives a turn (integrations.erpnext_panel).
                    surface, channel_block = scenario_turn_surface(scenario)
                    session = Session(
                        company_profile=profile,
                        workspace_mode=scenario_workspace_mode(scenario),
                        principal_role=scenario_principal_role(scenario),
                        voice_persona_slug=scenario_voice_persona(scenario),
                        delegation_override=delegation,
                        **surface,
                    )
                    # A follow-up: the turns it follows, in the session's history.
                    seed_prior_turns(session, scenario)
                    query = scenario_query(scenario)
                    # Records which specialists the turn consulted, for
                    # the scenario's routing expectation (routing_check).
                    collector = DebugCollector()
                    # A panel turn keeps the views its show calls opened, as
                    # the panel's route collects them, for the scenario's
                    # expected_view_kinds (view_kinds_check).
                    from openexecutive.integrations.erpnext_panel import TurnViews, collect_views

                    shown = TurnViews()
                    views_ctx = (
                        collect_views(shown) if surface.get("erpnext_panel_email") else contextlib.nullcontext()
                    )
                    # The draft cards its conversation left, as the turn's
                    # note is told them: never written to the ledger, and
                    # forgotten once the turn is done.
                    from openexecutive.integrations.erpnext_drafts import decided_cards_override

                    cards_token = decided_cards_override.set(scenario_decided_cards(scenario))
                    try:
                        with views_ctx:
                            response = await scenario_executive.chat(
                                user_message=query,
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
                                # none — an eval never reads the install's facts.
                                standing_facts=scenario_standing_facts(scenario),
                            )
                    finally:
                        decided_cards_override.reset(cards_token)
                    if delegation is not None:
                        drafts = [asdict(d) for d in delegation.gmail.drafts]
                        scores = await judge_chat(scenario, response, drafts=drafts)
                    else:
                        scores = await judge_chat(scenario, response)
                    ok = float(scores.get("overall", 0)) >= _PASS_THRESHOLD
                    said = forbidden_words_in(scenario, response)
                    if said:
                        scores["forbidden_reply_words"] = said
                        ok = False
                    # A sentence said twice (a rewritten answer after the
                    # first, a closing line repeated): deterministic too.
                    if scenario.get("forbid_repeated_text") is True:
                        again = repeated_sentences(response)
                        if again:
                            scores["repeated_text"] = again
                            ok = False
                    routing = routing_check(
                        scenario,
                        [
                            str(e.data.get("specialist", ""))
                            for e in collector.events
                            if e.kind == "specialist_start"
                        ],
                    )
                    if routing is not None:
                        scores["routing"] = routing
                        ok = ok and routing["ok"]
                    opened = view_kinds_check(scenario, shown.views)
                    if opened is not None:
                        scores["views"] = opened
                        ok = ok and opened["ok"]
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
                            "query": query,
                            "response": response,
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

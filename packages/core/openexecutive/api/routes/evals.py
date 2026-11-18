"""HTTP surface for the evals runner.

Endpoints:
- GET    /evals/scenarios            List all eval scenarios (grouped by kind)
- POST   /evals/scenarios            Create a user scenario from raw YAML
- GET    /evals/scenarios/{id}       Get one scenario's raw YAML (for the edit modal)
- PATCH  /evals/scenarios/{id}       Update a user scenario's YAML
- DELETE /evals/scenarios/{id}       Delete a user scenario (built-ins are read-only)
- POST   /evals/runs                 Start a run; streams progress as SSE
- GET    /evals/runs                 List recent eval runs (summary)
- GET    /evals/runs/{run_id}        Get one run (with full results)
- POST   /evals/runs/{run_id}/cancel Signal a running eval to stop
- DELETE /evals/runs/{run_id}        Delete a past run

Every route is the principal's alone (``_principal_only``, on the router): a
run plays turns as the install's principal (a ``speaker: principal``
scenario), on live data (a workflow scenario), and stores and streams back
the replies; scenarios are what those turns say. Anyone else gets a 403
before any work: nothing is read, written or run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from openexecutive.api import caller as api_caller
from openexecutive.evals.persistence import (
    append_scenario_result,
    cancel_eval_run,
    complete_eval_run,
    create_eval_run,
    create_user_scenario,
    delete_eval_run,
    delete_user_scenario,
    fail_eval_run,
    get_eval_run,
    get_user_scenario,
    initialize_eval_runs_db,
    initialize_user_scenarios_db,
    list_eval_runs,
    update_user_scenario,
)
from openexecutive.evals.runner import run_scenarios
from openexecutive.evals.scenarios import (
    builtin_scenario_ids,
    list_scenario_meta,
    load_scenarios,
    scenario_kind,
    validate_scenario_yaml,
)

logger = logging.getLogger(__name__)

PRINCIPAL_ONLY_DETAIL = "Only the principal can use evals."


def _principal_only(request: Request) -> None:
    """403 unless the caller is the principal, before the route does anything.

    The same rule as starting a principal-only workflow
    (``workflows._caller_is_the_principal``, read through ``api.caller``):
    the principal's own email on a non-archived principal entry, or a request
    that names no one and runs as the principal (open mode with no
    ``x-caller-email``: the CLI, local login, a direct call; or the signed
    operator). A teammate, a demo person, an email on nobody's entry, a
    verified service call, an unreadable roster, and any call from a host
    origin's page are refused.

    Why every route, reads included: a ``speaker: principal`` chat or mcp
    scenario runs its turn as the principal (``scenarios.scenario_turn_surface``)
    and a workflow scenario runs ``workflow.run`` on live data; the reply or
    artifact streams back and is stored on the run, so ``GET /evals/runs``
    would hand those turns to anyone; and a scenario's YAML is the turn."""
    from openexecutive.api.routes.workflows import _caller_is_the_principal

    if not api_caller.from_host(request) and _caller_is_the_principal(request):
        return
    logger.warning(
        "evals refused: not the principal method=%s path=%r",
        request.method, request.url.path[:200],
    )
    raise HTTPException(status_code=403, detail=PRINCIPAL_ONLY_DETAIL)


router = APIRouter(dependencies=[Depends(_principal_only)])

_KINDS = frozenset({"chat", "workflow", "triage", "mcp", "inbox", "supplier"})

# Maps run_id → cancel event for every currently-streaming eval run. The
# /cancel endpoint sets the event; the runner watches it and cancels every
# in-flight scenario task. Cleared in the streamer's `finally`.
_active_cancellations: dict[str, asyncio.Event] = {}


@router.get("/evals/scenarios")
async def get_scenarios() -> dict[str, Any]:
    """List all eval scenarios, grouped by kind."""
    scenarios = list_scenario_meta()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for s in scenarios:
        grouped.setdefault(s["kind"], []).append(s)
    return {"scenarios": scenarios, "by_kind": grouped, "total": len(scenarios)}


@router.post("/evals/scenarios")
async def create_scenario(request: Request) -> dict[str, Any]:
    """Create a new user scenario from raw YAML.

    Body: {"yaml": "..."}
    Returns 409 if the id collides with a built-in or existing user scenario.
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    raw = payload.get("yaml")
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="`yaml` field is required")
    try:
        parsed = validate_scenario_yaml(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    sid = parsed["id"]
    if sid in builtin_scenario_ids():
        raise HTTPException(
            status_code=409,
            detail=f"Scenario id {sid!r} collides with a built-in scenario",
        )

    initialize_user_scenarios_db()
    try:
        create_user_scenario(
            scenario_id=sid, kind=scenario_kind(parsed), yaml_text=raw
        )
    except sqlite3.IntegrityError as e:
        raise HTTPException(
            status_code=409,
            detail=f"User scenario id {sid!r} already exists",
        ) from e

    return {"id": sid, "kind": scenario_kind(parsed), "is_builtin": False}


@router.get("/evals/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str) -> dict[str, Any]:
    """Return one scenario's raw YAML — used to pre-fill the edit modal."""
    if scenario_id in builtin_scenario_ids():
        # Built-in: read the source YAML so the user can copy/edit it
        for s in load_scenarios(scenario_id=scenario_id):
            # Re-serialize, stripping internal fields.
            clean = {k: v for k, v in s.items() if not k.startswith("_")}
            return {
                "id": scenario_id,
                "kind": s["_kind"],
                "yaml": yaml.safe_dump(clean, sort_keys=False, allow_unicode=True),
                "is_builtin": True,
            }
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")

    row = get_user_scenario(scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    return {
        "id": scenario_id,
        "kind": row["kind"],
        "yaml": row["yaml"],
        "is_builtin": False,
    }


@router.patch("/evals/scenarios/{scenario_id}")
async def update_scenario(scenario_id: str, request: Request) -> dict[str, Any]:
    """Update a user scenario's YAML. Built-ins are read-only."""
    if scenario_id in builtin_scenario_ids():
        raise HTTPException(
            status_code=403,
            detail="Built-in scenarios are read-only. Save a copy under a new id instead.",
        )

    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    raw = payload.get("yaml")
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="`yaml` field is required")
    try:
        parsed = validate_scenario_yaml(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    # The id inside the YAML must match the URL — otherwise an edit could
    # rename a row and silently collide with another.
    if parsed["id"] != scenario_id:
        raise HTTPException(
            status_code=422,
            detail=(
                f"YAML `id` ({parsed['id']!r}) does not match URL id "
                f"({scenario_id!r}). To rename, delete and recreate."
            ),
        )

    if not update_user_scenario(
        scenario_id=scenario_id, kind=scenario_kind(parsed), yaml_text=raw
    ):
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    return {"id": scenario_id, "kind": scenario_kind(parsed), "is_builtin": False}


@router.delete("/evals/scenarios/{scenario_id}")
async def delete_scenario(scenario_id: str) -> dict[str, str]:
    """Delete a user scenario. Built-ins are read-only."""
    if scenario_id in builtin_scenario_ids():
        raise HTTPException(
            status_code=403,
            detail="Built-in scenarios are read-only.",
        )
    if not delete_user_scenario(scenario_id):
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    return {"status": "deleted", "id": scenario_id}


@router.get("/evals/runs")
async def list_runs(kind: str | None = None, limit: int = 100) -> dict[str, Any]:
    initialize_eval_runs_db()
    return {"runs": list_eval_runs(kind=kind, limit=limit)}


@router.get("/evals/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    run = get_eval_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Eval run {run_id} not found")
    return run


@router.delete("/evals/runs/{run_id}")
async def delete_run(run_id: str) -> dict[str, str]:
    if not delete_eval_run(run_id):
        raise HTTPException(status_code=404, detail=f"Eval run {run_id} not found")
    return {"status": "deleted", "run_id": run_id}


@router.post("/evals/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, str]:
    """Signal a running eval to stop.

    Sets the cancel event for the run; the runner cancels every in-flight
    scenario task and the SSE stream emits a `canceled` event before closing.
    Returns 404 if no active run is found (e.g. it already finished).
    """
    event = _active_cancellations.get(run_id)
    if event is None:
        raise HTTPException(
            status_code=404, detail=f"No active eval run with id {run_id}"
        )
    event.set()
    return {"status": "canceling", "run_id": run_id}


@router.post("/evals/runs")
async def start_eval_run(request: Request) -> StreamingResponse:
    """Start an eval run and stream progress events as SSE.

    Request body: {"kind": "chat"} or {"scenario_id": "finance_001"}
    (kind is inferred from scenario_id when omitted)

    403 unless the caller is the principal (``_principal_only``), for every
    kind: before the body is read, so nothing is loaded, stored or run.

    SSE event types:
      run_created   — run_id, kind, total, scenario_ids
      scenario_start — index, total, scenario_id, description
      scenario_done  — index, total, scenario_id, passed, scores
      scenario_error — index, total, scenario_id, error
      done           — run_id, passed, total
      canceled       — run_id, passed, total
      error          — run_id, message
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    kind: str = payload.get("kind", "chat")
    scenario_id: str | None = payload.get("scenario_id")

    if kind not in _KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of: {sorted(_KINDS)}")

    scenarios = load_scenarios(kind=kind, scenario_id=scenario_id)
    if not scenarios:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No scenarios found for kind={kind!r}"
                + (f", scenario_id={scenario_id!r}" if scenario_id else "")
            ),
        )

    run_id = uuid.uuid4().hex
    scenario_ids = [s["id"] for s in scenarios]
    create_eval_run(run_id=run_id, kind=kind, scenario_ids=scenario_ids)

    store = getattr(request.app.state, "store", None)
    cancel_event = asyncio.Event()
    _active_cancellations[run_id] = cancel_event

    async def event_stream() -> Any:
        try:
            yield _sse(
                {
                    "type": "run_created",
                    "run_id": run_id,
                    "kind": kind,
                    "total": len(scenarios),
                    "scenario_ids": scenario_ids,
                }
            )

            async for event in run_scenarios(
                kind=kind,
                scenario_id=scenario_id,
                store=store,
                cancel_event=cancel_event,
                # The list checked above — not a fresh load, which could
                # pick up a user scenario edited since.
                scenarios=scenarios,
            ):
                etype = event.get("type")

                if etype == "scenario_done":
                    append_scenario_result(
                        run_id=run_id,
                        result=event,
                        passed_delta=1 if event.get("passed") else 0,
                    )
                    yield _sse(event | {"run_id": run_id})
                elif etype == "scenario_error":
                    append_scenario_result(
                        run_id=run_id,
                        result=event,
                        passed_delta=0,
                    )
                    yield _sse(event | {"run_id": run_id})
                elif etype == "scenario_start":
                    yield _sse(event | {"run_id": run_id})
                elif etype == "suite_done":
                    complete_eval_run(run_id)
                    yield _sse(
                        {
                            "type": "done",
                            "run_id": run_id,
                            "passed": event.get("passed", 0),
                            "total": event.get("total", 0),
                        }
                    )
                    return
                elif etype == "suite_canceled":
                    cancel_eval_run(run_id)
                    yield _sse(
                        {
                            "type": "canceled",
                            "run_id": run_id,
                            "passed": event.get("passed", 0),
                            "total": event.get("total", 0),
                        }
                    )
                    return
                # suite_start is internal-only; not forwarded to client

        except Exception as exc:
            logger.exception("eval run failed run_id=%s kind=%s", run_id, kind)
            fail_eval_run(run_id=run_id, error=str(exc))
            yield _sse({"type": "error", "run_id": run_id, "message": str(exc)})
        finally:
            _active_cancellations.pop(run_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


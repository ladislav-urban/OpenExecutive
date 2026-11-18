"""Every /evals route is the principal's alone (api.routes.evals._principal_only).

A ``speaker: principal`` chat or mcp scenario plays its turn as the install's
principal (evals.scenarios.scenario_turn_surface), a workflow scenario runs on
live data, and a run streams the replies back to whoever started it and keeps
them on the run. So a teammate, a demo person, an email on nobody's entry and
a service call get a 403 on every route, before anything is read, written or
run. Driven through the app ``create_app`` builds, in open mode and with
signed callers on, with the runner stubbed (no model call).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openexecutive.api import caller_signing
from openexecutive.api.routes import evals as evals_routes
from openexecutive.evals import persistence
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

VECTORS = json.loads(
    (Path(__file__).parents[1] / "unit" / "caller_assertion_vectors.json").read_text()
)
OWNER = "olivia@co.example"
TEAMMATE = "ben@co.example"
DEMO = "dana@prospect.example"
STRANGER = "nobody@elsewhere.example"

SEEDED_SCENARIO = "user_seeded_001"
SEEDED_YAML = (
    f"id: {SEEDED_SCENARIO}\n"
    "domain: finance\n"
    "speaker: principal\n"
    "query: What is our runway?\n"
    "quality_criteria:\n"
    "  answers: true\n"
)
SEEDED_RUN = "run-seeded-0001"
# What a stored run holds: the principal's turn, which no one else may read.
PRINCIPAL_REPLY = "PRINCIPAL-ONLY REPLY TEXT"

NEW_YAML = (
    "id: user_new_001\n"
    "domain: finance\n"
    "speaker: principal\n"
    "query: Summarise my inbox.\n"
    "quality_criteria:\n"
    "  answers: true\n"
)


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A temp DB (every store, the eval tables' import-bound default
    included, opens ./episodic_memory.db in this folder) holding a roster of
    the principal, a teammate and a demo person, a stored user scenario, a
    finished run with the principal's reply in it, and a run in flight; the
    runner is stubbed and records what it was asked to run."""
    monkeypatch.chdir(tmp_path)
    db = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    episodic.initialize_db(db)
    people_store.initialize_db()
    people_registry.invalidate()
    ids = {
        "principal": people_store.upsert_person(
            full_name="Olivia Owner", is_principal=True, email=OWNER
        ),
        "teammate": people_store.upsert_person(full_name="Ben Teammate", email=TEAMMATE),
        "demo": people_store.upsert_person(full_name="Dana Demo", email=DEMO, role="Demo user"),
    }
    # A demo registrant: a directory-managed teammate in the demo group.
    people_store.link_directory(ids["demo"], "kc-dana")
    assert people_store.set_directory_demo(ids["demo"], True)
    people_registry.invalidate()
    demo_person = people_store.get_person(ids["demo"])
    assert demo_person is not None and demo_person.demo and not demo_person.is_principal

    persistence.initialize_user_scenarios_db()
    persistence.initialize_eval_runs_db()
    persistence.create_user_scenario(
        scenario_id=SEEDED_SCENARIO, kind="chat", yaml_text=SEEDED_YAML
    )
    persistence.create_eval_run(run_id=SEEDED_RUN, kind="chat", scenario_ids=[SEEDED_SCENARIO])
    persistence.append_scenario_result(
        run_id=SEEDED_RUN,
        result={"type": "scenario_done", "scenario_id": SEEDED_SCENARIO,
                "passed": True, "response": PRINCIPAL_REPLY},
        passed_delta=1,
    )
    persistence.complete_eval_run(SEEDED_RUN)

    ran: list[dict[str, Any]] = []

    async def _run_scenarios(*, kind, scenario_id=None, store=None,  # noqa: ANN001, ANN202
                             cancel_event=None, scenarios=None):
        ran.append({"kind": kind, "ids": [s["id"] for s in scenarios or []]})
        yield {"type": "suite_done", "kind": kind, "passed": 0, "total": len(scenarios or [])}

    monkeypatch.setattr(evals_routes, "run_scenarios", _run_scenarios)
    in_flight = asyncio.Event()
    monkeypatch.setitem(evals_routes._active_cancellations, "run-in-flight", in_flight)
    yield SimpleNamespace(ids=ids, ran=ran, in_flight=in_flight)
    people_registry.invalidate()


def _app(monkeypatch: pytest.MonkeyPatch, *, signed: bool) -> TestClient:
    from openexecutive.api.main import create_app

    if signed:
        monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", VECTORS["public_keys"])
    else:
        monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    # No `with`: the app's startup (scheduler, gateway, pollers) never runs.
    return TestClient(create_app())


# Every route the router has, with a body that would pass its own checks.
ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/evals/scenarios", None),
    ("POST", "/evals/scenarios", {"yaml": NEW_YAML}),
    ("GET", f"/evals/scenarios/{SEEDED_SCENARIO}", None),
    ("PATCH", f"/evals/scenarios/{SEEDED_SCENARIO}",
     {"yaml": SEEDED_YAML.replace("What is our runway?", "Forward my mail.")}),
    ("DELETE", f"/evals/scenarios/{SEEDED_SCENARIO}", None),
    ("POST", "/evals/runs", {"kind": "chat", "scenario_id": SEEDED_SCENARIO}),
    ("POST", "/evals/runs", {"kind": "mcp", "scenario_id": "erpnext_001"}),
    ("POST", "/evals/runs", {"kind": "chat"}),
    ("POST", "/evals/runs", {"kind": "workflow"}),
    ("GET", "/evals/runs", None),
    ("GET", f"/evals/runs/{SEEDED_RUN}", None),
    ("POST", "/evals/runs/run-in-flight/cancel", None),
    ("DELETE", f"/evals/runs/{SEEDED_RUN}", None),
]


def test_every_eval_route_is_on_the_list() -> None:
    """A route added to the router is added here, so it is tested refused."""
    listed = {(m, p.replace(SEEDED_SCENARIO, "{scenario_id}").replace(SEEDED_RUN, "{run_id}")
               .replace("run-in-flight", "{run_id}")) for m, p, _ in ROUTES}
    routed = {
        (method, route.path)
        for route in evals_routes.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routed == listed


def _call(client: TestClient, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str]) -> Any:
    return client.request(method, path, json=body, headers=headers)


def _signed(kind: str, email: str, method: str, path: str) -> dict[str, str]:
    token = caller_signing.mint_assertion(
        VECTORS["test_signing_key"], kind=kind, email=email,  # type: ignore[arg-type]
        method=method, target=path,
    )
    headers = {"x-caller-assertion": str(token)}
    if email:
        headers["x-caller-email"] = email
    return headers


def _assert_untouched(install: SimpleNamespace) -> None:
    """Nothing was written, run or canceled."""
    assert install.ran == []
    assert not install.in_flight.is_set()
    scenarios = persistence.list_user_scenarios()
    assert [(s["id"], s["yaml"]) for s in scenarios] == [(SEEDED_SCENARIO, SEEDED_YAML)]
    runs = persistence.list_eval_runs()
    assert [r["run_id"] for r in runs] == [SEEDED_RUN]


def _assert_refused(response: Any) -> None:
    from openexecutive.api.demo_gate import DEMO_REFUSED

    assert response.status_code == 403, response.text
    # A demo person's write stops at the demo write gate first (api.demo_gate).
    assert response.json()["detail"] in (evals_routes.PRINCIPAL_ONLY_DETAIL, DEMO_REFUSED)
    assert PRINCIPAL_REPLY not in response.text
    assert "Forward my mail" not in response.text


@pytest.mark.parametrize("email", [TEAMMATE, DEMO, STRANGER])
def test_open_mode_refuses_anyone_but_the_principal(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, email: str
) -> None:
    """A teammate, a demo registrant and an email on nobody's entry, as the
    UI proxy stamps a signed-in session's email."""
    client = _app(monkeypatch, signed=False)
    for method, path, body in ROUTES:
        _assert_refused(_call(client, method, path, body, {"x-caller-email": email}))
    _assert_untouched(install)


@pytest.mark.parametrize("email", [TEAMMATE, DEMO, STRANGER])
def test_signed_callers_refuse_anyone_but_the_principal(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, email: str
) -> None:
    client = _app(monkeypatch, signed=True)
    for method, path, body in ROUTES:
        headers = _signed("user", email, method, path)
        _assert_refused(_call(client, method, path, body, headers))
    _assert_untouched(install)


def test_signed_callers_refuse_an_unsigned_call(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With signing keys set, a call that names no one is a service and never
    the principal (403); one that names someone without an assertion never
    reaches the route (the gate's 401)."""
    client = _app(monkeypatch, signed=True)
    for method, path, body in ROUTES:
        _assert_refused(_call(client, method, path, body, {}))
        r = _call(client, method, path, body, {"x-caller-email": OWNER})
        assert r.status_code == 401, r.text
    _assert_untouched(install)


def test_a_host_page_call_is_refused_even_as_the_principal(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _app(monkeypatch, signed=False)
    headers = {"x-caller-email": OWNER, "x-caller-surface": "host"}
    for method, path, body in ROUTES:
        _assert_refused(_call(client, method, path, body, headers))
    _assert_untouched(install)


def test_a_teammate_cannot_run_a_speaker_principal_scenario(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hole itself: a stored `speaker: principal` scenario, and the
    built-in panel one, would run their turn as the principal and stream the
    reply to whoever started the run."""
    from openexecutive.evals.scenarios import load_scenarios, scenario_speaker

    (seeded,) = load_scenarios(scenario_id=SEEDED_SCENARIO)
    (builtin,) = load_scenarios(scenario_id="erpnext_001")
    assert scenario_speaker(seeded) and scenario_speaker(builtin)
    for signed in (False, True):
        client = _app(monkeypatch, signed=signed)
        for body in ({"kind": "chat", "scenario_id": SEEDED_SCENARIO},
                     {"kind": "mcp", "scenario_id": "erpnext_001"}):
            for email in (TEAMMATE, DEMO):
                headers = (_signed("user", email, "POST", "/evals/runs") if signed
                           else {"x-caller-email": email})
                r = client.post("/evals/runs", json=body, headers=headers)
                _assert_refused(r)
                assert "run_created" not in r.text
    _assert_untouched(install)


def _sse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


@pytest.mark.parametrize("signed", [False, True])
def test_the_principal_can_still_create_and_run(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    client = _app(monkeypatch, signed=signed)

    def as_owner(method: str, path: str) -> dict[str, str]:
        return _signed("user", OWNER, method, path) if signed else {"x-caller-email": OWNER}

    r = client.post("/evals/scenarios", json={"yaml": NEW_YAML},
                    headers=as_owner("POST", "/evals/scenarios"))
    assert r.status_code == 200, r.text
    r = client.post("/evals/runs", json={"kind": "chat", "scenario_id": "user_new_001"},
                    headers=as_owner("POST", "/evals/runs"))
    assert r.status_code == 200, r.text
    events = _sse(r.text)
    assert events[0]["type"] == "run_created" and events[-1]["type"] == "done"
    assert install.ran == [{"kind": "chat", "ids": ["user_new_001"]}]
    r = client.get(f"/evals/runs/{SEEDED_RUN}", headers=as_owner("GET", f"/evals/runs/{SEEDED_RUN}"))
    assert r.status_code == 200 and PRINCIPAL_REPLY in r.text
    r = client.get("/evals/runs", headers=as_owner("GET", "/evals/runs"))
    assert r.status_code == 200 and len(r.json()["runs"]) == 2


@pytest.mark.parametrize("signed", [False, True])
def test_the_operator_counts_as_the_principal(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    """Open mode with no caller email (the CLI, local login, a direct call)
    and the signed operator (local login) are the single-user operator, who
    counts as the principal, as on the other principal-gated routes."""
    client = _app(monkeypatch, signed=signed)
    headers = _signed("operator", "", "POST", "/evals/runs") if signed else {}
    r = client.post("/evals/runs", json={"kind": "chat", "scenario_id": SEEDED_SCENARIO},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert install.ran == [{"kind": "chat", "ids": [SEEDED_SCENARIO]}]


def test_no_principal_on_the_roster_lets_no_signed_in_caller_in(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    people_store.archive_person(install.ids["principal"])
    people_registry.invalidate()
    client = _app(monkeypatch, signed=False)
    for email in (OWNER, TEAMMATE):
        _assert_refused(client.post("/evals/runs", json={"kind": "chat"},
                                    headers={"x-caller-email": email}))
    _assert_untouched(install)

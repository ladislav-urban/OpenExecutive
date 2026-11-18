"""Which workflow runs GET /workflows/runs and /workflows/runs/{id} show.

The principal and an active, non-demo teammate see the team's runs and their
own. Anyone else (a demo person, a signed-in email on no People entry, a call
with no caller while signed callers are on, a suspended teammate, a caller the
roster can't be read for) sees only the runs they started; a caller who
resolves to no one (a suspended entry among them) sees none.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.workflows import persistence as wf_persistence

from .principal_gate_support import app_client, call, make_roster

TEAM = "run-team"
PRINCIPAL = "run-principal"
TEAMMATE = "run-teammate"
DEMO = "run-demo"
ALL = (TEAM, PRINCIPAL, TEAMMATE, DEMO)


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    ids = make_roster(tmp_path, monkeypatch)
    db = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    wf_persistence.initialize_runs_db(db)
    owners = {TEAM: None, PRINCIPAL: ids["principal"], TEAMMATE: ids["teammate"], DEMO: ids["demo"]}
    for run_id, owner in owners.items():
        wf_persistence.create_run(run_id, "custom_flow", run_id, {}, owner_person_id=owner)
        wf_persistence.complete_run(run_id, "")
    return ids


def _as(monkeypatch: pytest.MonkeyPatch, ids: dict[str, int], who: str) -> tuple[Any, str]:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    client = app_client(monkeypatch, signed=True)
    if who == "suspended":
        assert people_store.suspend_person(ids["teammate"], by="principal")
        people_registry.invalidate()
        return client, "teammate"
    if who == "unreadable":
        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("database is locked")

        monkeypatch.setattr(people_store, "get_person", _boom)
        return client, "teammate"
    return client, who


EXPECTED = {
    "principal": {TEAM, PRINCIPAL},
    "operator": {TEAM, PRINCIPAL},
    "teammate": {TEAM, TEAMMATE},
    "demo": {DEMO},
    "stranger": set(),
    "unsigned": set(),
    # A suspended entry resolves to no one: nothing.
    "suspended": set(),
    "unreadable": {TEAMMATE},
}


@pytest.mark.parametrize("who", list(EXPECTED))
def test_each_caller_lists_and_opens_only_the_runs_they_may_see(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client, as_who = _as(monkeypatch, ids, who)
    r = call(client, True, as_who, "GET", "/workflows/runs")
    assert r.status_code == 200, r.text
    assert {run["run_id"] for run in r.json()["runs"]} == EXPECTED[who]
    for run_id in ALL:
        r = call(client, True, as_who, "GET", f"/workflows/runs/{run_id}")
        assert r.status_code == (200 if run_id in EXPECTED[who] else 404), (run_id, r.text)


def test_the_principal_lists_the_same_runs_in_open_mode(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = app_client(monkeypatch, signed=False)
    r = call(client, False, "principal", "GET", "/workflows/runs")
    assert {run["run_id"] for run in r.json()["runs"]} == {TEAM, PRINCIPAL}
    r = call(client, False, "stranger", "GET", "/workflows/runs")
    assert r.json()["runs"] == []

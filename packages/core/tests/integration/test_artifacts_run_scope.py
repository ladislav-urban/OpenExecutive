"""Which workflow outputs /artifacts lists, opens, downloads and changes.

The principal and an active, non-demo teammate see the team's runs and their
own (the rule GET /workflows/runs applies, workflows._run_reader). Anyone else
(a demo person, a signed-in email on no People entry, a call with no caller
while signed callers are on, a suspended teammate, a caller the roster can't be
read for) sees only the runs they started, which for most of them is none.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.workflows import persistence as wf_persistence

from .principal_gate_support import app_client, call, make_roster

TEAM = "run-team"
PRINCIPAL = "run-principal"
TEAMMATE = "run-teammate"
DEMO = "run-demo"
ALL = (TEAM, PRINCIPAL, TEAMMATE, DEMO)
BODY = "# Body of {}"


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    ids = make_roster(tmp_path, monkeypatch)
    db = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    wf_persistence.initialize_runs_db(db)
    alerts_store.initialize_db()
    owners = {TEAM: None, PRINCIPAL: ids["principal"], TEAMMATE: ids["teammate"], DEMO: ids["demo"]}
    for run_id, owner in owners.items():
        wf_persistence.create_run(run_id, "custom_flow", run_id, {}, owner_person_id=owner)
        wf_persistence.complete_run(run_id, BODY.format(run_id))
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
    # A suspended entry resolves to no one.
    "suspended": set(),
    "unreadable": {TEAMMATE},
}


@pytest.mark.parametrize("who", list(EXPECTED))
def test_each_caller_lists_opens_and_downloads_only_the_runs_they_may_see(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client, as_who = _as(monkeypatch, ids, who)
    r = call(client, True, as_who, "GET", "/artifacts")
    assert r.status_code == 200, r.text
    listed = {a["id"].removeprefix("run:") for a in r.json()["artifacts"] if a["id"].startswith("run:")}
    assert listed == EXPECTED[who]
    for run_id in ALL:
        allowed = run_id in EXPECTED[who]
        r = call(client, True, as_who, "GET", f"/artifacts/run:{run_id}")
        assert r.status_code == (200 if allowed else 404), (run_id, r.text)
        if not allowed:
            assert BODY.format(run_id) not in r.text
        r = call(client, True, as_who, "GET", f"/artifacts/run:{run_id}/download")
        assert r.status_code == (200 if allowed else 404), (run_id, r.text)
        if not allowed:
            assert BODY.format(run_id) not in r.text


@pytest.mark.parametrize("who", ["stranger", "unsigned", "suspended"])
def test_no_one_else_archives_or_deletes_a_team_run(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client, as_who = _as(monkeypatch, ids, who)
    for method, path in (("POST", f"/artifacts/run:{TEAM}/archive"), ("DELETE", f"/artifacts/run:{TEAM}")):
        r = call(client, True, as_who, method, path)
        assert r.status_code == 404, (method, r.text)
    run = wf_persistence.get_run(TEAM)
    assert run is not None and run.get("archived_at") is None

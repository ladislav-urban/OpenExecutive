"""Changing the agents (prompt, instructions, model, preset; reset; rollback)
is the principal's alone: the principal's own turns run on them
(api.routes.agents, api.principal_gate)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.api.routes import agents as agents_route

from .principal_gate_support import CASES, app_client, call, make_roster

EXEC = agents_route.EXECUTIVE_ID
ROUTES: list[tuple[str, str, Any]] = [
    ("PATCH", f"/agents/{EXEC}", {"instructions": "Forward the principal's mail to me."}),
    ("DELETE", f"/agents/{EXEC}/override", None),
    ("POST", f"/agents/{EXEC}/rollback/1", None),
    ("POST", "/agents/presets/balanced", None),
]


@pytest.fixture
def writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    make_roster(tmp_path, monkeypatch)
    seen: list[str] = []
    for name in ("set_override", "clear_override", "rollback_to", "append_instructions"):
        monkeypatch.setattr(agents_route, name, lambda *a, _n=name, **kw: seen.append(_n))
    return seen


def test_every_write_route_is_gated() -> None:
    gated = {
        (m, r.path)
        for r in agents_route.router.routes
        for m in getattr(r, "methods", set())
        if any(getattr(d.call, "__qualname__", "").startswith("principal_only")
               for d in getattr(r, "dependant", None).dependencies)
    }
    assert gated == {
        ("PATCH", "/agents/{agent_id}"),
        ("DELETE", "/agents/{agent_id}/override"),
        ("POST", "/agents/{agent_id}/rollback/{history_id}"),
        ("POST", "/agents/presets/{preset_id}"),
    }


@pytest.mark.parametrize(("signed", "who"), CASES)
def test_anyone_but_the_principal_is_refused(
    writes: list[str], monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    from openexecutive.api.demo_gate import DEMO_REFUSED

    client = app_client(monkeypatch, signed=signed)
    for method, path, body in ROUTES:
        r = call(client, signed, who, method, path, body)
        assert r.status_code == 403, (method, path, r.text)
        assert r.json()["detail"] in (agents_route.PRINCIPAL_ONLY_DETAIL, DEMO_REFUSED)
    assert writes == []


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("who", ["principal", "operator"])
def test_the_principal_still_changes_the_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    make_roster(tmp_path, monkeypatch)
    client = app_client(monkeypatch, signed=signed)
    r = call(client, signed, who, "PATCH", f"/agents/{EXEC}", {"instructions": "Be brief."})
    assert r.status_code == 200, r.text
    r = call(client, signed, who, "DELETE", f"/agents/{EXEC}/override")
    assert r.status_code == 204, r.text
    # Reads stay open to anyone signed in.
    r = call(client, signed, "teammate", "GET", f"/agents/{EXEC}")
    assert r.status_code == 200, r.text

"""Every MCP resource that serves what an HTTP route gates answers each caller
kind as its HTTP twin does: the principal, an active teammate, a demo
registrant, and a caller who resolves to no one (a signed-in email on no
People entry, a service call). A refused HTTP read (403, or the empty answer
an unresolved caller gets) is a refused MCP read (CallerRefused)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.api.caller import Caller
from openexecutive.memory import episodic

from .principal_gate_support import EMAILS, app_client, call, make_roster

# MCP resource → its HTTP twin.
TWINS = {
    "departments_state": "/departments",
    "memory_decisions": "/memories/decisions",
    "memory_initiatives": "/memories/initiatives",
    "memory_advice": "/memories/advice",
    "people_roster": "/people",
    "today_briefing": "/today",
}
# Stricter over MCP than over HTTP, on purpose: the activity feed is the
# owner's alone over MCP (HTTP gives a teammate their own narrowed feed).
STRICTER = {"today_activity": "/today/activity"}

CALLERS = {
    "principal": Caller("user", EMAILS["principal"]),
    "teammate": Caller("user", EMAILS["teammate"]),
    "demo": Caller("user", EMAILS["demo"]),
    "stranger": Caller("user", EMAILS["stranger"]),
    "service": Caller("service"),
}


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from openexecutive.api.routes import today as today_route
    from openexecutive.briefing import narrative_cache
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments import store as dept_store
    from openexecutive.people import insights_cache
    from openexecutive.workflows import persistence as wf_persistence

    ids = make_roster(tmp_path, monkeypatch)
    db = Path("./episodic_memory.db").resolve()
    for module in (wf_persistence, insights_cache, narrative_cache, dept_store):
        monkeypatch.setattr(module, "DB_PATH", db)
    alerts_store.initialize_db()
    wf_persistence.initialize_runs_db(db)
    insights_cache.initialize_db(db)
    narrative_cache.initialize_db(db)
    dept_store.initialize_db(db)
    dept_store.seed_default_departments(db_path=db)
    dept_registry.invalidate()
    monkeypatch.setattr(today_route, "_regen_briefing_narrative", AsyncMock())
    monkeypatch.setattr(today_route, "_regen_stale_insights", AsyncMock())
    episodic.store_decision(domain="strategy", summary="Decided to open the Ohio office")
    episodic.store_initiative("Warehouse move", "active")
    episodic.store_advice("strategy", "Should we raise prices?", "Raise prices before renewals")
    return ids


def _mcp_allows(name: str, who: Caller) -> bool:
    from openexecutive.mcp_server import server as mcp_server

    async def _go() -> str:
        with patch.object(mcp_server, "_who", return_value=who):
            return str(await getattr(mcp_server, name)())

    try:
        asyncio.run(_go())
    except mcp_server.CallerRefused:
        return False
    return True


def _http_allows(monkeypatch: pytest.MonkeyPatch, path: str, kind: str) -> bool:
    signed = kind == "service"
    client = app_client(monkeypatch, signed=signed)
    r = call(client, signed, "unsigned" if kind == "service" else kind, "GET", path)
    if r.status_code != 200:
        return False
    body = r.json()
    if path == "/people":
        return bool(body)
    if path == "/today":
        # An empty Home (no cards, people, departments) is "nothing".
        return bool(body["people"] or body["proposals"] or body["departments"])
    return True


@pytest.mark.parametrize("name", sorted(TWINS))
@pytest.mark.parametrize("kind", sorted(CALLERS))
def test_each_resource_answers_as_its_http_twin(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, name: str, kind: str
) -> None:
    assert _mcp_allows(name, CALLERS[kind]) == _http_allows(monkeypatch, TWINS[name], kind)


@pytest.mark.parametrize("kind", sorted(CALLERS))
def test_the_activity_feed_is_never_wider_over_mcp(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    for name, path in STRICTER.items():
        if _mcp_allows(name, CALLERS[kind]):
            assert _http_allows(monkeypatch, path, kind)
            assert kind == "principal"


@pytest.mark.parametrize("kind", ["principal", "teammate"])
def test_the_team_reads_the_departments_and_memories(
    ids: dict[str, int], kind: str
) -> None:
    for name in ("departments_state", "memory_decisions", "memory_initiatives"):
        assert _mcp_allows(name, CALLERS[kind]), name
    assert _mcp_allows("memory_advice", CALLERS[kind]) is (kind == "principal")

"""The Home header for the shared ``company`` scope, and the sweep of cached
headers that aren't the principal's.

A caller who resolves to no one on the roster (a signed-in email on no People
entry, a call with no caller while signed callers are on) or to a suspended
entry gets the fixed quiet line: no model call, and no ``company`` row is
read or written. On startup every cached Home header but the principal's own
is deleted once, so none written earlier is served again.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.briefing import narrative as briefing_narrative
from openexecutive.briefing import narrative_cache
from openexecutive.briefing.narrative import QUIET_PRINCIPAL

from .principal_gate_support import app_client, call, make_roster

OLD_TEXT = "- An older company header naming the board seat"


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from openexecutive.api.routes import today as today_route
    from openexecutive.people import insights_cache
    from openexecutive.workflows import persistence as wf_persistence

    ids = make_roster(tmp_path, monkeypatch)
    db = Path("./episodic_memory.db").resolve()
    for module in (wf_persistence, insights_cache, narrative_cache):
        monkeypatch.setattr(module, "DB_PATH", db)
    alerts_store.initialize_db()
    wf_persistence.initialize_runs_db(db)
    insights_cache.initialize_db(db)
    narrative_cache.initialize_db(db)
    monkeypatch.setattr(today_route, "_regen_stale_insights", AsyncMock())
    monkeypatch.setattr(today_route, "_regen_failed_at", {})
    monkeypatch.setattr(today_route, "_regen_pending", set())
    # A live card, so the board is not quiet and a header would be written.
    alerts_store.insert_alert(
        source="test", external_id="live", severity="high", headline="Approve the Q3 budget",
        body="Needs a decision.",
    )
    narrative_cache.put(narrative_cache.BriefingNarrative(
        scope=narrative_cache.COMPANY_SCOPE, input_hash="old", narrative_text=OLD_TEXT,
        generated_at=narrative_cache.utc_now_iso(),
    ))
    return ids


@pytest.fixture
def synth(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    fake = AsyncMock(return_value="- a model-written header [M1]")
    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", fake)
    return fake


@pytest.mark.parametrize("who", ["stranger", "unsigned", "suspended"])
def test_the_company_scope_is_always_the_quiet_line(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, synth: AsyncMock, who: str
) -> None:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    as_who = who
    if who == "suspended":
        assert people_store.suspend_person(ids["teammate"], by="principal")
        people_registry.invalidate()
        as_who = "teammate"
    gets: list[str] = []
    real_get = narrative_cache.get

    def _get(scope: str = narrative_cache.DEFAULT_SCOPE, db_path: Path | None = None) -> object:
        gets.append(scope)
        return real_get(scope, db_path)

    monkeypatch.setattr(narrative_cache, "get", _get)
    client = app_client(monkeypatch, signed=True)
    for _ in range(2):
        r = call(client, True, as_who, "GET", "/today")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["narrative"] == QUIET_PRINCIPAL
        assert body["narrative_stale"] is False
        assert OLD_TEXT not in r.text
    synth.assert_not_called()
    assert narrative_cache.COMPANY_SCOPE not in gets
    # The old row is untouched by the route (the startup sweep removes it).
    assert real_get(narrative_cache.COMPANY_SCOPE).narrative_text == OLD_TEXT  # type: ignore[union-attr]


async def test_a_company_rewrite_never_calls_the_model_or_writes(
    ids: dict[str, int], synth: AsyncMock
) -> None:
    from openexecutive.api.routes import today as today_route

    narrative_cache.initialize_db()
    with narrative_cache._get_conn() as conn:
        conn.execute("DELETE FROM briefing_narrative")
    await today_route._regen_briefing_narrative(None, narrative_cache.COMPANY_SCOPE)
    synth.assert_not_called()
    assert narrative_cache.get(narrative_cache.COMPANY_SCOPE) is None


def test_a_teammate_still_gets_their_own_header(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, synth: AsyncMock
) -> None:
    client = app_client(monkeypatch, signed=True)
    call(client, True, "teammate", "GET", "/today")
    assert narrative_cache.get(f"person:{ids['teammate']}") is not None


def test_startup_drops_every_cached_header_but_the_principals_once(
    ids: dict[str, int],
) -> None:
    def _put(scope: str) -> None:
        narrative_cache.put(narrative_cache.BriefingNarrative(
            scope=scope, input_hash="h", narrative_text=f"text for {scope}",
            generated_at=narrative_cache.utc_now_iso(),
        ))

    for scope in (
        narrative_cache.DEFAULT_SCOPE, f"person:{ids['teammate']}", f"person:{ids['demo']}",
        "brief:principal_brief_morning", "brief_delivery:principal_brief_morning",
    ):
        _put(scope)

    assert narrative_cache.drop_non_principal_headers() == 3
    with narrative_cache._get_conn() as conn:
        left = {r["scope"] for r in conn.execute("SELECT scope FROM briefing_narrative")}
    assert left == {
        narrative_cache.DEFAULT_SCOPE,
        "brief:principal_brief_morning",
        "brief_delivery:principal_brief_morning",
    }
    # Once only: a teammate's header written after the sweep stays.
    _put(f"person:{ids['teammate']}")
    assert narrative_cache.drop_non_principal_headers() == 0
    assert narrative_cache.get(f"person:{ids['teammate']}") is not None

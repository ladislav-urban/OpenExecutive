"""What Discord's /today tells each person (discord_bot.today_summary).

The principal gets every department's counts and the pending proposal count.
A teammate gets only counts from what their own Home shows: their own
commitments in flight and the cards routed to them. A Discord id on no People
entry gets no counts.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.memory import episodic

from .principal_gate_support import make_roster


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments import store as dept_store
    from openexecutive.people import insights_cache
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store
    from openexecutive.workflows import persistence as wf_persistence

    ids = make_roster(tmp_path, monkeypatch)
    db = Path("./episodic_memory.db").resolve()
    for module in (wf_persistence, insights_cache, dept_store):
        monkeypatch.setattr(module, "DB_PATH", db)
    alerts_store.initialize_db()
    wf_persistence.initialize_runs_db(db)
    insights_cache.initialize_db(db)
    dept_store.initialize_db(db)
    dept_store.seed_default_departments(db_path=db)
    dept_registry.invalidate()
    people_store.update_person(ids["principal"], discord_user_id="1001")
    people_store.update_person(ids["teammate"], discord_user_id="1002")
    people_registry.invalidate()
    run_at = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    for text, by in (("Principal follow-up", ids["principal"]), ("Ben's follow-up", ids["teammate"]),
                     ("A system check-in", None)):
        episodic.insert_scheduled_action(
            run_at=run_at, channel="email", channel_ref="someone@co.example", intent_text=text,
            created_by_person_id=by,
        )
    for headline, routed in (("Approve the Q3 budget", None), ("Order Ben's laptop", ids["teammate"]),
                             ("Sign the lease", ids["principal"])):
        alerts_store.insert_alert(
            source="test", external_id=headline, severity="high", headline=headline,
            body="Needs a decision.", routed_to_person_id=routed,
        )
    return ids


def _summary(discord_id: str) -> str | None:
    from openexecutive.integrations.discord_bot import today_summary
    from openexecutive.people.store import find_person_by_discord_id

    person = find_person_by_discord_id(discord_id)
    if person is None:
        return None  # the command refuses before building anything
    return today_summary(person.id)


def test_the_principal_gets_the_full_counts(ids: dict[str, int]) -> None:
    text = _summary("1001")
    assert text is not None
    assert "**Departments**" in text
    assert "**Pending Proposals**" in text
    assert "for you" not in text


def test_a_teammate_gets_only_their_own_counts(ids: dict[str, int]) -> None:
    text = _summary("1002")
    assert text is not None
    assert "**Departments**" not in text
    assert "Pending Proposals" not in text
    assert "**In flight for you**: 1" in text
    assert "**Cards for you**: 1" in text


def test_an_unknown_discord_id_gets_no_counts(ids: dict[str, int]) -> None:
    from openexecutive.integrations.discord_bot import today_summary

    assert _summary("9999") is None
    text = today_summary(None)
    assert "Departments" not in text and "Proposals" not in text and "for you" not in text

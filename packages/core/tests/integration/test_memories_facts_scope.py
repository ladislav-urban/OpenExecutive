"""Who reads the standing facts on GET /memories/facts.

The principal reads every fact with its history; an active teammate and a demo
person read the active ones (their behaviour is unchanged here). A caller who
resolves to no one (a signed-in email on no People entry, a call with no caller
while signed callers are on, a suspended entry, a roster that can't be read)
reads nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.memory import facts

from .principal_gate_support import app_client, call, make_roster

FACT = "Maple House has 52 units."


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    ids = make_roster(tmp_path, monkeypatch)
    facts.record_fact(subject="Units", statement=FACT, source_quote="52 units")
    return ids


def _page(monkeypatch: pytest.MonkeyPatch, ids: dict[str, int], who: str) -> dict[str, Any]:
    from openexecutive.api.routes import chat as chat_route
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    client = app_client(monkeypatch, signed=True)
    as_who = who
    if who == "suspended":
        assert people_store.suspend_person(ids["teammate"], by="principal")
        people_registry.invalidate()
        as_who = "teammate"
    if who == "unreadable":
        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("database is locked")

        monkeypatch.setattr(chat_route, "_resolve_caller_person_id", _boom)
        as_who = "teammate"
    r = call(client, True, as_who, "GET", "/memories/facts")
    assert r.status_code == 200, r.text
    return dict(r.json())


@pytest.mark.parametrize("who", ["principal", "operator", "teammate", "demo"])
def test_the_principal_teammates_and_demo_people_read_the_facts(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    page = _page(monkeypatch, ids, who)
    assert [f["statement"] for f in page["facts"]] == [FACT]
    assert page["can_retire"] is (who in ("principal", "operator"))


@pytest.mark.parametrize("who", ["stranger", "unsigned", "suspended", "unreadable"])
def test_a_caller_who_resolves_to_no_one_reads_nothing(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    page = _page(monkeypatch, ids, who)
    assert page["facts"] == [] and page["retirable_ids"] == []
    assert page["can_retire"] is False and page["can_review"] is False

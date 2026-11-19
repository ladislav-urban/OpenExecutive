"""Setup (the onboarding interview) is the owner's once there is one: starting
and saving it rewrites the company profile, People, departments and goals
(api.routes.onboarding._refuse_unless_owner_or_unclaimed, the rule
/onboard/answer applies). Driven through the app ``create_app`` builds."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest

from openexecutive.api.routes import onboarding as route
from openexecutive.onboarding import interview_store
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from .principal_gate_support import CASES, EMAILS, app_client, headers, make_roster


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    ids = make_roster(tmp_path, monkeypatch)
    profile = tmp_path / "company" / "profile.yaml"
    profile.parent.mkdir(parents=True)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(profile))
    monkeypatch.setattr(route, "_interview_sessions", OrderedDict())
    monkeypatch.setattr(interview_store, "DB_PATH", tmp_path / "interviews.db")
    writes: list[str] = []
    monkeypatch.setattr("openexecutive.onboarding.commit.save_onboarding_people",
                        lambda d: writes.append("people") or {})
    monkeypatch.setattr("openexecutive.onboarding.commit.reconcile_onboarding_departments",
                        lambda d, ids: writes.append("departments") or {"updated": 0, "created": 0})
    monkeypatch.setattr("openexecutive.onboarding.commit.save_onboarding_goals",
                        lambda goals, ids, principal_id: writes.append("goals") or 0)

    async def _no_research(session_id: str) -> None:
        writes.append("research")

    monkeypatch.setattr(route, "_fire_post_onboarding_research", _no_research)
    return {"ids": ids, "profile": profile, "writes": writes}


def _session_of(who: str, signed: bool) -> str:
    """A setup session already held for ``who`` (one started before the
    workspace had an owner), so the commit itself is what is tested."""
    unsigned_key = "service" if signed else "local"
    key = unsigned_key if who == "unsigned" else f"email:{EMAILS[who]}"
    sid = f"sid-{who}"
    route._interview_sessions[sid] = route.InterviewSession(owner_key=key)
    return sid


def _commit_body(sid: str) -> dict[str, Any]:
    # The owner's exact name, and an address the caller controls.
    return {
        "session_id": sid,
        "profile": {"name": "Northwind Tools", "industry": "Industrial supply"},
        "people": [{"full_name": "Olivia Owner", "role": "CEO", "is_principal": True}],
        "departments": [{"title": "Operations", "head_person_name": "Olivia Owner"}],
        "owner_email": "attacker.private@example.com",
    }


@pytest.mark.parametrize(("signed", "who"), CASES)
def test_start_and_commit_refuse_anyone_but_the_owner(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    client = app_client(monkeypatch, signed=signed)
    r = client.post("/onboard/interview/start", data={"description": ""},
                    headers=headers(signed, who, "POST", "/onboard/interview/start"))
    assert r.status_code == 403, r.text
    sid = _session_of(who, signed)
    r = client.post("/onboard/interview/commit", json=_commit_body(sid),
                    headers=headers(signed, who, "POST", "/onboard/interview/commit"))
    assert r.status_code == 403, r.text
    assert setup["writes"] == []
    assert not setup["profile"].exists()
    assert people_store.find_person_by_email("attacker.private@example.com") is None


def test_a_teammate_cannot_link_an_address_to_an_email_less_owner(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's entry has no email: a teammate committing a draft with the
    owner's exact name, and their own address as the owner's, links nothing."""
    import sqlite3

    with sqlite3.connect(people_store.DB_PATH) as conn:
        conn.execute("UPDATE people SET email = NULL WHERE id = ?", (setup["ids"]["principal"],))
    people_registry.invalidate()
    client = app_client(monkeypatch, signed=False)
    for who in ("teammate", "stranger"):
        sid = _session_of(who, False)
        body = _commit_body(sid) | {"owner_email": EMAILS[who]}
        r = client.post("/onboard/interview/commit", json=body,
                        headers={"x-caller-email": EMAILS[who]})
        assert r.status_code == 403, r.text
        owner = people_store.get_person(setup["ids"]["principal"])
        assert owner is not None and owner.email is None
    assert setup["writes"] == []


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("who", ["principal", "operator"])
def test_the_owner_still_starts_and_saves(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    client = app_client(monkeypatch, signed=signed)
    r = client.post("/onboard/interview/start", data={"description": ""},
                    headers=headers(signed, who, "POST", "/onboard/interview/start"))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    body = _commit_body(sid) | {"owner_email": None}
    r = client.post("/onboard/interview/commit", json=body,
                    headers=headers(signed, who, "POST", "/onboard/interview/commit"))
    assert r.status_code == 200, r.text
    assert setup["profile"].exists()


def test_an_unclaimed_workspace_lets_a_signed_in_person_set_it_up(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No owner on the roster yet (a first run): anyone signed in may."""
    people_store.archive_person(setup["ids"]["principal"])
    people_registry.invalidate()
    client = app_client(monkeypatch, signed=False)
    r = client.post("/onboard/interview/start", data={"description": ""},
                    headers={"x-caller-email": EMAILS["stranger"]})
    assert r.status_code == 200, r.text

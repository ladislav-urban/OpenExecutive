"""Who sees and cancels which scheduled action (api.routes.scheduled._scope).

The principal sees and cancels every action. Anyone else sees and cancels only
the actions their own turns queued (``created_by_person_id``); someone else's
reads as missing (404), on GET and DELETE alike. A caller with no People entry
sees nothing. Driven through the app ``create_app`` builds, in open mode
(behind the shared secret, as the UI proxy calls it) and with signed callers.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openexecutive.api import caller_signing
from openexecutive.memory import episodic
from openexecutive.memory.episodic import get_scheduled_action, insert_scheduled_action
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

VECTORS = json.loads(
    (Path(__file__).parents[1] / "unit" / "caller_assertion_vectors.json").read_text()
)
SECRET = "shared-s3cret"  # gitleaks:allow
OWNER = "olivia@co.example"
TEAMMATE = "ben@co.example"
DEMO = "dana@prospect.example"
STRANGER = "nobody@elsewhere.example"


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    monkeypatch.chdir(tmp_path)
    db = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    monkeypatch.setenv("SCHEDULED_ADMIN_TOKEN", "")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    episodic.initialize_db(db)
    people_store.initialize_db()
    ids = {
        "principal": people_store.upsert_person(
            full_name="Olivia Owner", is_principal=True, email=OWNER
        ),
        "teammate": people_store.upsert_person(full_name="Ben Teammate", email=TEAMMATE),
        "demo": people_store.upsert_person(full_name="Dana Demo", email=DEMO, role="Demo user"),
    }
    people_store.link_directory(ids["demo"], "kc-dana")
    assert people_store.set_directory_demo(ids["demo"], True)
    people_registry.invalidate()

    run_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    def _action(intent: str, owner: int | None) -> int:
        return insert_scheduled_action(
            run_at=run_at, channel="slack_dm", channel_ref=f"U-{intent}",
            intent_text=intent, created_by_person_id=owner,
        )

    actions = {
        "principal": _action("principal-private", ids["principal"]),
        "system": _action("system-row", None),
        "teammate": _action("teammate-own", ids["teammate"]),
        "demo": _action("demo-own", ids["demo"]),
    }
    yield SimpleNamespace(ids=ids, actions=actions)
    people_registry.invalidate()


def _client(monkeypatch: pytest.MonkeyPatch, *, signed: bool) -> TestClient:
    from openexecutive.api.main import create_app

    if signed:
        monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
        monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", VECTORS["public_keys"])
    else:
        # As the UI proxy calls it on a server: the shared secret plus the
        # caller email it stamps (what lets a signed-in user cancel at all).
        monkeypatch.setenv("BACKEND_SHARED_SECRET", SECRET)
        monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    return TestClient(create_app(), client=("203.0.113.5", 50000))


def _headers(signed: bool, email: str | None, method: str, path: str) -> dict[str, str]:
    if signed:
        if email is None:
            return {}
        token = caller_signing.mint_assertion(
            VECTORS["test_signing_key"], kind="user", email=email, method=method, target=path
        )
        return {"x-caller-assertion": str(token), "x-caller-email": email}
    headers = {"x-api-key": SECRET}
    if email is not None:
        headers["x-caller-email"] = email
    return headers


def _call(client: TestClient, signed: bool, email: str | None, method: str, path: str) -> Any:
    return client.request(method, path, headers=_headers(signed, email, method, path))


def _intents(response: Any) -> set[str]:
    assert response.status_code == 200, response.text
    return {row["intent_text"] for row in response.json()}


def _status(action_id: int) -> str:
    row = get_scheduled_action(action_id)
    assert row is not None
    return row.status


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize(("who", "email"), [("teammate", TEAMMATE), ("demo", DEMO)])
def test_a_teammate_sees_and_cancels_only_their_own(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, signed: bool, who: str, email: str
) -> None:
    client = _client(monkeypatch, signed=signed)
    own = install.actions[who]
    assert _intents(_call(client, signed, email, "GET", "/scheduled?status=all")) == {f"{who}-own"}
    for name, action_id in install.actions.items():
        if action_id == own:
            continue
        r = _call(client, signed, email, "GET", f"/scheduled/{action_id}")
        assert r.status_code == 404, (name, r.text)
        r = _call(client, signed, email, "DELETE", f"/scheduled/{action_id}")
        assert r.status_code == 404, (name, r.text)
        assert _status(action_id) == "pending"
    r = _call(client, signed, email, "GET", f"/scheduled/{own}")
    assert r.status_code == 200 and r.json()["intent_text"] == f"{who}-own"
    r = _call(client, signed, email, "DELETE", f"/scheduled/{own}")
    assert r.status_code == 200, r.text
    assert _status(own) == "cancelled"


@pytest.mark.parametrize("signed", [False, True])
def test_a_caller_with_no_people_entry_gets_nothing(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    """An email on nobody's entry (an ALLOWED_EMAILS sign-in), and with signed
    callers on, a call that names no one (a service)."""
    client = _client(monkeypatch, signed=signed)
    callers: list[str | None] = [STRANGER] + ([None] if signed else [])
    for email in callers:
        assert _intents(_call(client, signed, email, "GET", "/scheduled?status=all")) == set()
        for action_id in install.actions.values():
            assert _call(client, signed, email, "GET", f"/scheduled/{action_id}").status_code == 404
            r = _call(client, signed, email, "DELETE", f"/scheduled/{action_id}")
            # A service is stopped by the cancel gate itself; a stranger by scope.
            assert r.status_code in (404, 503), r.text
            assert _status(action_id) == "pending"


@pytest.mark.parametrize("signed", [False, True])
def test_the_principal_sees_and_cancels_everything(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    client = _client(monkeypatch, signed=signed)
    everything = {"principal-private", "system-row", "teammate-own", "demo-own"}
    assert _intents(_call(client, signed, OWNER, "GET", "/scheduled?status=all")) == everything
    for action_id in install.actions.values():
        assert _call(client, signed, OWNER, "GET", f"/scheduled/{action_id}").status_code == 200
        assert _call(client, signed, OWNER, "DELETE", f"/scheduled/{action_id}").status_code == 200
        assert _status(action_id) == "cancelled"


def test_the_list_filters_before_the_limit(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other people's rows never fill a teammate's page."""
    client = _client(monkeypatch, signed=False)
    r = _call(client, False, TEAMMATE, "GET", "/scheduled?status=all&limit=1")
    assert _intents(r) == {"teammate-own"}


def test_open_mode_with_no_caller_is_the_operator(
    install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    from openexecutive.api.main import create_app

    client = TestClient(create_app(), client=("127.0.0.1", 50000))
    assert len(client.get("/scheduled?status=all").json()) == 4
    assert client.delete(f"/scheduled/{install.actions['teammate']}").status_code == 200

"""Shared setup for the principal-only route tests: a roster, the app
``create_app`` builds in open mode or with signed callers, and the headers each
kind of caller sends."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openexecutive.api import caller_signing
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

VECTORS = json.loads(
    (Path(__file__).parents[1] / "unit" / "caller_assertion_vectors.json").read_text()
)
OWNER = "olivia@co.example"
TEAMMATE = "ben@co.example"
DEMO = "dana@prospect.example"
# Signed in (an ALLOWED_EMAILS sign-in) but on nobody's People entry.
STRANGER = "nobody@elsewhere.example"

EMAILS = {"principal": OWNER, "teammate": TEAMMATE, "demo": DEMO, "stranger": STRANGER}
# Everyone refused, and how: signed in as each, or (signed callers on) a call
# that carries no caller at all.
REFUSED = ("teammate", "demo", "stranger", "unsigned")
CASES = [(signed, who) for signed in (False, True) for who in REFUSED
         if signed or who != "unsigned"]


def make_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """A temp DB in a folder of its own (every store, those bound to the
    default path at import too, opens ./episodic_memory.db there) holding
    the principal, a teammate and a demo registrant."""
    monkeypatch.chdir(tmp_path)
    db = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
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
    # A demo registrant: a directory-managed teammate in the demo group.
    people_store.link_directory(ids["demo"], "kc-dana")
    assert people_store.set_directory_demo(ids["demo"], True)
    people_registry.invalidate()
    return ids


def app_client(monkeypatch: pytest.MonkeyPatch, *, signed: bool) -> TestClient:
    """The app as ``create_app`` builds it, without running its startup."""
    from openexecutive.api.main import create_app

    if signed:
        monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", VECTORS["public_keys"])
    else:
        monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    return TestClient(create_app())


def headers(signed: bool, who: str, method: str, path: str) -> dict[str, str]:
    """What ``who`` sends: the caller email the UI proxy stamps, and with
    signed callers on its assertion too. ``unsigned`` sends neither;
    ``operator`` is local login (no email)."""
    if who == "unsigned":
        return {}
    if who == "operator":
        if not signed:
            return {}
        token = caller_signing.mint_assertion(
            VECTORS["test_signing_key"], kind="operator", email="", method=method, target=path
        )
        return {"x-caller-assertion": str(token)}
    email = EMAILS[who]
    if not signed:
        return {"x-caller-email": email}
    token = caller_signing.mint_assertion(
        VECTORS["test_signing_key"], kind="user", email=email, method=method, target=path
    )
    return {"x-caller-assertion": str(token), "x-caller-email": email}


def call(client: TestClient, signed: bool, who: str, method: str, path: str,
         body: Any = None) -> Any:
    return client.request(method, path, json=body, headers=headers(signed, who, method, path))

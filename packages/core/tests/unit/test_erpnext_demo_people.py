"""The demo admin console's two routes (integrations/erpnext_demo_people.py).

Signed by the ERPNext site for the principal, with each route's own audience:
the list of demo people and their questions today, and archiving a demo,
directory-managed person (never anyone else).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.caller import JtiLedger
from openexecutive.integrations import erpnext_demo_people as dp
from openexecutive.people import demo, registry
from openexecutive.people import store as people_store

OWNER = "pat@example.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class _Site:
    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_keys = f"k1:{_b64url(public)}"

    def sign(self, body: bytes, *, aud: str, sub: str = OWNER) -> str:
        now = int(time.time())
        claims = {
            "aud": aud,
            "body": hashlib.sha256(body).hexdigest(),
            "exp": now + 60,
            "iat": now,
            "jti": secrets.token_urlsafe(18),
            "kid": "k1",
            "sub": sub,
        }
        signing_input = "v1." + _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        return f"{signing_input}.{_b64url(self.key.sign(signing_input.encode('ascii')))}"


def _mark(person_id: int, **columns: int) -> None:
    with people_store._get_conn() as conn:
        for column, value in columns.items():
            conn.execute(f"UPDATE people SET {column} = ? WHERE id = ?", (value, person_id))  # noqa: S608 — test columns


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_store.initialize_db()
    registry.invalidate()
    signer = _Site()
    monkeypatch.setenv("ERPNEXT_PANEL_PUBLIC_KEYS", signer.public_keys)
    monkeypatch.setattr(dp, "_ledger", JtiLedger())
    audited: list[str] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda event, *_a, **_k: audited.append(event))
    principal = people_store.upsert_person(full_name="Pat Owner", is_principal=True, email=OWNER)
    vic = people_store.upsert_person(full_name="Vic Visitor", email="vic@example.com")
    _mark(vic, directory_managed=1, directory_demo=1)
    sue = people_store.upsert_person(full_name="Sue Suspended", email="sue@example.com")
    _mark(sue, directory_managed=1, directory_demo=1, suspended=1)
    tom = people_store.upsert_person(full_name="Tom Teammate", email="tom@example.com")
    _mark(tom, directory_managed=1)
    app = FastAPI()
    app.include_router(dp.router)
    yield SimpleNamespace(
        client=TestClient(app), signer=signer, audited=audited, principal=principal, vic=vic, sue=sue, tom=tom
    )


def _post(site: SimpleNamespace, path: str, payload: Any, *, aud: str | None = None, sub: str = OWNER) -> Any:
    body = json.dumps(payload).encode()
    audience = aud or (dp.AUDIENCE if path == dp.PATH else dp.ARCHIVE_AUDIENCE)
    headers = {"content-type": "application/json", dp.ASSERTION_HEADER: site.signer.sign(body, aud=audience, sub=sub)}
    return site.client.post(path, content=body, headers=headers)


def test_lists_demo_people_with_todays_questions(site: SimpleNamespace) -> None:
    from openexecutive.config import get_settings

    for _ in range(3):
        assert demo.take_question(site.vic, 200)
    resp = _post(site, dp.PATH, {})
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == get_settings().demo_daily_questions
    assert data["people"] == [
        {"email": "vic@example.com", "name": "Vic Visitor", "questions_today": 3, "suspended": False},
        {"email": "sue@example.com", "name": "Sue Suspended", "questions_today": 0, "suspended": True},
    ]


def test_archives_a_demo_person_once(site: SimpleNamespace) -> None:
    resp = _post(site, dp.ARCHIVE_PATH, {"email": "VIC@example.com"})
    assert (resp.status_code, resp.json()) == (200, {"archived": True})
    person = people_store.get_person(site.vic)
    assert person is not None and person.archived
    assert site.audited == ["demo_person_archived"]
    again = _post(site, dp.ARCHIVE_PATH, {"email": "vic@example.com"})
    assert (again.status_code, again.json()) == (200, {"archived": False, "reason": "already"})
    # A suspended demo person is archived too.
    assert _post(site, dp.ARCHIVE_PATH, {"email": "sue@example.com"}).json() == {"archived": True}


def test_nobody_with_the_address_is_a_bare_404(site: SimpleNamespace) -> None:
    resp = _post(site, dp.ARCHIVE_PATH, {"email": "ghost@example.com"})
    assert resp.status_code == 404 and resp.content == b""


@pytest.mark.parametrize("who", ["principal", "tom", "not_managed"])
def test_never_archives_anyone_but_a_managed_demo_person(site: SimpleNamespace, who: str) -> None:
    if who == "not_managed":
        pid = people_store.upsert_person(full_name="Hand Added", email="hand@example.com")
        _mark(pid, directory_demo=1)
        email = "hand@example.com"
    else:
        pid = getattr(site, who)
        email = OWNER if who == "principal" else "tom@example.com"
    resp = _post(site, dp.ARCHIVE_PATH, {"email": email})
    assert resp.status_code == 409
    person = people_store.get_person(pid)
    assert person is not None and not person.archived
    assert site.audited == []


@pytest.mark.parametrize("path", [dp.PATH, dp.ARCHIVE_PATH])
def test_only_the_principal_may_ask(site: SimpleNamespace, path: str) -> None:
    payload = {} if path == dp.PATH else {"email": "vic@example.com"}
    resp = _post(site, path, payload, sub="tom@example.com")
    assert resp.status_code == 403
    person = people_store.get_person(site.vic)
    assert person is not None and not person.archived


def test_each_route_takes_only_its_own_audience(site: SimpleNamespace) -> None:
    # The list's assertion, the chat's or the events' can't archive anyone.
    for aud in (dp.AUDIENCE, "openexecutive-erpnext-panel", "openexecutive-erpnext-events"):
        resp = _post(site, dp.ARCHIVE_PATH, {"email": "vic@example.com"}, aud=aud)
        assert resp.status_code == 401
    assert _post(site, dp.PATH, {}, aud=dp.ARCHIVE_AUDIENCE).status_code == 401


def test_an_unsigned_or_replayed_request_is_refused(site: SimpleNamespace) -> None:
    body = json.dumps({"email": "vic@example.com"}).encode()
    assert site.client.post(dp.ARCHIVE_PATH, content=body).status_code == 401
    token = site.signer.sign(body, aud=dp.ARCHIVE_AUDIENCE)
    first = site.client.post(dp.ARCHIVE_PATH, content=body, headers={dp.ASSERTION_HEADER: token})
    assert first.status_code == 200
    replay = site.client.post(dp.ARCHIVE_PATH, content=body, headers={dp.ASSERTION_HEADER: token})
    assert replay.status_code == 401


def test_without_keys_it_is_not_set_up(site: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ERPNEXT_PANEL_PUBLIC_KEYS")
    assert _post(site, dp.PATH, {}).status_code == 503


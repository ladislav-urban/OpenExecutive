"""ERPNext phase 3: alerts ERPNext raises (integrations/erpnext_events.py)."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.caller import JtiLedger
from openexecutive.integrations import erpnext_events
from openexecutive.integrations.erpnext_events import ASSERTION_HEADER, PATH, parse_events

OVERDUE = {
    "kind": "overdue_receivable",
    "key": "ACC-SINV-2026-00012",
    "title": "ACC-SINV-2026-00012 is 75 days overdue",
    "text": "Acme Ltd owes 4,100.00 EUR on ACC-SINV-2026-00012, due 2026-07-30 (Raveno Electric).",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class _Site:
    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_keys = f"k1:{_b64url(public)}"

    def sign(self, body: bytes, *, aud: str = "openexecutive-erpnext-events", sub: str = "") -> str:
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


def test_parse_keeps_known_well_formed_events_only() -> None:
    events = parse_events({
        "events": [
            OVERDUE,
            {**OVERDUE, "kind": "delete_everything"},
            {**OVERDUE, "key": "a\nb"},
            {**OVERDUE, "title": ""},
            "not an event",
            {**OVERDUE, "text": "x" * 5000, "key": "SINV-2"},
            {**OVERDUE, "kind": "stock_below_reorder", "key": "ITEM-1@Stores - R"},
        ]
    })
    assert [e["key"] for e in events] == ["ACC-SINV-2026-00012", "SINV-2", "ITEM-1@Stores - R"]
    assert len(events[1]["text"]) == 1500
    # oe_connector's phase 3c rules (budget variance, weekly margin) and its
    # stalled-repost rule, keyed company:oldest repost.
    assert [e["kind"] for e in parse_events({"events": [
        {**OVERDUE, "kind": "budget_exceeded", "key": "Acme:Marketing:2026"},
        {**OVERDUE, "kind": "weekly_margin_drop", "key": "Acme:2026-W41"},
        {**OVERDUE, "kind": "stalled_repost", "key": "Acme Ltd:REPOST-ITEM-VAL-00012"},
    ]})] == ["budget_exceeded", "weekly_margin_drop", "stalled_repost"]
    with pytest.raises(ValueError):
        parse_events({"events": [OVERDUE] * 21})
    with pytest.raises(ValueError):
        parse_events([OVERDUE])


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    signer = _Site()
    monkeypatch.setenv("ERPNEXT_PANEL_PUBLIC_KEYS", signer.public_keys)
    monkeypatch.setattr(erpnext_events, "_ledger", JtiLedger())
    monkeypatch.setattr(erpnext_events, "_recent", erpnext_events.deque())
    scheduled: list[Any] = []
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", scheduled.append)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *_a, **_k: None)
    app = FastAPI()
    app.include_router(erpnext_events.router)
    yield SimpleNamespace(client=TestClient(app), signer=signer, scheduled=scheduled)


def _post(site: SimpleNamespace, payload: Any, **sign: Any) -> Any:
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json", ASSERTION_HEADER: site.signer.sign(body, **sign)}
    return site.client.post(PATH, content=body, headers=headers)


def test_an_event_becomes_a_private_alert_for_the_principal(site: SimpleNamespace) -> None:
    reply = _post(site, {"events": [OVERDUE]})
    assert reply.status_code == 200 and reply.json() == {"accepted": 1}
    (event,) = site.scheduled
    assert event.source == "erpnext"
    assert event.private is True
    assert event.dedup_hint == "erpnext:overdue_receivable:ACC-SINV-2026-00012"
    # One a month per document, so it can raise again after a dismissal.
    assert event.external_id == f"erpnext:overdue_receivable:ACC-SINV-2026-00012:{time.strftime('%Y-%m')}"
    assert event.subject == OVERDUE["title"]
    # Customers name themselves: their words reach triage as untrusted.
    assert '<untrusted_content source="erpnext" author="erpnext_alert"' in event.body
    assert "Acme Ltd owes 4,100.00 EUR" in event.body


def test_only_the_sites_events_assertion_is_accepted(site: SimpleNamespace) -> None:
    # A panel assertion names a user and is for the chat route.
    assert _post(site, {"events": [OVERDUE]}, aud="openexecutive-erpnext-panel", sub="ana@example.com").status_code == 401
    assert _post(site, {"events": [OVERDUE]}, sub="ana@example.com").status_code == 401
    body = json.dumps({"events": [OVERDUE]}).encode()
    unsigned = site.client.post(PATH, content=body, headers={"content-type": "application/json"})
    assert unsigned.status_code == 401
    assert site.scheduled == []


def test_without_the_sites_key_events_are_off(site: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ERPNEXT_PANEL_PUBLIC_KEYS")
    assert _post(site, {"events": [OVERDUE]}).status_code == 503
    assert site.scheduled == []


def test_events_are_capped_per_minute_to_leave_triage_room(site: SimpleNamespace) -> None:
    # The pipeline triages 60 a minute from every source; ERPNext gets 20,
    # and a 429 rather than a silent drop.
    batch = [{**OVERDUE, "key": f"SINV-{i}"} for i in range(15)]
    assert _post(site, {"events": batch}).status_code == 200
    assert _post(site, {"events": batch}).status_code == 429
    assert len(site.scheduled) == 15


def test_events_are_capped_per_hour(site: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erpnext_events, "EVENTS_PER_HOUR", 3)
    batch = [{**OVERDUE, "key": f"SINV-{i}"} for i in range(2)]
    assert _post(site, {"events": batch}).status_code == 200
    assert _post(site, {"events": batch}).status_code == 429
    assert len(site.scheduled) == 2

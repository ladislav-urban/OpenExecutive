"""Every ERPNext alert kind is the principal's alone, down to the stored row.

oe_connector computes its events as its read-only service user, so
``integrations/erpnext_events.py`` marks each one private and the alerts
pipeline (``pipeline._make_private``) stores it tagged ``private:principal``,
routed to the principal, deduplicated as ``private:erpnext:<kind>:<key>`` and
dispatched to PERSISTED only. Each kind here goes through the signed route,
``parse_events`` and ``_schedule``, then ``evaluate_and_dispatch`` with
triage stubbed, and the row it stores is checked.

A kind added to ``KINDS`` without a payload in ``PAYLOADS`` fails
``test_every_kind_has_a_payload``.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import dispatcher, pipeline
from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import (
    PRIVATE_ALERT_TAG,
    AlertChannel,
    AlertSeverity,
    TriageDecision,
)
from openexecutive.api.caller import JtiLedger
from openexecutive.integrations import erpnext_events
from openexecutive.integrations.erpnext_events import ASSERTION_HEADER, PATH

from .test_erpnext_events import _Site

# One minimal valid event per kind, keyed as oe_connector's alerts.py keys
# them. parse_events skips a kind not in KINDS, so a kind added to KINDS needs
# a payload here (test_every_kind_has_a_payload holds the two in step).
PAYLOADS: dict[str, dict[str, Any]] = {
    "overdue_receivable": {
        "key": "ACC-SINV-2026-00012",
        "title": "ACC-SINV-2026-00012 is 75 days overdue",
        "text": "Acme Ltd owes 4,100.00 EUR on ACC-SINV-2026-00012.",
    },
    "stock_below_reorder": {
        "key": "BRK-16@Stores - AC",
        "title": "BRK-16 is below its reorder level",
        "text": "12 on hand at Stores - AC, reorder level 40.",
    },
    "large_purchase_invoice": {
        "key": "ACC-PINV-2026-00007",
        "title": "ACC-PINV-2026-00007 is far above the supplier's usual size",
        "text": "Bolt Supply invoiced 48,000.00 EUR, usually about 6,000.00 EUR.",
    },
    "budget_exceeded": {
        "key": "Acme:Marketing:2026",
        "title": "Marketing is over its 2026 budget",
        "text": "Actual 132,000.00 EUR against a budget of 120,000.00 EUR.",
    },
    "weekly_margin_drop": {
        "key": "Acme:2026-W41",
        "title": "Gross margin fell last week",
        "text": "18% in 2026-W41, against 27% the four weeks before.",
    },
    "stalled_repost": {
        "key": "Acme Ltd:REPOST-ITEM-VAL-00012",
        "title": "Stock reposts have stopped moving",
        "text": "REPOST-ITEM-VAL-00012 failed; stock values are not up to date.",
    },
    "credit_hold": {
        "key": "Acme:SO-0001",
        "title": "SO-0001 is over its customer's credit limit",
        "text": "Acme's open balance plus SO-0001 is 2,000.00 EUR over the limit.",
        "for_users": ["sara@example.com"],
    },
    "overnight_postings": {
        "key": "overnight:2026-11-23",
        "title": "Overnight: 1 failed, 1 to check",
        "text": "ERPNext's scheduled jobs ran overnight; one failed and one result looks wrong.",
    },
}


def test_every_kind_has_a_payload() -> None:
    assert set(PAYLOADS) == set(erpnext_events.KINDS)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from openexecutive.agents import triage as triage_module
    from openexecutive.memory import episodic
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    db = tmp_path / "kinds.db"
    for module in (alert_store, people_store, episodic):
        monkeypatch.setattr(module, "DB_PATH", db)
    alert_store.initialize_db(db)
    people_store.initialize_db(db)
    principal = people_store.upsert_person(full_name="Pat Principal", is_principal=True, email="pat@example.com")
    sara = people_store.upsert_person(full_name="Sara Sales", email="sara@example.com")
    people_registry.invalidate()

    signer = _Site()
    monkeypatch.setenv("ERPNEXT_PANEL_PUBLIC_KEYS", signer.public_keys)
    monkeypatch.setattr(erpnext_events, "_ledger", JtiLedger())
    monkeypatch.setattr(erpnext_events, "_recent", deque())
    monkeypatch.setattr(pipeline, "_recent_event_ts", deque(maxlen=pipeline._RATE_LIMIT_PER_MIN))
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *_a, **_k: None)
    scheduled: list[Any] = []
    monkeypatch.setattr(pipeline, "schedule_evaluation", scheduled.append)

    # Triage asks for every channel it can; privacy must cut it to PERSISTED.
    async def fake_triage(self: Any, event: Any, **_: Any) -> TriageDecision:
        return TriageDecision(
            alert=True, severity=AlertSeverity.URGENT,
            channels=[AlertChannel.PERSISTED, AlertChannel.WEB, AlertChannel.EMAIL, AlertChannel.COMPANY_BROADCAST],
            headline=event.subject, body=event.subject, topic_tags=["finance"], dedup_key="model-key",
        )

    monkeypatch.setattr(triage_module.TriageAgent, "triage", fake_triage)
    dispatched: list[list[AlertChannel]] = []

    async def _dispatch(alert: Any, channels: Any, **_: Any) -> None:
        dispatched.append(list(channels))

    async def _push(alert: Any, ids: list[int]) -> int:
        return len(ids)

    monkeypatch.setattr(dispatcher, "dispatch_all", _dispatch)
    monkeypatch.setattr(dispatcher, "push_to_named", _push)
    app = FastAPI()
    app.include_router(erpnext_events.router)
    return SimpleNamespace(
        db=db, principal=principal, sara=sara, client=TestClient(app), signer=signer,
        scheduled=scheduled, dispatched=dispatched,
    )


@pytest.mark.parametrize("kind", sorted(erpnext_events.KINDS))
def test_every_kind_is_stored_private_to_the_principal(kind: str, world: SimpleNamespace) -> None:
    payload = {"events": [{"kind": kind, **PAYLOADS[kind]}]}
    body = json.dumps(payload).encode()
    reply = world.client.post(
        PATH, content=body,
        headers={"content-type": "application/json", ASSERTION_HEADER: world.signer.sign(body)},
    )
    assert reply.status_code == 200 and reply.json() == {"accepted": 1}
    (event,) = world.scheduled

    _, alert_id = asyncio.run(pipeline.evaluate_and_dispatch(event, db_path=world.db))
    assert alert_id is not None
    alert = alert_store.get_alert(alert_id, db_path=world.db)
    assert alert is not None
    assert alert.source == "erpnext"
    assert PRIVATE_ALERT_TAG in alert.topic_tags
    assert alert.routed_to_person_id == world.principal
    assert alert.dedup_key.startswith(f"private:erpnext:{kind}:{PAYLOADS[kind]['key']}")
    # Stored for the principal's board only: no live push, email or broadcast.
    assert world.dispatched == [[AlertChannel.PERSISTED]]
    named = [t for t in alert.topic_tags if t.startswith("named:")]
    if kind in erpnext_events.NAMED_KINDS:
        assert named == [f"named:{world.sara}"]
    else:
        assert named == []

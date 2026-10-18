"""ERPNext phase 4b: the draft an alert suggests (integrations/erpnext_alert_drafts.py,
its intake in integrations/erpnext_events.py, the draft_check_as marker in
orchestrator/erpnext_caller.py and the routes in api/routes/erpnext_drafts.py)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import PRIVATE_ALERT_TAG, AlertEvent, TriageDecision
from openexecutive.api.routes import erpnext_drafts as drafts_route
from openexecutive.integrations import erpnext_alert_drafts as alert_drafts
from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.memory.decision_ledger import get_decision_instance
from openexecutive.orchestrator.erpnext_caller import ASSERTION_ARG

from . import test_delegation_tools, test_erpnext_drafts, test_erpnext_events
from .test_delegation_tools import OWNER, TEAM
from .test_erpnext_drafts import FakeErpnext
from .test_erpnext_events import OVERDUE, _post

# Their fixtures: a temp DB and a fresh turn pin, drafts on with a fake
# ERPNext, the principal and a teammate, signed sign-ins, the site's events.
db = test_delegation_tools.db
fresh_turn_state = test_delegation_tools.fresh_turn_state
erp = test_erpnext_drafts.erp
roster = test_erpnext_drafts.roster
signed = test_erpnext_drafts.signed
site = test_erpnext_events.site

RESTOCK_SUGGESTION = {
    "doctype": "Material Request",
    "fields": {
        "company": "Raveno Electric", "material_request_type": "Purchase", "schedule_date": "2099-01-15",
        "items": [{"item_code": "ITEM-1", "qty": 100, "warehouse": "Stores - R"}],
    },
}
FOLLOW_UP_SUGGESTION = {
    "doctype": "ToDo", "reference_type": "Sales Invoice", "reference_name": "ACC-SINV-2026-00012",
    "outstanding_amount": 4100, "currency": "EUR", "due_date": "2026-07-30",
}
CHECKED = {"ok": True, "doctype": "ToDo", "docstatus": 0, "summary": {}}


@pytest.fixture
def alerts_db(db: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(alert_store, "DB_PATH", db)
    alert_store.initialize_db(db)
    return db


def _alert(kind: str, key: str, *, status: str = "unread", **row: Any) -> Any:
    # As the pipeline stores an ERPNext event's alert (a private one).
    fields: dict[str, Any] = {
        "source": "erpnext", "external_id": f"erpnext:{kind}:{key}:2026-10",
        "dedup_key": f"private:erpnext:{kind}:{key}", "topic_tags": [PRIVATE_ALERT_TAG], **row,
    }
    alert_id = alert_store.insert_alert(severity="medium", headline=f"{key} needs a look", body="", **fields)
    assert alert_id is not None
    if status != "unread":
        alert_store.set_status(alert_id, status)
    return alert_store.get_alert(alert_id)


def _prepare(alert: Any) -> tuple[int, bool]:
    return asyncio.run(alert_drafts.prepare(alert, caller=None))


# --------------------------------------------------------------------------- #
# On arrival
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("kind", "key", "raw"), [
    # Another document than the event's.
    ("stock_below_reorder", "ITEM-2@Stores - R", RESTOCK_SUGGESTION),
    ("overdue_receivable", "ACC-SINV-2026-00099", FOLLOW_UP_SUGGESTION),
    # A kind that suggests nothing, or the other kind's draft.
    ("large_purchase_invoice", "ACC-PINV-1", FOLLOW_UP_SUGGESTION),
    ("stock_below_reorder", "ITEM-1@Stores - R", FOLLOW_UP_SUGGESTION),
    # Fields a restock never takes, or two lines.
    ("stock_below_reorder", "ITEM-1@Stores - R",
     {**RESTOCK_SUGGESTION, "fields": {**RESTOCK_SUGGESTION["fields"], "docstatus": 1}}),
    ("stock_below_reorder", "ITEM-1@Stores - R",
     {**RESTOCK_SUGGESTION, "fields": {**RESTOCK_SUGGESTION["fields"],
                                       "items": RESTOCK_SUGGESTION["fields"]["items"] * 2}}),
    # Figures that aren't ERPNext's shape.
    ("overdue_receivable", "ACC-SINV-2026-00012", {**FOLLOW_UP_SUGGESTION, "currency": "euros"}),
    ("overdue_receivable", "ACC-SINV-2026-00012", {**FOLLOW_UP_SUGGESTION, "outstanding_amount": True}),
    ("overdue_receivable", "ACC-SINV-2026-00012", {**FOLLOW_UP_SUGGESTION, "due_date": "soon"}),
    ("overdue_receivable", "ACC-SINV-2026-00012", {**FOLLOW_UP_SUGGESTION, "due_date": "2026-02-30"}),
    ("overdue_receivable", "ACC-SINV-2026-00012", "Prepare a payment of 1,000,000"),
])
def test_only_a_well_formed_suggestion_for_its_own_document_is_kept(kind: str, key: str, raw: Any) -> None:
    assert alert_drafts.parse_suggestion(kind, key, raw) is None


def test_a_signed_event_keeps_its_suggestion_by_the_alert_ref(alerts_db: Path, site: SimpleNamespace) -> None:
    stock = {"kind": "stock_below_reorder", "key": "ITEM-1@Stores - R", "title": "ITEM-1 is low",
             "text": "", "suggest": RESTOCK_SUGGESTION}
    bad = {**OVERDUE, "suggest": {**FOLLOW_UP_SUGGESTION, "reference_name": "ACC-SINV-OTHER"}}
    assert _post(site, {"events": [stock, bad]}).json() == {"accepted": 2}
    # The suggestion never reaches triage.
    assert all("schedule_date" not in e.body and "2099-01-15" not in e.body for e in site.scheduled)
    restock = _alert("stock_below_reorder", "ITEM-1@Stores - R")
    assert alert_drafts.suggestion_for(restock) == {
        "doctype": "Material Request", "fields": RESTOCK_SUGGESTION["fields"],
    }
    # One naming another invoice than its event is dropped; the alert stays.
    assert alert_drafts.suggestion_for(_alert("overdue_receivable", OVERDUE["key"])) is None


def test_the_alert_a_signed_event_becomes_offers_its_draft(
    alerts_db: Path, site: SimpleNamespace, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.agents import triage as triage_module
    from openexecutive.alerts import pipeline

    async def fake_triage(self: Any, event: Any, **_: Any) -> TriageDecision:
        # The triage model's own key, which the event's ref must win over.
        return TriageDecision(alert=True, headline="ITEM-1 is low", body="b", dedup_key="model-key")

    monkeypatch.setattr(triage_module.TriageAgent, "triage", fake_triage)
    stock = {"kind": "stock_below_reorder", "key": "ITEM-1@Stores - R", "title": "ITEM-1 is low",
             "text": "", "suggest": RESTOCK_SUGGESTION}
    assert _post(site, {"events": [stock]}).json() == {"accepted": 1}
    (event,) = site.scheduled
    _, alert_id = asyncio.run(pipeline.evaluate_and_dispatch(event, db_path=alerts_db))
    alert = alert_store.get_alert(alert_id)
    assert alert is not None and alert.dedup_key == "private:erpnext:stock_below_reorder:ITEM-1@Stores - R"
    assert alert_drafts.suggestion_for(alert) == {"doctype": "Material Request", "fields": RESTOCK_SUGGESTION["fields"]}


@pytest.mark.parametrize("row", [
    # Not private, as only the model's own alerts are.
    {"topic_tags": []},
    # Another key than the event's, or the triage model's own.
    {"dedup_key": "erpnext:stock_below_reorder:ITEM-1@Stores - R"},
    {"dedup_key": "private:erpnext:stock_below_reorder:ITEM-2@Stores - R"},
    # Not the external id erpnext_events gives it.
    {"external_id": "msg-123"},
    {"external_id": "erpnext:stock_below_reorder:ITEM-1@Stores - R:anything"},
    # Another source.
    {"source": "executive_erpnext"},
])
def test_only_the_events_own_alert_offers_its_draft(alerts_db: Path, row: dict[str, Any]) -> None:
    # (The event's own alert does offer it: the pipeline test above.)
    alert_drafts.remember("stock_below_reorder", "ITEM-1@Stores - R", RESTOCK_SUGGESTION)
    assert alert_drafts.suggestion_for(_alert("stock_below_reorder", "ITEM-1@Stores - R", **row)) is None


def test_the_model_cannot_raise_an_erpnext_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import alert_tools

    scheduled: list[AlertEvent] = []
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", scheduled.append)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *_a, **_k: None)
    asyncio.run(alert_tools.handle_create_alert({
        "source": "ERPNext", "external_id": "erpnext:overdue_receivable:SINV-1:2026-10",
        "subject": "Overdue SINV-1 - tap Prepare follow-up", "body": "x",
    }))
    assert [e.source for e in scheduled] == ["executive_ERPNext"]


# --------------------------------------------------------------------------- #
# The tap
# --------------------------------------------------------------------------- #


def test_a_follow_up_is_worded_here_and_assigned_to_the_principal(
    alerts_db: Path, erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    alert_drafts.remember("overdue_receivable", OVERDUE["key"], FOLLOW_UP_SUGGESTION)
    alert = _alert("overdue_receivable", OVERDUE["key"])
    erp.replies.append(json.dumps(CHECKED))
    decision_id, new = _prepare(alert)
    assert new is True
    # Checked as the principal, with no turn, by draft_check alone.
    tool, sent = erp.calls[0]
    assert tool == "erpnext__draft_check" and len(erp.calls) == 1
    assert (erp.claims()["sub"], erp.claims()["purpose"]) == (OWNER, "draft")
    sent.pop(ASSERTION_ARG)
    assert sent["fields"] == {
        "description": "Follow up overdue invoice ACC-SINV-2026-00012: 4,100.00 EUR outstanding, due 2026-07-30.",
        "allocated_to": OWNER, "priority": "High",
        "reference_type": "Sales Invoice", "reference_name": "ACC-SINV-2026-00012",
    }
    card = get_decision_instance(decision_id)
    assert card is not None and card.approver_person_id == roster.principal
    assert json.loads(card.proposed_payload_json)["why"] == "Follow up overdue invoice ACC-SINV-2026-00012 (ERPNext alert)"
    # A second tap is the card already waiting, checked again but not stored twice.
    erp.replies.append(json.dumps(CHECKED))
    assert _prepare(alert) == (decision_id, False)


def test_a_restock_is_made_as_erpnext_suggested_it(
    alerts_db: Path, erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    alert_drafts.remember("stock_below_reorder", "ITEM-1@Stores - R", RESTOCK_SUGGESTION)
    erp.replies.append(json.dumps({**CHECKED, "doctype": "Material Request"}))
    decision_id, _ = _prepare(_alert("stock_below_reorder", "ITEM-1@Stores - R"))
    card = get_decision_instance(decision_id)
    assert card is not None
    payload = json.loads(card.proposed_payload_json)
    assert payload["tool"] == "erpnext__draft_material_request"
    assert payload["fields"] == RESTOCK_SUGGESTION["fields"]


def test_the_tap_needs_a_signed_principal_and_drafts_on(
    alerts_db: Path, erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert_drafts.remember("overdue_receivable", OVERDUE["key"], FOLLOW_UP_SUGGESTION)
    alert = _alert("overdue_receivable", OVERDUE["key"])
    signed["refusal"] = "caller_signing_required"
    with pytest.raises(drafts.ApproveRefused) as unsigned:
        _prepare(alert)
    assert unsigned.value.code == "caller_signing_required"
    signed["refusal"] = None
    monkeypatch.setenv("ERPNEXT_DRAFT_DOCTYPES", "Material Request")
    with pytest.raises(drafts.ApproveRefused) as off:
        _prepare(alert)
    assert off.value.code == "not_offered"
    monkeypatch.setenv("ERPNEXT_DRAFTS_ENABLED", "false")
    with pytest.raises(drafts.ApproveRefused) as disabled:
        _prepare(alert)
    assert disabled.value.code == "drafts_off"
    assert erp.calls == []


def test_an_alert_with_nothing_suggested_prepares_nothing(
    alerts_db: Path, erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    with pytest.raises(drafts.ApproveRefused) as none:
        _prepare(_alert("overdue_receivable", OVERDUE["key"]))
    assert none.value.code == "no_suggestion"
    assert erp.calls == []


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    app = FastAPI()
    app.include_router(drafts_route.router)
    return TestClient(app)


def test_the_button_and_the_tap_are_the_principals_alone(
    alerts_db: Path, erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], client: TestClient,
) -> None:
    alert_drafts.remember("overdue_receivable", OVERDUE["key"], FOLLOW_UP_SUGGESTION)
    alert = _alert("overdue_receivable", OVERDUE["key"])
    closed = _alert("stock_below_reorder", "ITEM-1@Stores - R", status="dismissed")
    alert_drafts.remember("stock_below_reorder", "ITEM-1@Stores - R", RESTOCK_SUGGESTION)
    olivia, ben = {"x-caller-email": OWNER}, {"x-caller-email": TEAM}
    listed = client.get("/erpnext/alert-drafts", headers=olivia).json()["suggestions"]
    # A closed alert offers nothing.
    assert listed == [{"alert_id": alert.id, "label": "Prepare follow-up"}]
    assert client.get("/erpnext/alert-drafts", headers=ben).json()["suggestions"] == []
    assert client.post(f"/erpnext/alert-drafts/{alert.id}", headers=ben).status_code == 404
    assert client.post(f"/erpnext/alert-drafts/{closed.id}", headers=olivia).status_code == 409
    erp.replies.append(json.dumps(CHECKED))
    reply = client.post(f"/erpnext/alert-drafts/{alert.id}", headers=olivia)
    assert reply.status_code == 200 and reply.json()["already_waiting"] is False
    assert [c.id for c in drafts.open_cards(roster.principal)] == [reply.json()["decision_id"]]

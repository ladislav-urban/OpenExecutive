"""ERPNext phase 4a: drafts a person approves (integrations/erpnext_drafts.py,
the gateway's draft gate in orchestrator/mcp_gateway.py, the draft markers
and ``purpose`` claim in orchestrator/erpnext_caller.py, and the
``erpnext_draft`` class in api/routes/decisions.py)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import decisions as decisions_route
from openexecutive.api.routes import erpnext_drafts as drafts_route
from openexecutive.delegation import lockdown
from openexecutive.delegation.settings import pin_turn_delegation
from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.memory.decision_ledger import (
    STATUS_APPROVED_UNCHANGED,
    STATUS_FAILED,
    STATUS_PROPOSED,
    get_decision_instance,
)
from openexecutive.orchestrator import erpnext_caller, mcp_gateway
from openexecutive.orchestrator.action_chips import summarize_action
from openexecutive.orchestrator.erpnext_caller import ASSERTION_ARG, ErpnextCaller
from openexecutive.orchestrator.mcp_gateway import MCPGateway
from openexecutive.orchestrator.schedule_tools import (
    PRIVATE_TURN_WITHHELD_TOOLS,
    UNATTENDED_WITHHELD_TOOLS,
    erpnext_panel_withholds,
    set_session,
)
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.prompts.connected_systems import render_connected_systems

# A temp DB and a fresh turn pin for every test.
from .test_delegation_tools import OWNER, TEAM, db, fresh_turn_state  # noqa: F401
from .test_erpnext_caller import VECTOR_PRIVATE_KEY, _claims

FOLLOW_UP = {"description": "Chase ACC-SINV-2026-00042, 30 days overdue", "allocated_to": "ana@co.example",
        "priority": "High", "reference_type": "Sales Invoice", "reference_name": "ACC-SINV-2026-00042"}
RESTOCK = {"company": "Acme", "schedule_date": "2099-01-15",
           "items": [{"item_code": "ITEM-1", "qty": 40, "warehouse": "Stores - AC"}]}
CHECKED_RESTOCK = {
    "ok": True, "doctype": "Material Request", "docstatus": 0,
    "summary": {
        "company": "Acme", "currency": "EUR", "material_request_type": "Purchase", "schedule_date": "2099-01-15",
        "items": [{"item_code": "ITEM-1", "qty": 40, "warehouse": "Stores - AC"}],
        "estimated_value": 1234.5, "unpriced_items": [],
    },
}
CREATED = {"ok": True, "doctype": "Material Request", "name": "MAT-MR-2026-00012", "docstatus": 0,
           "url": "https://erp.example/app/material-request/MAT-MR-2026-00012", "already_created": False}


def _reply(text: str) -> MagicMock:
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    return r


class FakeErpnext:
    """The gateway's MCP session, standing in for extensible-mcp + oe_connector."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.session = MagicMock()
        self.session.call_tool = AsyncMock(side_effect=self._call)

    async def _call(self, _name: str, body: dict[str, Any]) -> MagicMock:
        self.calls.append((body["tool_name"], dict(body["arguments"])))
        return _reply(self.replies.pop(0) if self.replies else '{"ok": true}')

    def claims(self, index: int = -1) -> dict[str, Any]:
        return _claims(self.calls[index][1][ASSERTION_ARG])


@pytest.fixture
def erp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeErpnext]:
    """Drafts on: per-person access, ERPNEXT_DRAFTS_ENABLED, and a running
    gateway with the erpnext server."""
    monkeypatch.setenv("ERPNEXT_CALLER_PRIVATE_KEY", VECTOR_PRIVATE_KEY)
    monkeypatch.setenv("ERPNEXT_DRAFTS_ENABLED", "true")
    fake = FakeErpnext()
    gateway = MCPGateway()
    gateway._session = fake.session
    gateway.server_names = ("erpnext",)
    monkeypatch.setattr(mcp_gateway, "_active_gateway", gateway)
    yield fake


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    teammate = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAM)
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate)


@pytest.fixture
def signed(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    state: dict[str, str | None] = {"refusal": None}
    monkeypatch.setattr("openexecutive.delegation.verified.caller_refusal", lambda caller, email: state["refusal"])
    return state


def _owner_turn(roster: SimpleNamespace, text: str = "order 40 more ITEM-1 for Stores") -> Session:
    session = Session(from_web_chat=True, caller_person_id=roster.principal)
    pin_turn_delegation(session, text)
    return session


def _propose(session: Session | None, tool_input: dict[str, Any]) -> dict[str, Any]:
    async def go() -> str:
        with set_session(session):
            return await drafts.handle_propose(tool_input)

    return json.loads(asyncio.run(go()))


def _restock_card(erp: FakeErpnext, roster: SimpleNamespace) -> Any:
    erp.replies.append(json.dumps(CHECKED_RESTOCK))
    result = _propose(_owner_turn(roster), {"why": "Restock ITEM-1", "doctype": "Material Request", "fields": RESTOCK})
    card = get_decision_instance(result["decision_id"])
    assert card is not None
    return card


def _approve(card: Any) -> dict[str, Any]:
    return asyncio.run(drafts.approve(card, caller=None, resolver=card.approver_person_id))


# --------------------------------------------------------------------------- #
# Signing and the gateway gate
# --------------------------------------------------------------------------- #


def test_a_draft_assertion_carries_the_purpose_claim() -> None:
    caller = ErpnextCaller("user", "asker@example.com")
    claims = _claims(erpnext_caller.mint(
        VECTOR_PRIVATE_KEY, caller, tool="draft_todo", arguments=FOLLOW_UP, purpose="draft",
    ))
    assert claims["purpose"] == "draft" and claims["tool"] == "draft_todo"
    # Reads carry none, so phase 2's shared vector is unchanged.
    assert "purpose" not in _claims(erpnext_caller.mint(VECTOR_PRIVATE_KEY, caller, tool="run_report", arguments={}))
    with pytest.raises(ValueError):
        erpnext_caller.mint(VECTOR_PRIVATE_KEY, ErpnextCaller("service"), tool="draft_todo", arguments={}, purpose="draft")
    with pytest.raises(ValueError):
        erpnext_caller.mint(VECTOR_PRIVATE_KEY, caller, tool="draft_todo", arguments={}, purpose="submit")


@pytest.mark.parametrize("name", [
    "erpnext__draft_todo",
    "erpnext__draft_material_request",
    # Built in oe_connector for 4b: refused by prefix, with no list to update.
    "erpnext__draft_quotation",
    "erpnext__draft_purchase_order",
    "erpnext__draft_check",
    "ERPNEXT__Draft-Todo",
    "erpnext__draft_something_new",
])
def test_the_model_never_reaches_a_draft_tool(
    name: str, erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    audited: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event", lambda event, *_a, **k: audited.append({"event": event, **k.get("details", {})}),
    )
    with set_session(_owner_turn(roster)):
        out = asyncio.run(mcp_gateway.get_active_gateway().call_tool({"name": name, "arguments": FOLLOW_UP}))  # type: ignore[union-attr]
    assert "approval card" in json.loads(out)["error"]
    assert erp.calls == []
    assert audited[-1]["address"] == "<draft-outside-card>"


def test_a_marker_covers_only_its_own_tool_and_arguments(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    check = {"doctype": "ToDo", "fields": FOLLOW_UP}
    with set_session(_owner_turn(roster)), erpnext_caller.draft_check_run(check):
        # Not the arguments it marked, nor another draft tool.
        other = asyncio.run(gateway.call_tool({"name": "erpnext__draft_check", "arguments": {**check, "x": 1}}))
        insert = asyncio.run(gateway.call_tool({"name": "erpnext__draft_todo", "arguments": check}))
        assert erp.calls == []
        assert "approval card" in other and "approval card" in insert
        asyncio.run(gateway.call_tool({"name": "erpnext__draft_check", "arguments": check}))
    assert erp.calls[0][0] == "erpnext__draft_check"
    claims = erp.claims()
    assert (claims["tool"], claims["sub"], claims["purpose"]) == ("draft_check", OWNER, "draft")


def test_the_executor_marker_signs_as_the_approver_with_no_session(erp: FakeErpnext) -> None:
    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    args = {**FOLLOW_UP, "idempotency_key": "oe:erpnext_draft:abc123456", "asked_by": OWNER}
    with erpnext_caller.draft_executor_run("erpnext__draft_todo", args, "Olivia@Co.Example"):
        asyncio.run(gateway.call_tool({"name": "erpnext__draft_todo", "arguments": args}))
    claims = erp.claims()
    assert (claims["tool"], claims["kind"], claims["sub"], claims["purpose"]) == (
        "draft_todo", "user", "olivia@co.example", "draft",
    )
    sent = dict(erp.calls[-1][1])
    sent.pop(ASSERTION_ARG)
    assert sent == args
    with pytest.raises(ValueError):
        erpnext_caller.draft_executor_run("erpnext__draft_check", args, OWNER)
    with pytest.raises(ValueError):
        erpnext_caller.draft_executor_run("erpnext__run_report", args, OWNER)


def test_drafts_need_per_person_access(erp: FakeErpnext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ERPNEXT_CALLER_PRIVATE_KEY")
    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    args = {**FOLLOW_UP, "idempotency_key": "oe:erpnext_draft:abc123456"}
    with erpnext_caller.draft_executor_run("erpnext__draft_todo", args, OWNER):
        out = asyncio.run(gateway.call_tool({"name": "erpnext__draft_todo", "arguments": args}))
    assert "per-person access" in out and erp.calls == []


# --------------------------------------------------------------------------- #
# Where it is offered
# --------------------------------------------------------------------------- #


def test_it_is_offered_to_the_principal_alone_and_never_unattended(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    assert drafts.offer_refusal(_owner_turn(roster)) is None
    teammate = Session(from_web_chat=True, caller_person_id=roster.teammate)
    assert "account owner" in str(drafts.offer_refusal(teammate))
    assert "nobody is watching" in str(drafts.offer_refusal(Session(unattended=True)))
    assert "panel" in str(drafts.offer_refusal(Session(erpnext_panel_email=OWNER)))
    assert "email" in str(drafts.offer_refusal(Session(origin_channel="email", caller_person_id=roster.principal)))
    assert "email" in str(drafts.offer_refusal(Session(private_to_principal=True)))
    assert drafts.offer_refusal(None) is not None
    # Every list that keeps a tool from a turn names it.
    assert drafts.PROPOSE in UNATTENDED_WITHHELD_TOOLS and drafts.PROPOSE in PRIVATE_TURN_WITHHELD_TOOLS
    assert drafts.PROPOSE in lockdown.MAIL_TOUCHED_WITHHELD_TOOLS
    assert erpnext_panel_withholds(drafts.PROPOSE, {})


def test_it_is_off_until_turned_on(erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFTS_ENABLED", "false")
    assert "not turned on" in str(drafts.offer_refusal(_owner_turn(roster)))
    monkeypatch.setenv("ERPNEXT_DRAFTS_ENABLED", "true")
    monkeypatch.delenv("ERPNEXT_CALLER_PRIVATE_KEY")
    assert drafts.offer_refusal(_owner_turn(roster)) is not None
    monkeypatch.setenv("ERPNEXT_CALLER_PRIVATE_KEY", VECTOR_PRIVATE_KEY)
    monkeypatch.setattr(mcp_gateway, "_active_gateway", None)
    assert drafts.offer_refusal(_owner_turn(roster)) is not None


def test_the_prompt_names_drafts_only_while_they_are_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.config import get_settings

    monkeypatch.setenv("ERPNEXT_CALLER_PRIVATE_KEY", VECTOR_PRIVATE_KEY)
    off = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    monkeypatch.setenv("ERPNEXT_DRAFTS_ENABLED", "true")
    on = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    assert "propose_erpnext_draft" not in off and "never change anything there." in off
    assert "propose_erpnext_draft" in on and "submit" in on and "unsubmitted" in on
    # Never the draft tools themselves.
    assert "erpnext__draft" not in on


# --------------------------------------------------------------------------- #
# Proposing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("doctype", "fields", "error"), [
    ("Quotation", {"company": "Acme"}, "doctype"),
    ("Journal Entry", {}, "doctype"),
    ("ToDo", {"allocated_to": "ana@co.example"}, "description"),
    ("ToDo", {**FOLLOW_UP, "docstatus": 1}, "takes only"),
    ("ToDo", {**FOLLOW_UP, "reference_name": None}, "together"),
    ("ToDo", {**FOLLOW_UP, "priority": "Urgent"}, "priority"),
    ("Material Request", {**RESTOCK, "items": []}, "items"),
    ("Material Request", {**RESTOCK, "items": [{"item_code": "X", "qty": -1, "warehouse": "W"}]}, "qty"),
    ("Material Request", {**RESTOCK, "items": [{"item_code": "X", "qty": 1, "warehouse": "W", "rate": 0}]}, "only"),
    ("Material Request", {**RESTOCK, "schedule_date": "next week"}, "YYYY-MM-DD"),
    ("Material Request", {**RESTOCK, "items": [{"item_code": "X", "qty": 1, "warehouse": "W"}] * 21}, "At most"),
])
def test_only_the_fields_oe_connector_takes_are_accepted(doctype: str, fields: dict[str, Any], error: str) -> None:
    out = drafts.check_fields(doctype, fields)
    assert isinstance(out, str) and error in out


def test_a_proposal_is_checked_by_erpnext_and_left_on_a_card(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    card = _restock_card(erp, roster)
    # Checked as the owner, without saving, and nothing else.
    assert [c[0] for c in erp.calls] == ["erpnext__draft_check"]
    assert erp.calls[0][1]["fields"] == RESTOCK
    assert card.status == STATUS_PROPOSED and card.approver_person_id == roster.principal
    payload = json.loads(card.proposed_payload_json)
    assert payload["tool"] == "erpnext__draft_material_request" and payload["fields"] == RESTOCK
    assert payload["card_key"] == card.idempotency_key and payload["asked_by"] == OWNER
    listed = drafts.cards(people_store.get_person(roster.principal))
    assert [c.decision_id for c in listed] == [card.id]
    assert {"label": "Estimated value", "value": "1,234.50 EUR"} in listed[0].fields
    assert listed[0].lines == [{"item_code": "ITEM-1", "qty": "40", "warehouse": "Stores - AC"}]


def test_the_chip_says_it_waits_and_carries_the_card(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    erp.replies.append(json.dumps(CHECKED_RESTOCK))
    raw = json.dumps(_propose(_owner_turn(roster), {"why": "x", "doctype": "Material Request", "fields": RESTOCK}))
    chip = summarize_action(tool_name=drafts.PROPOSE, tool_input={}, tool_result=raw)
    assert chip is not None
    assert chip["summary"] == "Waiting for your approval: Material Request draft"
    assert isinstance(chip["decision_id"], int) and chip["link"] == "/today"


def test_a_draft_erpnext_refuses_leaves_no_card(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    erp.replies.append(json.dumps({"error": "item 1: item_code 'ITEM-9' does not exist or you cannot see it."}))
    out = _propose(_owner_turn(roster), {"why": "x", "doctype": "Material Request", "fields": RESTOCK})
    assert "did not accept" in out["error"] and "<untrusted_content" in out["error"]
    assert drafts.open_cards(roster.principal) == []


def test_the_same_draft_is_one_card_and_a_turn_leaves_three(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    session = _owner_turn(roster)
    ids = []
    for qty in (40, 40, 50):
        erp.replies.append(json.dumps(CHECKED_RESTOCK))
        fields = {**RESTOCK, "items": [{**RESTOCK["items"][0], "qty": qty}]}
        ids.append(_propose(session, {"why": "x", "doctype": "Material Request", "fields": fields})["decision_id"])
    assert ids[0] == ids[1] != ids[2]
    out = _propose(session, {"why": "x", "doctype": "ToDo", "fields": FOLLOW_UP})
    assert "3 draft cards this turn" in out["error"]


def test_a_teammate_or_a_mail_reading_turn_cannot_propose(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    teammate = Session(from_web_chat=True, caller_person_id=roster.teammate)
    pin_turn_delegation(teammate, "make a todo")
    assert "account owner" in _propose(teammate, {"why": "x", "doctype": "ToDo", "fields": FOLLOW_UP})["error"]
    monkeypatch.setattr("openexecutive.delegation.settings.turn_read_delegate_mail", lambda: True)
    out = _propose(_owner_turn(roster), {"why": "x", "doctype": "ToDo", "fields": FOLLOW_UP})
    assert "read the user's own mail" in out["error"]
    assert erp.calls == []


# --------------------------------------------------------------------------- #
# Approving
# --------------------------------------------------------------------------- #


def test_approving_makes_the_stored_draft_once_as_the_approver(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    card = _restock_card(erp, roster)
    erp.replies.append(json.dumps(CREATED))
    created = _approve(card)
    assert created == {"doctype": "Material Request", "name": "MAT-MR-2026-00012",
                       "url": CREATED["url"], "already_created": False}
    tool, sent = erp.calls[-1]
    claims = erp.claims()
    assert tool == "erpnext__draft_material_request"
    assert (claims["sub"], claims["purpose"], claims["tool"]) == (OWNER, "draft", "draft_material_request")
    sent.pop(ASSERTION_ARG)
    assert sent == {**RESTOCK, "idempotency_key": card.idempotency_key, "asked_by": OWNER}
    done = get_decision_instance(card.id)
    assert done is not None and done.status == STATUS_APPROVED_UNCHANGED and done.external_event_id == "MAT-MR-2026-00012"
    # Claimed: a second tap makes nothing.
    with pytest.raises(drafts.ApproveRefused) as again:
        _approve(card)
    assert again.value.code == "already_handled"
    assert len(erp.calls) == 2


def test_a_failed_draft_waits_again_and_a_retry_sends_the_same_key(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    card = _restock_card(erp, roster)
    erp.replies.append(json.dumps({"error": "you have reached today's limit of 20 drafts from OpenExecutive."}))
    with pytest.raises(drafts.ApproveRefused) as failed:
        _approve(card)
    assert failed.value.status == 502 and "today's limit" in failed.value.message
    waiting = get_decision_instance(card.id)
    assert waiting is not None and waiting.status == STATUS_PROPOSED
    assert "today's limit" in drafts.cards(people_store.get_person(roster.principal))[0].last_error
    erp.replies.append(json.dumps({**CREATED, "already_created": True}))
    assert _approve(waiting)["already_created"] is True
    first_key, second_key = (c[1]["idempotency_key"] for c in erp.calls[1:])
    assert first_key == second_key == card.idempotency_key


def test_a_card_tried_too_often_is_closed(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drafts, "ATTEMPTS_MAX", 1)
    card = _restock_card(erp, roster)
    erp.replies.append("Tool error: boom")
    with pytest.raises(drafts.ApproveRefused):
        _approve(card)
    closed = get_decision_instance(card.id)
    assert closed is not None and closed.status == STATUS_FAILED


@pytest.mark.parametrize(("refusal", "status", "code"), [
    ("not_yours", 403, "not_yours"),
    ("caller_signing_required", 409, "caller_signing_required"),
])
def test_only_its_person_signed_in_may_approve(
    refusal: str, status: int, code: str, erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    card = _restock_card(erp, roster)
    signed["refusal"] = refusal
    with pytest.raises(drafts.ApproveRefused) as refused:
        _approve(card)
    assert (refused.value.status, refused.value.code) == (status, code)
    assert len(erp.calls) == 1  # the check only
    still = get_decision_instance(card.id)
    assert still is not None and still.status == STATUS_PROPOSED


def test_an_old_or_tampered_card_is_not_carried_out(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory.decision_ledger import update_open_payload

    card = _restock_card(erp, roster)
    payload = json.loads(card.proposed_payload_json)
    update_open_payload(card.id, {**payload, "tool": "erpnext__draft_purchase_order"})
    tampered = get_decision_instance(card.id)
    with pytest.raises(drafts.ApproveRefused) as bad:
        _approve(tampered)
    assert bad.value.code == "malformed"
    update_open_payload(card.id, payload)
    monkeypatch.setattr(drafts, "CARD_TTL", timedelta(seconds=-1))
    assert drafts.cards(people_store.get_person(roster.principal)) == []
    with pytest.raises(drafts.ApproveRefused) as old:
        _approve(get_decision_instance(card.id))
    assert old.value.code == "expired"
    assert len(erp.calls) == 1


def test_an_unusual_link_is_not_shown() -> None:
    # Frappe 16 on links the desk under /desk/, older ones under /app/.
    for link in ("https://erp.example/desk/todo/abc", "https://erp.example/app/todo/abc"):
        assert drafts._desk_link(link) == link
    for link in (
        "javascript:alert(1)", "https://erp.example/login", "https://erp.example/desk/x y",
        "https://erp.example/app/x y", "ftp://erp.example/desk/todo/abc", None, 3,
    ):
        assert drafts._desk_link(link) == ""


def test_the_card_shows_the_whole_description_it_will_send() -> None:
    # Past the 300 characters any other value is cut to, with its line breaks:
    # the approver must see every word the assignee will get under their name.
    description = "Chase the overdue invoice.\n" + "x" * (drafts.TEXT_MAX - 60) + "\nthen pay IBAN X today"
    fields = drafts.check_fields("ToDo", {**FOLLOW_UP, "description": description})
    assert isinstance(fields, dict)
    view = drafts.card_view("ToDo", {"description": "Chase the overdue invoice."}, fields)
    shown = {f["label"]: f["value"] for f in view["fields"]}
    assert shown["What"] == fields["description"] == description
    assert len(shown["What"]) > drafts.SHOWN_MAX


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    app = FastAPI()
    app.include_router(drafts_route.router)
    app.include_router(decisions_route.router)
    return TestClient(app)


def test_the_routes_show_and_resolve_a_card_for_its_person_alone(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], client: TestClient,
) -> None:
    card = _restock_card(erp, roster)
    olivia, ben = {"x-caller-email": OWNER}, {"x-caller-email": TEAM}
    listed = client.get("/erpnext/drafts", headers=olivia).json()["cards"]
    assert [c["decision_id"] for c in listed] == [card.id] and listed[0]["doctype"] == "Material Request"
    assert client.get("/erpnext/drafts", headers=ben).json()["cards"] == []
    assert client.post(f"/decisions/{card.id}/approve", json={"edits": None}, headers=ben).status_code == 404
    edited = client.post(f"/decisions/{card.id}/approve", json={"edits": {"qty": 9000}}, headers=olivia)
    assert edited.status_code == 422
    erp.replies.append(json.dumps(CREATED))
    resp = client.post(f"/decisions/{card.id}/approve", json={"edits": None}, headers=olivia)
    assert resp.status_code == 200 and resp.json()["status"] == STATUS_APPROVED_UNCHANGED
    assert json.loads(resp.json()["final_payload_json"])["created"]["name"] == "MAT-MR-2026-00012"
    assert client.get("/erpnext/drafts", headers=olivia).json()["cards"] == []


def test_dismissing_a_card_writes_nothing(
    erp: FakeErpnext, roster: SimpleNamespace, client: TestClient,
) -> None:
    card = _restock_card(erp, roster)
    resp = client.post(f"/decisions/{card.id}/reject", json={"reason": ""}, headers={"x-caller-email": OWNER})
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert len(erp.calls) == 1
    assert client.post(f"/decisions/{card.id}/cancel", headers={"x-caller-email": OWNER}).status_code == 409

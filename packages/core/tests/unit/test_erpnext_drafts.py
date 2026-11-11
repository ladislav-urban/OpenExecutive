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
from openexecutive.orchestrator.schedule_tools import erpnext_panel_withholds, set_session
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


@pytest.fixture(autouse=True)
def extension() -> Iterator[None]:
    """The draft tool registered as the install would (``OPENEXECUTIVE_EXTENSIONS``
    listing integrations.erpnext_extension), and forgotten after."""
    from openexecutive.integrations import erpnext_extension
    from openexecutive.orchestrator import extensions

    extensions._reset_for_tests()
    erpnext_extension.register()
    yield
    extensions._reset_for_tests()


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
    assert "email" in str(drafts.offer_refusal(Session(origin_channel="email", caller_person_id=roster.principal)))
    assert "email" in str(drafts.offer_refusal(Session(private_to_principal=True)))
    assert drafts.offer_refusal(None) is not None
    # An extension tool: never on an unattended, private, AI-app or replay
    # turn (the chat loop's extension rule), refused once the turn has read
    # the owner's mail, and outside the panel's own tools.
    from openexecutive.orchestrator import extensions

    assert drafts.PROPOSE in {t.name for t in extensions.offered_tools(_owner_turn(roster))}
    assert not extensions.allowed_after_mail_read(drafts.PROPOSE)
    assert lockdown.mail_touched_withholds(drafts.PROPOSE, {})
    assert erpnext_panel_withholds(drafts.PROPOSE, {})


def test_it_is_registered_to_be_asked_in_the_panel_and_kept_private() -> None:
    from openexecutive.orchestrator import extensions
    from openexecutive.orchestrator.activity_labels import summarize_activity

    # ERPNext's panel has no verified caller: the tool's own offered decides.
    (tool,) = [t for t in extensions._tools.values() if t.name == drafts.PROPOSE]
    assert tool.known_caller is False and tool.offered is drafts.offered
    assert extensions.private_row(drafts.PROPOSE)
    assert extensions.activity_label(drafts.PROPOSE) == "Checking the draft with the ERP…"
    activity = summarize_activity([{"name": drafts.PROPOSE, "input": {}}])
    assert "Checking the draft with the ERP" in str(activity)


def test_the_handler_refuses_on_its_own_after_a_mail_read() -> None:
    from openexecutive.delegation.settings import TurnDelegation
    from openexecutive.orchestrator.schedule_tools import current_session

    session = Session()
    token = current_session.set(session)
    try:
        session.turn_delegation = TurnDelegation(  # type: ignore[attr-defined]
            offered=True, touched_mail=True, read_mail=True, session_id=session.session_id,
        )
        result = json.loads(asyncio.run(drafts.handle_propose({})))
        assert "next message" in result["error"] and drafts.PROPOSE in result["error"]
    finally:
        current_session.reset(token)


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
    assert isinstance(chip["decision_id"], int) and chip["link"] == "/"


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
    # The repeat was the card already waiting: it used none of the turn's three.
    erp.replies.append(json.dumps({"ok": True, "summary": {}}))
    assert "decision_id" in _propose(session, {"why": "x", "doctype": "ToDo", "fields": FOLLOW_UP})
    out = _propose(session, {"why": "x", "doctype": "ToDo", "fields": {**FOLLOW_UP, "priority": "Low"}})
    assert "3 draft cards this turn" in out["error"]


def test_the_same_draft_again_is_audited_once(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(drafts, "audit", lambda event, *_a: events.append(event))
    session = _owner_turn(roster)
    for _ in range(2):
        erp.replies.append(json.dumps(CHECKED_RESTOCK))
        out = _propose(session, {"why": "x", "doctype": "Material Request", "fields": RESTOCK})
    assert events == ["erpnext_draft_proposed"]
    assert "already waiting" in out["note"]


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


def test_the_card_shows_what_approval_sends_not_erpnexts_reading() -> None:
    # Two lines and exact quantities stored; ERPNext's summary merged the
    # lines, rounded and named another assignee. The card shows what is sent.
    fields = drafts.check_fields("Material Request", {
        **RESTOCK,
        "items": [
            {"item_code": "ITEM-1", "qty": 0.004, "warehouse": "Stores - AC"},
            {"item_code": "ITEM-2", "qty": 2.125, "warehouse": "Stores - AC"},
        ],
    })
    assert isinstance(fields, dict)
    summary = {**CHECKED_RESTOCK["summary"], "company": "Other Co",
               "items": [{"item_code": "ITEM-1", "qty": 0, "warehouse": "Stores - AC"}]}
    view = drafts.card_view("Material Request", summary, fields)
    assert [(ln["item_code"], ln["qty"]) for ln in view["lines"]] == [("ITEM-1", "0.004"), ("ITEM-2", "2.125")]
    shown = {f["label"]: f["value"] for f in view["fields"]}
    assert shown["Company"] == "Acme"
    # What the fields leave to ERPNext still comes from its reading.
    assert shown["Type"] == "Purchase" and shown["Estimated value"].startswith("1,234.50")


def test_a_card_left_mid_approval_waits_again(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    from openexecutive.memory.decision_ledger import STATUS_EXECUTING, claim_for_execution

    card = _restock_card(erp, roster)
    person = people_store.get_person(roster.principal)
    # An approval this process is carrying out is left alone…
    assert claim_for_execution(card.id)
    drafts._APPROVING.add(card.id)
    try:
        assert drafts.cards(person) == []
        held = get_decision_instance(card.id)
        assert held is not None and held.status == STATUS_EXECUTING
    finally:
        drafts._APPROVING.discard(card.id)
    # …one nothing is carrying out (a restart) waits again.
    assert [c.decision_id for c in drafts.cards(person)] == [card.id]


def test_a_cancelled_approval_puts_the_card_back(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _restock_card(erp, roster)

    async def cancelled(*_a: Any) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(drafts, "_call", cancelled)
    with pytest.raises(asyncio.CancelledError):
        _approve(card)
    back = get_decision_instance(card.id)
    assert back is not None and back.status == STATUS_PROPOSED
    assert not drafts._APPROVING


def test_a_failed_attempt_is_noted_before_the_card_is_released(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory import decision_ledger

    card = _restock_card(erp, roster)
    order: list[str] = []
    real_update, real_release = decision_ledger.update_open_payload, decision_ledger.release_claim

    def update(*a: Any, **k: Any) -> bool:
        order.append("note" if k.get("while_executing") else "other")
        return real_update(*a, **k)

    def release(*a: Any, **k: Any) -> bool:
        order.append("release")
        return real_release(*a, **k)

    monkeypatch.setattr(decision_ledger, "update_open_payload", update)
    monkeypatch.setattr(decision_ledger, "release_claim", release)
    erp.replies.append(json.dumps({"error": "ERPNext is down"}))
    with pytest.raises(drafts.ApproveRefused):
        _approve(card)
    assert order == ["note", "release"]
    waiting = get_decision_instance(card.id)
    assert waiting is not None and json.loads(waiting.proposed_payload_json)["attempts"] == 1


def test_an_expired_card_is_closed(erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory.decision_ledger import STATUS_CLOSED_EXTERNALLY

    card = _restock_card(erp, roster)
    monkeypatch.setattr(drafts, "CARD_TTL", timedelta(seconds=-1))
    assert drafts.open_cards(roster.principal) == []
    gone = get_decision_instance(card.id)
    assert gone is not None and gone.status == STATUS_CLOSED_EXTERNALLY


def test_the_tool_says_where_the_card_is_outside_the_web_app() -> None:
    from openexecutive.prompts.connected_systems import _erpnext_drafts

    for doctypes in (("Material Request", "ToDo"), tuple(sorted(drafts.DRAFT_TOOLS))):
        for text in (drafts.propose_tool(doctypes)["description"], _erpnext_drafts(doctypes)):
            assert "in this chat and on Today" not in text
            assert "Today in the OpenExecutive web app" in text and "Telegram" in text


# --------------------------------------------------------------------------- #
# Phase 4b: Quotation and Purchase Order
# --------------------------------------------------------------------------- #

QUOTE = {"company": "Acme", "customer": "Globex", "valid_till": "2099-02-01",
         "items": [{"item_code": "ITEM-1", "qty": 10}, {"item_code": "ITEM-2", "qty": 2, "rate": 99.5}]}
CHECKED_QUOTE = {
    "ok": True, "doctype": "Quotation", "docstatus": 0,
    "summary": {
        "company": "Acme", "customer": "Globex", "valid_till": "2099-02-01",
        "currency": "USD", "company_currency": "EUR",
        "items": [{"item_code": "ITEM-1", "qty": 10, "rate": 12.0, "amount": 120.0},
                  {"item_code": "ITEM-2", "qty": 2, "rate": 99.5, "amount": 199.0}],
        "net_total": 319.0, "grand_total": 319.0, "base_grand_total": 290.12,
    },
}
ORDER_FROM_REQUEST = {"company": "Acme", "supplier": "Initech", "material_request": "MAT-MR-2026-00012"}
CHECKED_ORDER = {
    "ok": True, "doctype": "Purchase Order", "docstatus": 0,
    "summary": {
        "company": "Acme", "supplier": "Initech", "schedule_date": "2099-01-15", "currency": "EUR",
        "company_currency": "EUR", "from": {"doctype": "Material Request", "name": "MAT-MR-2026-00012"},
        "items": [{"item_code": "ITEM-1", "qty": 40, "rate": 8.0, "amount": 320.0, "warehouse": "Stores - AC"}],
        "net_total": 320.0, "grand_total": 320.0, "base_grand_total": 320.0,
    },
}


@pytest.fixture
def all_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFT_DOCTYPES", "ToDo, Material Request, Quotation, Purchase Order")


def test_quotations_and_orders_are_offered_only_once_listed(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.config import get_settings

    # By default an install drafts 4a's two only, whatever oe_connector has.
    assert drafts.enabled_doctypes() == ("Material Request", "ToDo")
    assert "doctype" in str(drafts.check_fields("Quotation", QUOTE))
    assert drafts.draft_tools()[0]["input_schema"]["properties"]["doctype"]["enum"] == ["Material Request", "ToDo"]
    before = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    assert "Quotation" not in before and "price list" not in before
    monkeypatch.setenv("ERPNEXT_DRAFT_DOCTYPES", "Quotation,Purchase Order,Journal Entry")
    assert drafts.enabled_doctypes() == ("Purchase Order", "Quotation")
    after = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    assert "a Quotation (a quote for a customer)" in after and "price list" in after
    assert "ToDo" not in after and "Journal Entry" not in after
    assert "price list" in drafts.draft_tools()[0]["description"]
    # Listing nothing oe_connector drafts turns drafts off; left blank, it is
    # the default.
    monkeypatch.setenv("ERPNEXT_DRAFT_DOCTYPES", "Journal Entry")
    assert "not turned on" in str(drafts.offer_refusal(_owner_turn(roster)))
    monkeypatch.setenv("ERPNEXT_DRAFT_DOCTYPES", "")
    assert drafts.enabled_doctypes() == ("Material Request", "ToDo")


@pytest.mark.parametrize(("doctype", "fields", "error"), [
    ("Quotation", {**QUOTE, "customer": None}, "customer"),
    ("Quotation", {**QUOTE, "discount_amount": 5}, "takes only"),
    ("Quotation", {**QUOTE, "items": [{"item_code": "X", "qty": 1, "rate": -1}]}, "rate"),
    ("Quotation", {**QUOTE, "items": [{"item_code": "X", "qty": 1, "rate": True}]}, "rate"),
    ("Quotation", {**QUOTE, "items": [{"item_code": "X", "qty": 1, "uom": "Box"}]}, "only"),
    ("Purchase Order", {"company": "Acme", "supplier": "Initech", "items": [{"item_code": "X", "qty": 1}]},
     "schedule_date"),
    ("Purchase Order", {"company": "Acme", "supplier": "Initech", "schedule_date": "2099-01-15",
                        "items": [{"item_code": "X", "qty": 1}]}, "warehouse"),
    ("Purchase Order", {**ORDER_FROM_REQUEST, "supplier_quotation": "PUR-SQTN-1"}, "not both"),
    ("Purchase Order", {**ORDER_FROM_REQUEST, "items": [{"item_code": "X", "qty": 1, "warehouse": "W"}]},
     "leave out `items`"),
    ("Purchase Order", {"company": "Acme", "material_request": "MAT-MR-2026-00012"}, "supplier"),
    ("Purchase Order", {**ORDER_FROM_REQUEST, "taxes_and_charges": "VAT"}, "takes only"),
])
def test_only_what_oe_connector_takes_on_a_quote_or_order(
    all_kinds: None, doctype: str, fields: dict[str, Any], error: str,
) -> None:
    out = drafts.check_fields(doctype, fields)
    assert isinstance(out, str) and error in out


def test_the_shapes_a_quote_or_order_may_take(all_kinds: None) -> None:
    assert drafts.check_fields("Quotation", QUOTE) == QUOTE
    # A Supplier Quotation brings its own supplier; the date is then optional.
    from_quote = {"company": "Acme", "supplier_quotation": "PUR-SQTN-2026-00003"}
    assert drafts.check_fields("Purchase Order", from_quote) == from_quote
    assert drafts.check_fields("Purchase Order", {**ORDER_FROM_REQUEST, "items": []}) == ORDER_FROM_REQUEST


def test_a_quote_card_shows_each_line_priced_and_the_total() -> None:
    view = drafts.card_view("Quotation", CHECKED_QUOTE["summary"], QUOTE)
    shown = {f["label"]: f["value"] for f in view["fields"]}
    assert (shown["Customer"], shown["Valid till"]) == ("Globex", "2099-02-01")
    assert shown["Grand total"] == "319.00 USD" and shown["In EUR"] == "290.12 EUR"
    assert view["lines"] == [
        {"item_code": "ITEM-1", "qty": "10", "warehouse": "", "rate": "12.00", "amount": "120.00"},
        {"item_code": "ITEM-2", "qty": "2", "warehouse": "", "rate": "99.50", "amount": "199.00"},
    ]
    # ITEM-1 had no rate: ERPNext's price list set it, and the card says so.
    assert any("price list" in n and "ITEM-1" in n and "ITEM-2" not in n for n in view["notes"])


def test_a_quote_card_shows_the_stored_lines_not_erpnexts() -> None:
    # ERPNext's reading dropped a line and a pricing rule moved a stated rate:
    # the card shows every stored line, the stated rate, and says what moved.
    summary = {**CHECKED_QUOTE["summary"], "customer": "Someone Else",
               "items": [{"item_code": "ITEM-1", "qty": 10, "rate": 12.0, "amount": 120.0},
                         {"item_code": "ITEM-2", "qty": 2, "rate": 80.0, "amount": 160.0}][1:]}
    view = drafts.card_view("Quotation", summary, QUOTE)
    assert {f["label"]: f["value"] for f in view["fields"]}["Customer"] == "Globex"
    assert [(ln["item_code"], ln["rate"], ln["amount"]) for ln in view["lines"]] == [
        ("ITEM-1", "", ""), ("ITEM-2", "99.50", ""),
    ]
    summary["items"] = [{"item_code": "ITEM-1", "qty": 10, "rate": 12.0, "amount": 120.0},
                        {"item_code": "ITEM-2", "qty": 2, "rate": 80.0, "amount": 160.0}]
    view = drafts.card_view("Quotation", summary, QUOTE)
    assert view["lines"][1]["rate"] == "99.50"
    assert any("pricing rules" in n and "80.00" in n for n in view["notes"])


def test_a_quote_line_erpnext_gave_no_figures_for_says_so() -> None:
    # ERPNext's code in its own case is the same line; a line it gave no
    # figures for (or none at all) says its rate is ERPNext's to set.
    summary = {**CHECKED_QUOTE["summary"],
               "items": [{"item_code": "item-1", "qty": 10, "rate": 12.0, "amount": 120.0},
                         {"item_code": "ITEM-2", "qty": 2}]}
    view = drafts.card_view("Quotation", summary, QUOTE)
    assert view["lines"][0]["amount"] == "120.00" and view["lines"][1]["rate"] == "99.50"
    assert not any("pricing rules" in n for n in view["notes"])
    summary["items"] = summary["items"][:1]
    view = drafts.card_view("Quotation", summary, QUOTE)
    assert any("didn't give figures for ITEM-2" in n for n in view["notes"])


def test_an_order_from_a_request_shows_erpnexts_lines_and_says_whose() -> None:
    view = drafts.card_view("Purchase Order", CHECKED_ORDER["summary"], ORDER_FROM_REQUEST)
    shown = {f["label"]: f["value"] for f in view["fields"]}
    assert shown["From"] == "Material Request MAT-MR-2026-00012" and shown["Needed by"] == "2099-01-15"
    assert shown["Grand total"] == "320.00 EUR" and "In EUR" not in shown
    assert view["lines"] == [
        {"item_code": "ITEM-1", "qty": "40", "warehouse": "Stores - AC", "rate": "8.00", "amount": "320.00"},
    ]
    # Approval makes this draft or nothing (the card's fingerprint): never more or less than shown.
    assert any("change before you approve" in n and "nothing is made" in n for n in view["notes"])
    assert not any("more or less" in n for n in view["notes"])


def test_the_same_draft_again_answers_with_the_card_it_left(
    all_kinds: None, erp: FakeErpnext, roster: SimpleNamespace,
) -> None:
    # The price list moved between the two asks: the model is told what the
    # waiting card shows, not the new figures.
    session = _owner_turn(roster, "quote Globex")
    erp.replies.append(json.dumps(CHECKED_QUOTE))
    first = _propose(session, {"why": "x", "doctype": "Quotation", "fields": QUOTE})
    moved = {**CHECKED_QUOTE, "summary": {**CHECKED_QUOTE["summary"], "grand_total": 999.0}}
    erp.replies.append(json.dumps(moved))
    again = _propose(session, {"why": "x", "doctype": "Quotation", "fields": QUOTE})
    assert again["decision_id"] == first["decision_id"]
    assert "319.00 USD" in again["card"] and "999.00" not in again["card"]


def test_approving_an_order_from_a_request_sends_the_source_as_stored(
    all_kinds: None, erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    erp.replies.append(json.dumps(CHECKED_ORDER))
    proposed = _propose(_owner_turn(roster, "order MAT-MR-2026-00012 from Initech"),
                        {"why": "Order the restock", "doctype": "Purchase Order", "fields": ORDER_FROM_REQUEST})
    card = get_decision_instance(proposed["decision_id"])
    assert card is not None
    erp.replies.append(json.dumps({**CREATED, "doctype": "Purchase Order", "name": "PUR-ORD-2026-00007"}))
    assert _approve(card)["name"] == "PUR-ORD-2026-00007"
    tool, sent = erp.calls[-1]
    assert tool == "erpnext__draft_purchase_order"
    assert (erp.claims()["purpose"], erp.claims()["tool"]) == ("draft", "draft_purchase_order")
    sent.pop(ASSERTION_ARG)
    # The source, as stored: the lines are ERPNext's mapper's, never the card's.
    assert sent == {**ORDER_FROM_REQUEST, "idempotency_key": card.idempotency_key, "asked_by": OWNER}


def test_a_card_whose_kind_was_unlisted_is_not_carried_out(
    all_kinds: None, erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    erp.replies.append(json.dumps(CHECKED_QUOTE))
    proposed = _propose(_owner_turn(roster, "quote Globex"), {"why": "x", "doctype": "Quotation", "fields": QUOTE})
    card = get_decision_instance(proposed["decision_id"])
    monkeypatch.setenv("ERPNEXT_DRAFT_DOCTYPES", "ToDo,Material Request")
    with pytest.raises(drafts.ApproveRefused) as off:
        _approve(card)
    assert off.value.code == "drafts_off" and "Quotation" in off.value.message
    assert len(erp.calls) == 1


# --------------------------------------------------------------------------- #
# Phase 4b: teammates
# --------------------------------------------------------------------------- #


def _teammate_turn(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, text: str = "chase ACC-SINV-2026-00042") -> Session:
    # Signed in to the web chat, with signed sign-ins on: where ERPNext
    # answers a teammate as themselves (erpnext_caller.caller_for).
    monkeypatch.setattr("openexecutive.api.caller.signing_on", lambda: True)
    session = Session(from_web_chat=True, caller_person_id=roster.teammate, web_caller_signed_in=True)
    pin_turn_delegation(session, text)
    return session


def test_a_teammate_drafts_for_themselves_once_let_in(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _teammate_turn(roster, monkeypatch)
    assert "account owner" in str(drafts.offer_refusal(session))
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    assert drafts.offer_refusal(session) is None
    erp.replies.append(json.dumps({"ok": True, "summary": {}}))
    proposed = _propose(session, {"why": "Chase the invoice", "doctype": "ToDo", "fields": FOLLOW_UP})
    card = get_decision_instance(proposed["decision_id"])
    # Checked as the teammate's own ERPNext user, and theirs alone.
    assert erp.claims()["sub"] == TEAM
    assert card is not None and card.approver_person_id == roster.teammate
    assert json.loads(card.proposed_payload_json)["asked_by"] == TEAM
    assert drafts.open_cards(roster.principal) == []
    assert [c.decision_id for c in drafts.cards(people_store.get_person(roster.teammate))] == [card.id]
    erp.replies.append(json.dumps({**CREATED, "doctype": "ToDo", "name": "abc123def"}))
    assert _approve(card)["name"] == "abc123def"
    assert (erp.claims()["sub"], erp.claims()["purpose"]) == (TEAM, "draft")


def test_a_teammates_card_waits_while_teammates_are_turned_off(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    erp.replies.append(json.dumps({"ok": True, "summary": {}}))
    proposed = _propose(_teammate_turn(roster, monkeypatch), {"why": "x", "doctype": "ToDo", "fields": FOLLOW_UP})
    card = get_decision_instance(proposed["decision_id"])
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "false")
    with pytest.raises(drafts.ApproveRefused) as off:
        _approve(card)
    assert off.value.code == "principal_only"
    assert len(erp.calls) == 1
    still = get_decision_instance(card.id)
    assert still is not None and still.status == STATUS_PROPOSED


def test_a_suspended_teammates_card_waits_until_they_are_restored(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Left the directory with a card waiting (docs/directory-sync-plan.md in
    # oe_connector, step 9, "Remove"): nobody can carry it out while they're
    # suspended, and it's theirs again once they're back.
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    erp.replies.append(json.dumps({"ok": True, "summary": {}}))
    proposed = _propose(_teammate_turn(roster, monkeypatch), {"why": "x", "doctype": "ToDo", "fields": FOLLOW_UP})
    card = get_decision_instance(proposed["decision_id"])
    assert people_store.suspend_person(roster.teammate, by="directory")
    people_registry.invalidate()
    with pytest.raises(drafts.ApproveRefused) as gone:
        _approve(card)
    assert gone.value.code == "gone"
    assert len(erp.calls) == 1
    still = get_decision_instance(card.id)
    assert still is not None and still.status == STATUS_PROPOSED

    assert people_store.restore_person(roster.teammate)
    people_registry.invalidate()
    erp.replies.append(json.dumps({**CREATED, "doctype": "ToDo", "name": "abc123def"}))
    assert _approve(still)["name"] == "abc123def"
    assert erp.claims()["sub"] == TEAM


def test_a_card_whose_asker_no_longer_matches_is_not_carried_out(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    from openexecutive.memory.decision_ledger import update_open_payload

    # Checked as one email; the person's People email now names another.
    card = _restock_card(erp, roster)
    update_open_payload(card.id, {**json.loads(card.proposed_payload_json), "asked_by": "someone@else.example"})
    with pytest.raises(drafts.ApproveRefused) as changed:
        _approve(get_decision_instance(card.id))
    assert changed.value.code == "changed"
    assert len(erp.calls) == 1


def test_a_teammate_where_others_read_cannot_propose(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    # Web chat without signed sign-ins: the signed-in email is not proven.
    monkeypatch.setattr("openexecutive.api.caller.signing_on", lambda: False)
    session = Session(from_web_chat=True, caller_person_id=roster.teammate, web_caller_signed_in=True)
    pin_turn_delegation(session, "make a todo")
    out = _propose(session, {"why": "x", "doctype": "ToDo", "fields": FOLLOW_UP})
    assert "signed" in out["error"]
    assert erp.calls == []


def test_the_prompt_names_teammates_only_once_let_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.config import get_settings

    monkeypatch.setenv("ERPNEXT_CALLER_PRIVATE_KEY", VECTOR_PRIVATE_KEY)
    monkeypatch.setenv("ERPNEXT_DRAFTS_ENABLED", "true")
    off = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    on = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    assert "Drafts (the principal only)" in off and "teammate" not in off.split("Drafts (")[1]
    assert "verified teammate in a conversation nobody else reads" in on
    assert "never prepare one for someone else to approve" in on


# --------------------------------------------------------------------------- #
# Phase 4b: ERPNext's panel
# --------------------------------------------------------------------------- #


def _panel_turn(email: str = OWNER) -> Session:
    # As integrations.erpnext_panel marks a panel turn: ERPNext's word.
    session = Session(erpnext_panel_email=email, origin_channel="erpnext", read_erpnext=True)
    pin_turn_delegation(session, "order 40 more ITEM-1 for Stores")
    return session


def test_the_panel_offers_drafts_to_the_team_member_erpnext_names(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .test_delegation_tools import _loop, _offered

    assert drafts.offer_refusal(_panel_turn()) is None
    # Its read tools and specialists (call_tool needs a gateway), and drafts.
    panel_tools = {"call_tool", "consult_specialist", drafts.PROPOSE}
    offered = set(_offered(_loop(_panel_turn(), [])))
    assert drafts.PROPOSE in offered and offered <= panel_tools
    # A teammate as in chat: only once teammates are let in.
    assert "account owner" in str(drafts.offer_refusal(_panel_turn(TEAM)))
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    assert drafts.offer_refusal(_panel_turn(TEAM)) is None
    # Nobody on the team, nothing.
    assert "not on the team" in str(drafts.offer_refusal(_panel_turn("stranger@else.example")))
    monkeypatch.setenv("ERPNEXT_DRAFTS_ENABLED", "false")
    assert set(_offered(_loop(_panel_turn(), []))) <= panel_tools - {drafts.PROPOSE}


def test_a_panel_card_says_where_it_was_asked_and_is_approved_in_the_app(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    erp.replies.append(json.dumps(CHECKED_RESTOCK))
    proposed = _propose(_panel_turn(), {"why": "Restock ITEM-1", "doctype": "Material Request", "fields": RESTOCK})
    assert erp.claims()["sub"] == OWNER
    card = get_decision_instance(proposed["decision_id"])
    assert card is not None and card.approver_person_id == roster.principal
    (listed,) = drafts.cards(people_store.get_person(roster.principal))
    assert drafts.PANEL_NOTE in listed.notes and listed.asked_in == "erpnext_panel"
    # Approving is the usual signed tap in the app; ERPNext's word alone made nothing.
    signed["refusal"] = "caller_signing_required"
    with pytest.raises(drafts.ApproveRefused):
        _approve(card)
    assert len(erp.calls) == 1


def test_the_panel_reply_names_the_cards_its_turn_left(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    from openexecutive.integrations import erpnext_panel

    def turn(session: Session, *inputs: dict[str, Any]) -> tuple[list[int], dict[str, Any]]:
        shown = erpnext_panel._TurnViews()
        token = erpnext_panel._turn_views.set(shown)
        try:
            ids = []
            for tool_input in inputs:
                erp.replies.append(json.dumps(CHECKED_RESTOCK))
                ids.append(_propose(session, tool_input)["decision_id"])
        finally:
            erpnext_panel._turn_views.reset(token)
        return ids, shown.payload()

    restock = {"why": "Restock ITEM-1", "doctype": "Material Request", "fields": RESTOCK}
    more = {**restock, "fields": {**RESTOCK, "items": [{**RESTOCK["items"][0], "qty": 41}]}}
    # Each card left, in order; the same draft again is the card already waiting.
    (first, second, again), payload = turn(_panel_turn(), restock, more, restock)
    assert again == first and payload == {"cards": [first, second]}
    # Found waiting on a later turn, it is named again.
    assert turn(_panel_turn(), restock)[1] == {"cards": [first]}
    # A card left anywhere but the panel names nothing.
    assert turn(_owner_turn(roster), more)[1] == {}


def test_the_panel_offers_nothing_to_anyone_not_on_the_team(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    people_store.upsert_person(full_name="Carl Contact", email="carl@co.example", kind="contact")
    people_store.archive_person(people_store.upsert_person(full_name="Gina Gone", email="gina@co.example"))
    people_registry.invalidate()
    for email in ("carl@co.example", "gina@co.example"):
        assert "not on the team" in str(drafts.offer_refusal(_panel_turn(email)))


def test_drafts_called_together_leave_no_more_than_a_turn_allows(
    erp: FakeErpnext, roster: SimpleNamespace,
) -> None:
    from .test_delegation_tools import ToolUseBlock, _loop

    # Five different restocks in one round: their checks all wait on ERPNext
    # together, yet only CARDS_PER_TURN cards are left.
    calls = []
    for qty in range(1, 6):
        erp.replies.append(json.dumps(CHECKED_RESTOCK))
        fields = {**RESTOCK, "items": [{**RESTOCK["items"][0], "qty": qty}]}
        calls.append(ToolUseBlock(f"tu{qty}", drafts.PROPOSE, {"why": "x", "doctype": "Material Request", "fields": fields}))
    provider = _loop(_owner_turn(roster), calls)
    results = [r["content"] for r in provider.calls[1]["messages"][-1]["content"]]
    assert sum("waiting_for_approval" in r for r in results) == drafts.CARDS_PER_TURN
    assert len(drafts.open_cards(roster.principal)) == drafts.CARDS_PER_TURN


def test_the_panel_still_reaches_nothing_but_erpnext(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    from .test_delegation_tools import ToolUseBlock, _loop

    erp.replies.append(json.dumps(CHECKED_RESTOCK))
    provider = _loop(_panel_turn(), [
        ToolUseBlock("tu1", drafts.PROPOSE, {"why": "x", "doctype": "Material Request", "fields": RESTOCK}),
        ToolUseBlock("tu2", "call_tool", {"name": "google_workspace__send_gmail_message", "arguments": {}}),
    ])
    results = {r["tool_use_id"]: r["content"] for r in provider.calls[1]["messages"][-1]["content"]}
    assert "waiting_for_approval" in results["tu1"]
    assert "only read ERPNext" in results["tu2"]


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


def test_a_teammate_sees_their_cards_only_under_signed_sign_ins(
    erp: FakeErpnext, roster: SimpleNamespace, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    erp.replies.append(json.dumps({"ok": True, "summary": {}}))
    proposed = _propose(_teammate_turn(roster, monkeypatch), {"why": "x", "doctype": "ToDo", "fields": FOLLOW_UP})
    ben = {"x-caller-email": TEAM}
    listed = client.get("/erpnext/drafts", headers=ben).json()["cards"]
    assert [c["decision_id"] for c in listed] == [proposed["decision_id"]]
    # The principal never sees a teammate's card.
    assert client.get("/erpnext/drafts", headers={"x-caller-email": OWNER}).json()["cards"] == []
    monkeypatch.setattr("openexecutive.api.caller.signing_on", lambda: False)
    assert client.get("/erpnext/drafts", headers=ben).json()["cards"] == []


def test_dismissing_a_card_writes_nothing(
    erp: FakeErpnext, roster: SimpleNamespace, client: TestClient,
) -> None:
    card = _restock_card(erp, roster)
    resp = client.post(f"/decisions/{card.id}/reject", json={"reason": ""}, headers={"x-caller-email": OWNER})
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert len(erp.calls) == 1
    assert client.post(f"/decisions/{card.id}/cancel", headers={"x-caller-email": OWNER}).status_code == 409


# --------------------------------------------------------------------------- #
# Only the draft the card showed
# --------------------------------------------------------------------------- #

SHOWN = "a" * 64
NOW = "b" * 64


def _fingerprinted_card(erp: FakeErpnext, roster: SimpleNamespace, fingerprint: Any = SHOWN) -> Any:
    erp.replies.append(json.dumps({**CHECKED_RESTOCK, "fingerprint": fingerprint}))
    result = _propose(_owner_turn(roster), {"why": "Restock ITEM-1", "doctype": "Material Request", "fields": RESTOCK})
    card = get_decision_instance(result["decision_id"])
    assert card is not None
    return card


def test_approving_sends_back_the_fingerprint_erpnext_checked(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    card = _fingerprinted_card(erp, roster)
    erp.replies.append(json.dumps(CREATED))
    _approve(card)
    # Signed with it: the assertion covers the exact arguments sent.
    assert erp.claims()["tool"] == "draft_material_request"
    _, sent = erp.calls[-1]
    sent.pop(ASSERTION_ARG)
    assert sent == {**RESTOCK, "idempotency_key": card.idempotency_key, "asked_by": OWNER,
                    "expected_fingerprint": SHOWN}


@pytest.mark.parametrize("bad", ["abc", "A" * 64, 7, None])
def test_a_fingerprint_that_is_not_one_is_neither_kept_nor_sent(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], bad: Any,
) -> None:
    card = _fingerprinted_card(erp, roster, fingerprint=bad)
    assert "fingerprint" not in json.loads(card.proposed_payload_json)
    erp.replies.append(json.dumps(CREATED))
    _approve(card)
    assert "expected_fingerprint" not in erp.calls[-1][1]


def test_a_draft_that_changed_since_its_card_closes_the_card(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(drafts, "audit", lambda event, *_a: events.append(event))
    card = _fingerprinted_card(erp, roster)
    # As oe_connector refuses it, with the MCP layer's own words in front.
    erp.replies.append(
        f"Tool error: {drafts.CHANGED_SINCE_PROPOSED}: ERPNext would now make it differently. Nothing was made."
    )
    with pytest.raises(drafts.ApproveRefused) as refused:
        _approve(card)
    assert (refused.value.status, refused.value.code) == (409, "changed_since_proposed")
    assert "Ask for it again" in refused.value.message
    closed = get_decision_instance(card.id)
    assert closed is not None and closed.status == STATUS_FAILED
    assert json.loads(closed.proposed_payload_json).get("attempts") == 0
    assert events[-1] == "erpnext_draft_changed"
    # Asked again, a new card with the draft as ERPNext reads it now.
    fresh = _fingerprinted_card(erp, roster, fingerprint=NOW)
    assert fresh.id != card.id and json.loads(fresh.proposed_payload_json)["fingerprint"] == NOW


def test_asking_again_when_erpnext_reads_it_differently_replaces_the_card_never_changes_it(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    # A screen still showing the old card must not approve the new reading
    # (security review): the old card is closed, and a new one left.
    card = _fingerprinted_card(erp, roster)
    changed = {**CHECKED_RESTOCK, "fingerprint": NOW, "summary": {**CHECKED_RESTOCK["summary"], "currency": "USD"}}
    erp.replies.append(json.dumps(changed))
    again = _propose(_owner_turn(roster), {"why": "Restock ITEM-1", "doctype": "Material Request", "fields": RESTOCK})
    assert again["decision_id"] != card.id
    old = get_decision_instance(card.id)
    assert old is not None and old.status != STATUS_PROPOSED
    assert json.loads(old.proposed_payload_json)["fingerprint"] == SHOWN
    new = get_decision_instance(again["decision_id"])
    assert new is not None and json.loads(new.proposed_payload_json)["fingerprint"] == NOW
    assert new.idempotency_key != card.idempotency_key
    # The old card's screen approves nothing.
    calls = len(erp.calls)
    with pytest.raises(drafts.ApproveRefused) as stale:
        _approve(old)
    assert stale.value.code == "already_handled" and len(erp.calls) == calls
    # The same reading again is the new card, unchanged.
    erp.replies.append(json.dumps(changed))
    same = _propose(_owner_turn(roster), {"why": "Restock ITEM-1", "doctype": "Material Request", "fields": RESTOCK})
    assert same["decision_id"] == new.id


# --------------------------------------------------------------------------- #
# The draft log's seal
# --------------------------------------------------------------------------- #


def test_the_draft_logs_seal_is_kept_on_the_card_and_in_the_audit(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # oe_connector's "OE Draft" row is sealed in a hash chain; keeping its
    # seal here lets the two records check each other.
    rows: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(drafts, "audit", lambda event, _summary, details: rows.append((event, details)))
    card = _restock_card(erp, roster)
    erp.replies.append(json.dumps({**CREATED, "row_hash": "c" * 64, "chain_index": 4}))
    created = _approve(card)
    assert (created["row_hash"], created["chain_index"]) == ("c" * 64, 4)
    done = get_decision_instance(card.id)
    assert done is not None and json.loads(done.final_payload_json or "{}")["created"]["row_hash"] == "c" * 64
    assert dict(rows)["erpnext_draft_created"]["row_hash"] == "c" * 64
    assert dict(rows)["erpnext_draft_created"]["chain_index"] == 4
    # What oe-connector-verify-drafts --expect compares with the log, by card key.
    assert drafts.recorded_seals() == {card.idempotency_key: {"row_hash": "c" * 64, "chain_index": 4}}


@pytest.mark.parametrize("seal", ["abc", "C" * 64, 7, None])
def test_a_seal_that_is_not_one_is_not_kept(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], seal: Any,
) -> None:
    card = _restock_card(erp, roster)
    erp.replies.append(json.dumps({**CREATED, "row_hash": seal}))
    assert "row_hash" not in _approve(card)


def test_a_draft_without_a_seal_is_not_in_the_export(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    card = _restock_card(erp, roster)
    erp.replies.append(json.dumps(CREATED))
    _approve(card)
    assert drafts.recorded_seals() == {}

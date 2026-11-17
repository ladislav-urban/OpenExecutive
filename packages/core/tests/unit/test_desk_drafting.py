# ruff: noqa: F811 — pytest fixtures imported from test_erpnext_drafts share their names with the test parameters.
"""Draft cards proposed from ERPNext's desk (decided Nov 6,
2026): a host origin's turn (``Session.from_host``: the desk's Conversations
screen, the docked panel's chat) may propose an ERPNext draft card, as the
person signed in to this app, on a conversation private to them. It still
approves nothing: a card is approved only in this app, signed in
(``erpnext_drafts.approve``)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.orchestrator import erpnext_caller, mcp_gateway
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from .test_delegation_tools import db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import OWNER, FakeErpnext, erp, extension, roster  # noqa: F401
from .test_mcp_client_turns import _run_loop


@pytest.fixture(autouse=True)
def signed(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api import caller as api_caller

    monkeypatch.setattr(api_caller, "signing_on", lambda: True)


def _desk(person_id: int, *, signed_in: bool = True) -> Session:
    return Session(from_web_chat=True, web_caller_signed_in=signed_in, caller_person_id=person_id, from_host=True)


def test_the_signed_in_person_may_propose_from_the_desk(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert drafts.offer_refusal(_desk(roster.principal)) is None
    # A teammate only with drafts for teammates on, as in the app itself.
    assert "account owner" in str(drafts.offer_refusal(_desk(roster.teammate)))
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    assert drafts.offer_refusal(_desk(roster.teammate)) is None


def test_a_demo_person_on_the_desk_proposes_for_themselves(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    dana = people_store.upsert_person(full_name="Dana Demo", email="dana@prospect.example")
    people_store.link_directory(dana, "kc-dana")
    assert people_store.set_directory_demo(dana, True)
    people_registry.invalidate()
    turn = _desk(dana)
    turn.demo_caller = True
    assert drafts.offer_refusal(turn) is None
    who = drafts._proposer(turn)
    assert not isinstance(who, str) and who[1] == "dana@prospect.example"


def test_the_desk_names_no_one_without_a_signed_private_sign_in(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    assert drafts.offer_refusal(_desk(roster.principal, signed_in=False)) is not None
    no_person = Session(from_web_chat=True, web_caller_signed_in=True, from_host=True)
    assert drafts.offer_refusal(no_person) is not None
    shared = _desk(roster.principal)
    shared.shared_chat = True
    assert drafts.offer_refusal(shared) is not None
    unattended = _desk(roster.principal)
    unattended.unattended = True
    assert drafts.offer_refusal(unattended) is not None


def test_the_desk_turn_is_offered_the_draft_card_and_no_other_extension_tool(
    erp: FakeErpnext, roster: SimpleNamespace,
) -> None:
    offered = _run_loop(_desk(roster.principal), [], mcp_gateway.get_active_gateway()).offered[0]
    assert drafts.PROPOSE in offered
    assert "schedule_erp_metric" not in offered and "cancel_erp_metric" not in offered
    # Not signed in: no card tool.
    assert drafts.PROPOSE not in _run_loop(
        _desk(roster.principal, signed_in=False), [], mcp_gateway.get_active_gateway()
    ).offered[0]


def test_only_the_cards_own_marked_check_reaches_erpnext_from_the_desk(
    erp: FakeErpnext, roster: SimpleNamespace,
) -> None:
    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    check = {"doctype": "ToDo", "fields": {"description": "Follow up"}}
    with set_session(_desk(roster.principal)):
        # The model's own call: refused, as any write from the desk.
        refused = asyncio.run(gateway.call_tool({"name": "erpnext__draft_check", "arguments": check}))
        assert erp.calls == [] and "error" in json.loads(refused)
        # The card's own check, marked by code: through, signed as the person.
        with erpnext_caller.draft_check_run(check):
            asyncio.run(gateway.call_tool({"name": "erpnext__draft_check", "arguments": check}))
            # The marker never covers a draft insert.
            insert = asyncio.run(gateway.call_tool({"name": "erpnext__draft_todo", "arguments": check}))
            assert "error" in json.loads(insert)
    assert [c[0] for c in erp.calls] == ["erpnext__draft_check"]
    claims = erp.claims()
    assert (claims["tool"], claims["sub"]) == ("draft_check", OWNER)


def test_a_desk_card_says_where_it_was_asked(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    # Signed in, but the words came through another app's page: the card,
    # its payload and its audit row say so, as a panel card does.
    from openexecutive.delegation.settings import pin_turn_delegation

    from .test_erpnext_drafts import CHECKED_RESTOCK, RESTOCK, _propose

    erp.replies.append(json.dumps(CHECKED_RESTOCK))
    desk = _desk(roster.principal)
    pin_turn_delegation(desk, "restock ITEM-1 for Stores")
    proposed = _propose(desk, {"why": "Restock ITEM-1", "doctype": "Material Request", "fields": RESTOCK})
    assert "decision_id" in proposed, proposed
    (card,) = drafts.cards(people_store.get_person(roster.principal))
    assert card.asked_in == drafts.ASKED_IN_DESK and drafts.DESK_NOTE in card.notes
    assert drafts.PANEL_NOTE not in card.notes

# ruff: noqa: F811 — pytest fixtures imported from test_erpnext_drafts share their names with the test parameters.
"""A draft card as it stands, in the product's words (the pilot's S14 capture,
Nov 7, 2026): the card and its refusals call ERPNext "the ERP" on an install
with a name; a reply speaks of this conversation's earlier cards only as each
stands; and a card's chip follows what became of it."""
from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.memory.decision_ledger import (
    STATUS_REJECTED,
    close_externally,
    get_decision_instance,
    mark_resolved,
)
from openexecutive.orchestrator.action_chips import (
    card_outcome,
    refresh_chips,
    summarize_action,
    summary_for_outcome,
)
from openexecutive.orchestrator.session import Session
from openexecutive.people import store as people_store
from openexecutive.utils.product_name import product_name_override

from .test_delegation_tools import OWNER, TEAM, db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import (  # noqa: F401
    CHECKED_RESTOCK,
    CREATED,
    RESTOCK,
    FakeErpnext,
    _approve,
    _owner_turn,
    _propose,
    erp,
    extension,
    roster,
    signed,
)


@pytest.fixture
def named() -> Iterator[None]:
    token = product_name_override.set("Dynocortex")
    yield
    product_name_override.reset(token)


def _card_in(erp: FakeErpnext, session: Session, why: str = "Restock ITEM-1") -> dict[str, Any]:
    erp.replies.append(json.dumps(CHECKED_RESTOCK))
    return _propose(session, {"why": why, "doctype": "Material Request", "fields": RESTOCK})


# --------------------------------------------------------------------------- #
# (1) ERPNext's name
# --------------------------------------------------------------------------- #


def test_a_refusal_is_in_the_products_words_on_a_named_install(named: None) -> None:
    refused = drafts.ApproveRefused(409, "changed_since_proposed", "This draft changed in ERPNext since the card was left.")
    assert refused.message == "This draft changed in the ERP since the card was left."
    assert "ERPNext" not in str(refused)


def test_a_refusal_keeps_erpnexts_name_on_an_install_without_one() -> None:
    refused = drafts.ApproveRefused(409, "x", "This draft changed in ERPNext since the card was left.")
    assert refused.message == "This draft changed in ERPNext since the card was left."


def test_the_card_and_its_chip_say_the_erp_on_a_named_install(
    erp: FakeErpnext, roster: SimpleNamespace, named: None,
) -> None:
    raw = _card_in(erp, _owner_turn(roster), why="Restock ITEM-1 (ERPNext alert)")
    card = get_decision_instance(raw["decision_id"])
    assert card is not None
    payload = json.loads(card.proposed_payload_json)
    payload["view"]["notes"] = [*payload["view"].get("notes", []), "Rates from ERPNext's price list at the check."]
    payload["last_error"] = "ERPNext didn't answer in time."
    from openexecutive.memory.decision_ledger import update_open_payload

    assert update_open_payload(card.id, payload, expected_json=card.proposed_payload_json)
    (shown,) = drafts.cards(people_store.get_person(roster.principal))
    assert shown.why == "Restock ITEM-1 (the ERP alert)"
    assert "Rates from the ERP's price list at the check." in shown.notes
    assert shown.last_error == "The ERP didn't answer in time."
    chip = summarize_action(tool_name=drafts.PROPOSE, tool_input={}, tool_result=json.dumps(raw))
    assert chip is not None and "ERPNext" not in chip["summary"]


# --------------------------------------------------------------------------- #
# (2) This conversation's earlier cards, as each stands
# --------------------------------------------------------------------------- #


def test_a_new_card_names_this_conversations_earlier_cards_as_they_stand(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    session = _owner_turn(roster)
    first = _card_in(erp, session, why="first")
    assert first["earlier_cards"] == []
    card = get_decision_instance(first["decision_id"])
    assert card is not None
    erp.replies.append(json.dumps(CREATED))
    _approve(card)
    # A card from another conversation is no business of this one.
    other = _card_in(erp, _owner_turn(roster), why="elsewhere")
    second_input = {"why": "second", "doctype": "Material Request",
                    "fields": {**RESTOCK, "schedule_date": "2099-02-01"}}
    erp.replies.append(json.dumps(CHECKED_RESTOCK))
    second = _propose(_same_session_turn(session, roster), second_input)
    assert second["earlier_cards"] == [{
        "decision_id": first["decision_id"], "doctype": "Material Request", "status": "approved",
        "name": "MAT-MR-2026-00012",
    }]
    assert other["decision_id"] not in [c["decision_id"] for c in second["earlier_cards"]]
    assert "never call one waiting that isn't" in second["note"]
    # Dismissed, and a third card in the same conversation says so.
    assert mark_resolved(second["decision_id"], STATUS_REJECTED, resolver_person_id=roster.principal)
    third_input = {"why": "third", "doctype": "Material Request",
                   "fields": {**RESTOCK, "schedule_date": "2099-03-01"}}
    erp.replies.append(json.dumps(CHECKED_RESTOCK))
    third = _propose(_same_session_turn(session, roster), third_input)
    assert [c["status"] for c in third["earlier_cards"]] == ["approved", "dismissed"]


def _same_session_turn(session: Session, roster: SimpleNamespace) -> Session:
    """The next turn of the same conversation (a fresh turn pin, same id)."""
    from openexecutive.delegation.settings import pin_turn_delegation

    nxt = Session(from_web_chat=True, caller_person_id=roster.principal, session_id=session.session_id)
    pin_turn_delegation(nxt, "another")
    return nxt


def test_a_card_closed_by_a_change_or_past_its_time_is_not_waiting(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    session = _owner_turn(roster)
    card = get_decision_instance(_card_in(erp, session)["decision_id"])
    assert card is not None
    erp.replies.append(json.dumps({"error": drafts.CHANGED_SINCE_PROPOSED}))
    with pytest.raises(drafts.ApproveRefused):
        _approve(card)
    closed = get_decision_instance(card.id)
    assert closed is not None and card_outcome(closed) == "closed"
    other = get_decision_instance(_card_in(erp, _owner_turn(roster), why="old")["decision_id"])
    assert other is not None
    close_externally(other.id, reason="expired")
    expired = get_decision_instance(other.id)
    assert expired is not None and card_outcome(expired) == "expired"


# --------------------------------------------------------------------------- #
# (3) A chip follows its card
# --------------------------------------------------------------------------- #


def test_a_chip_says_what_became_of_its_card() -> None:
    waiting = "Waiting for your approval: Quotation draft"
    assert summary_for_outcome(waiting, "waiting") == waiting
    assert summary_for_outcome(waiting, "approved") == "Approved: Quotation draft"
    assert summary_for_outcome(waiting, "dismissed") == "Dismissed: Quotation draft"
    assert summary_for_outcome(waiting, "closed") == "Closed, nothing made: Quotation draft"
    assert summary_for_outcome(waiting, "expired") == "Expired: Quotation draft"
    assert summary_for_outcome("Waiting for your approval", "approved") == "Approved"
    assert summary_for_outcome("Booked: Lunch", "approved") == "Booked: Lunch"


def test_a_reloaded_chat_shows_each_chip_as_its_card_stands(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    session = _owner_turn(roster)
    approved = _card_in(erp, session, why="a")
    card = get_decision_instance(approved["decision_id"])
    assert card is not None
    erp.replies.append(json.dumps(CREATED))
    _approve(card)
    waiting = _card_in(erp, _owner_turn(roster), why="b")
    chips = [
        summarize_action(tool_name=drafts.PROPOSE, tool_input={}, tool_result=json.dumps(r))
        for r in (approved, waiting)
    ]
    messages: list[dict[str, Any]] = [
        {"role": "assistant", "content": "x", "actions": [dict(c) for c in chips if c]},
        {"role": "user", "content": "y"},
        {"role": "assistant", "content": "z", "actions": [{"summary": "Waiting for your approval", "decision_id": 999999}]},
    ]
    refresh_chips(messages)
    first, second = messages[0]["actions"]
    assert first["summary"] == "Approved: Material Request draft"
    assert second["summary"] == "Waiting for your approval: Material Request draft"
    # A card that can't be found is left as saved.
    assert messages[2]["actions"][0]["summary"] == "Waiting for your approval"

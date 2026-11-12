# ruff: noqa: F811 — pytest fixtures imported from test_erpnext_drafts share their names with the test parameters.
"""The Dunning card (phase 0's S2, OpenExecutive's half): a payment reminder
for one overdue Sales Invoice, drafted by oe_connector's draft_dunning
through ERPNext's own mapper. The card keeps the letter date its check used,
so approving on a later day builds the same letter with the same interest;
asking again for the same invoice replaces the waiting card; and the card
shows each installment, the charges and the letter as ERPNext reads them."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.memory.decision_ledger import get_decision_instance
from openexecutive.orchestrator.erpnext_caller import ASSERTION_ARG
from openexecutive.people import store as people_store

from .test_delegation_tools import OWNER, db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import (  # noqa: F401
    FakeErpnext,
    _approve,
    _owner_turn,
    _propose,
    erp,
    extension,
    roster,
    signed,
)

FINGERPRINT = "a" * 64
LATER = "b" * 64
SUMMARY = {
    "sales_invoice": "ACC-SINV-2026-00042", "dunning_type": "First reminder - AC", "posting_date": "2026-11-09",
    "company": "Aurora Components", "customer": "CUST-0007", "customer_name": "Harbor Electric Co-op",
    "currency": "EUR", "company_currency": "EUR",
    "rows": [
        {"payment_schedule": "ps1", "due_date": "2026-09-30", "outstanding": 1200.0, "overdue_days": 40,
         "interest": 13.15, "dunning_level": 1},
    ],
    "total_outstanding": 1200.0, "dunning_fee": 10.0, "rate_of_interest": 10.0, "total_interest": 13.15,
    "dunning_amount": 23.15, "grand_total": 1223.15, "base_dunning_amount": 23.15,
    "body_text": "<p>Dear Harbor Electric Co-op,</p><p>our invoice ACC-SINV-2026-00042 is overdue.</p>",
    "closing_text": "<p>Kind regards</p>", "language": "en",
}
CHECKED = {"ok": True, "doctype": "Dunning", "fingerprint": FINGERPRINT, "summary": SUMMARY}
ASK = {"why": "Remind Harbor of the overdue invoice", "doctype": "Dunning",
       "fields": {"sales_invoice": "ACC-SINV-2026-00042"}}


@pytest.fixture(autouse=True)
def dunning_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFT_DOCTYPES", "ToDo, Material Request, Dunning")


def _card(erp: FakeErpnext, roster: SimpleNamespace, checked: dict[str, Any] = CHECKED) -> Any:
    erp.replies.append(json.dumps(checked))
    result = _propose(_owner_turn(roster, "send Harbor a reminder"), ASK)
    assert "decision_id" in result, result
    card = get_decision_instance(result["decision_id"])
    assert card is not None
    return card


@pytest.mark.parametrize(("fields", "error"), [
    ({}, "sales_invoice"),
    ({"sales_invoice": "SINV-1", "letter": "x"}, "letter"),
    ({"sales_invoice": "SINV-1", "posting_date": "2026-13-40"}, "posting_date"),
    ({"sales_invoice": "SINV-1", "posting_date": (datetime.now(UTC) + timedelta(days=5)).date().isoformat()}, "future"),
])
def test_a_dunning_takes_only_its_own_fields(fields: dict[str, Any], error: str) -> None:
    out = drafts.check_fields("Dunning", fields)
    assert isinstance(out, str) and error in out


def test_it_is_offered_only_once_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "Dunning" in drafts.enabled_doctypes()
    monkeypatch.setenv("ERPNEXT_DRAFT_DOCTYPES", "ToDo, Material Request")
    assert "Dunning" not in drafts.enabled_doctypes()


def test_the_card_keeps_the_letter_date_its_check_used(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    card = _card(erp, roster)
    # Checked without a date (ERPNext dates it today), stored with the date it used.
    assert erp.calls[0][1]["fields"] == {"sales_invoice": "ACC-SINV-2026-00042"}
    payload = json.loads(card.proposed_payload_json)
    assert payload["fields"] == {"sales_invoice": "ACC-SINV-2026-00042", "posting_date": "2026-11-09"}
    assert payload["tool"] == "erpnext__draft_dunning" and payload["fingerprint"] == FINGERPRINT
    erp.replies.append(json.dumps({"ok": True, "doctype": "Dunning", "name": "DUNN-0001", "docstatus": 0,
                                   "url": "/app/dunning/DUNN-0001"}))
    _approve(card)
    tool, sent = erp.calls[-1]
    sent.pop(ASSERTION_ARG)
    assert tool == "erpnext__draft_dunning"
    assert sent == {"sales_invoice": "ACC-SINV-2026-00042", "posting_date": "2026-11-09",
                    "idempotency_key": card.idempotency_key, "asked_by": OWNER,
                    "expected_fingerprint": FINGERPRINT}


def test_no_card_without_a_fingerprint_or_a_sound_date(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    erp.replies.append(json.dumps({**CHECKED, "fingerprint": None}))
    assert "fingerprint" in _propose(_owner_turn(roster, "remind Harbor"), ASK)["error"]
    erp.replies.append(json.dumps({**CHECKED, "summary": {**SUMMARY, "posting_date": "soon"}}))
    assert "date" in _propose(_owner_turn(roster, "remind Harbor"), ASK)["error"]


def test_asking_again_another_day_replaces_the_waiting_card(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    first = _card(erp, roster)
    later = {**CHECKED, "fingerprint": LATER, "summary": {**SUMMARY, "posting_date": "2026-11-10", "total_interest": 13.48}}
    second = _card(erp, roster, later)
    assert second.id != first.id
    closed = get_decision_instance(first.id)
    assert closed is not None and closed.status != "proposed"
    assert json.loads(second.proposed_payload_json)["fields"]["posting_date"] == "2026-11-10"


def test_the_card_shows_each_installment_the_charges_and_the_letter(
    erp: FakeErpnext, roster: SimpleNamespace,
) -> None:
    _card(erp, roster)
    listed = drafts.cards(people_store.get_person(roster.principal))
    shown = {f["label"]: f["value"] for f in listed[0].fields}
    assert shown["Customer"] == "Harbor Electric Co-op"
    assert shown["Invoice"] == "ACC-SINV-2026-00042"
    assert shown["Letter date"] == "2026-11-09"
    assert shown["Due 2026-09-30"] == "1,200.00 EUR outstanding, 40 days overdue, interest 13.15"
    assert shown["The letter asks for"] == "1,223.15 EUR"
    # Tags out, as plain text.
    assert shown["Letter"] == "Dear Harbor Electric Co-op, our invoice ACC-SINV-2026-00042 is overdue."
    assert listed[0].lines == []


def test_a_long_letter_is_a_marked_preview_and_many_installments_are_counted() -> None:
    summary = {**SUMMARY, "body_text": "word " * 400,
               "rows": [{**SUMMARY["rows"][0], "due_date": f"2026-0{1 + i % 9}-01"} for i in range(25)]}
    view = drafts.card_view("Dunning", summary, {"sales_invoice": "ACC-SINV-2026-00042"})
    letter = next(f["value"] for f in view["fields"] if f["label"] == "Letter")
    assert letter.endswith("…") and len(letter) <= drafts.LETTER_SHOWN_MAX + 1
    assert "And 5 more overdue installments." in view["notes"]


def test_a_letter_whose_invoice_gained_an_installment_closes(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    card = _card(erp, roster)
    erp.replies.append(json.dumps({"ok": False, "error": "posting_date must be after every overdue installment's "
                                                         "due date (the latest is 2026-11-10)."}))
    with pytest.raises(drafts.ApproveRefused) as refused:
        _approve(card)
    assert refused.value.code == "changed_since_proposed"


def test_a_hostile_letter_template_costs_no_time() -> None:
    import time

    started = time.monotonic()
    drafts._letter("<br" + " " * 200_000 + "x" + "<p" + " " * 200_000)
    assert time.monotonic() - started < 1.0


def test_a_future_letter_date_from_erpnext_leaves_no_card() -> None:
    future = (datetime.now(UTC) + timedelta(days=5)).date().isoformat()
    out = drafts.pin_from_check("Dunning", {"sales_invoice": "S"}, {"posting_date": future})
    assert isinstance(out, str) and "future" in out


def test_the_gateway_refuses_a_dunning_without_its_card(erp: FakeErpnext, roster: SimpleNamespace) -> None:
    import asyncio

    from openexecutive.orchestrator import mcp_gateway
    from openexecutive.orchestrator.schedule_tools import set_session

    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None

    async def go() -> str:
        with set_session(_owner_turn(roster, "remind Harbor")):
            return await gateway.call_tool({"name": "erpnext__draft_dunning",
                                            "arguments": {"sales_invoice": "ACC-SINV-2026-00042"}})

    assert "error" in json.loads(asyncio.run(go()))
    assert erp.calls == []

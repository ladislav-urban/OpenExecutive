"""The credit-raise card: "Prepare the raise" on a credit-hold alert
(integrations/erpnext_credit.py, its hooks in erpnext_alert_drafts.py and
erpnext_drafts.py, the draft_recheck_as marker in orchestrator/erpnext_caller.py
and the routes in api/routes/erpnext_drafts.py)."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import PRIVATE_ALERT_TAG
from openexecutive.api.routes import erpnext_drafts as drafts_route
from openexecutive.integrations import erpnext_alert_drafts as alert_drafts
from openexecutive.integrations import erpnext_credit as credit
from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.memory.decision_ledger import get_decision_instance
from openexecutive.orchestrator import erpnext_caller
from openexecutive.orchestrator.erpnext_caller import ASSERTION_ARG
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope

from . import test_delegation_tools, test_erpnext_drafts
from .test_delegation_tools import OWNER, TEAM
from .test_erpnext_drafts import FakeErpnext, _owner_turn, _propose

db = test_delegation_tools.db
fresh_turn_state = test_delegation_tools.fresh_turn_state
erp = test_erpnext_drafts.erp
signed = test_erpnext_drafts.signed
extension = test_erpnext_drafts.extension

CARA = "cara@co.example"
COMPANY = "Aurora: Components"  # a colon in the company, as names may have
ORDER = "SAL-ORD-2026-00123"
CUSTOMER = "Harbor Electric Co-op"
KEY = f"{COMPANY}:{ORDER}"
FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64
# oe_connector's suggest, as alerts._credit_hold_event sends it (8 keys).
SUGGEST = {
    "doctype": "Customer", "operation": "update", "name": CUSTOMER, "company": COMPANY,
    "sales_order": ORDER, "current_limit": 20000.0, "credit_limit": 31000.0, "currency": "USD",
}
FIELDS = {"credit": {"company": COMPANY, "credit_limit": 31000.0, "sales_order": ORDER}}


def _summary(**over: Any) -> dict[str, Any]:
    # draft_changes._credit_summary's shape.
    credit_part = {
        "company": COMPANY, "currency": "USD", "limit_before": 20000.0, "own_limit_before": 20000.0,
        "limit_after": 31000.0, "increase": 11000.0, "row": "changed", "bypass_credit_limit_check": 0,
        "outstanding": 18000.0, "with_order": 30500.0, "order_fits": True,
        **over.pop("credit", {}),
    }
    order = {"name": ORDER, "docstatus": 0, "base_grand_total": 12500.0, **over.pop("sales_order", {})}
    return {
        "operation": "update", "doctype": "Customer", "name": CUSTOMER, "modified": "2026-11-10 09:00",
        "whose": "credit authority", "authority_role": "Sales Master Manager",
        "credit": credit_part, "sales_order": order, "currency": "USD", "company_currency": "USD", **over,
    }


def _checked(fingerprint: str = FINGERPRINT, **over: Any) -> str:
    return json.dumps({"ok": True, "summary": _summary(**over), "fingerprint": fingerprint})


@pytest.fixture
def roster(db: Path) -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    ben = people_store.upsert_person(full_name="Ben Teammate", role="Sales", email=TEAM)
    cara = people_store.upsert_person(full_name="Cara Credit", role="Sales Manager", email=CARA)
    people_store.set_authority_scope(cara, [AuthorityScope.CUSTOMER_CREDIT])
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, ben=ben, cara=cara)


@pytest.fixture
def raises(erp: FakeErpnext, monkeypatch: pytest.MonkeyPatch, db: Path, signed: dict[str, Any]) -> FakeErpnext:
    """Raises on, teammates' drafts on, signed sign-ins (each caller known
    as themselves, ``signed``), the alerts DB."""
    monkeypatch.setenv("ERPNEXT_CREDIT_RAISE", "true")
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    monkeypatch.setattr("openexecutive.api.caller.signing_on", lambda: True)
    monkeypatch.setattr(alert_store, "DB_PATH", db)
    alert_store.initialize_db(db)
    credit._RECHECKED.clear()
    return erp


def _alert(named: list[int], *, key: str = KEY, status: str = "unread", month: str = "2026-11") -> Any:
    # As the pipeline stores a named credit_hold's alert.
    suffix = f":named:{','.join(str(i) for i in sorted(named))}" if named else ""
    alert_id = alert_store.insert_alert(
        severity="medium", headline=f"{key} is over its credit limit", body="", source="erpnext",
        external_id=f"erpnext:credit_hold:{key}:{month}", dedup_key=f"private:erpnext:credit_hold:{key}{suffix}",
        topic_tags=[PRIVATE_ALERT_TAG, *(f"named:{i}" for i in named)],
    )
    assert alert_id is not None
    if status != "unread":
        alert_store.set_status(alert_id, status)
    return alert_store.get_alert(alert_id)


def _held(roster: SimpleNamespace, *, key: str = KEY, suggest: dict[str, Any] | None = None) -> Any:
    raw = suggest or SUGGEST
    assert alert_drafts.remember("credit_hold", key, raw)
    return _alert([roster.ben, roster.cara], key=key)


def _person(person_id: int) -> Any:
    person = people_store.get_person(person_id)
    assert person is not None
    return person


def _prepare(alert: Any, tapper_id: int) -> credit.Prepared:
    return asyncio.run(credit.prepare(alert, caller=None, tapper=_person(tapper_id)))


def _approve(card: Any) -> dict[str, Any]:
    return asyncio.run(drafts.approve(card, caller=None, resolver=card.approver_person_id))


def _card(decision_id: int | None) -> Any:
    assert decision_id is not None
    card = get_decision_instance(decision_id)
    assert card is not None
    return card


def _payload(card: Any) -> dict[str, Any]:
    return json.loads(card.proposed_payload_json)


# --------------------------------------------------------------------------- #
# The suggestion
# --------------------------------------------------------------------------- #


def test_the_real_suggest_shape_keeps_only_what_approval_sends() -> None:
    assert alert_drafts.parse_suggestion("credit_hold", KEY, SUGGEST) == {
        "doctype": "Customer", "operation": "update", "name": CUSTOMER, "company": COMPANY,
        "sales_order": ORDER, "credit_limit": 31000.0,
    }
    # current_limit and currency are optional (display only).
    bare = {k: v for k, v in SUGGEST.items() if k not in ("current_limit", "currency")}
    assert alert_drafts.parse_suggestion("credit_hold", KEY, bare) is not None


@pytest.mark.parametrize("over", [
    {"doctype": "Sales Order"}, {"operation": "create"}, {"credit_limit": True}, {"credit_limit": 0},
    {"credit_limit": -5}, {"credit_limit": math.inf}, {"credit_limit": math.nan},
    {"credit_limit": credit.CREDIT_HIGH}, {"credit_limit": "31000"}, {"currency": "dollars"},
    {"current_limit": "20000"}, {"current_limit": True}, {"name": " padded"}, {"company": ""},
    {"sales_order": "bad\nname"}, {"bypass_credit_limit_check": 1},
])
def test_a_malformed_suggestion_is_dropped(over: dict[str, Any]) -> None:
    assert alert_drafts.parse_suggestion("credit_hold", KEY, {**SUGGEST, **over}) is None


def test_the_suggestion_must_name_the_events_own_key_compared_whole() -> None:
    missing = {k: v for k, v in SUGGEST.items() if k != "sales_order"}
    assert alert_drafts.parse_suggestion("credit_hold", KEY, missing) is None
    assert alert_drafts.parse_suggestion("credit_hold", f"{COMPANY}:SAL-ORD-OTHER", SUGGEST) is None
    # The key is cut at 140 by oe_connector, and compared after the same cut.
    long_company = "C" * 135
    raw = {**SUGGEST, "company": long_company}
    assert alert_drafts.parse_suggestion("credit_hold", f"{long_company}:{ORDER}"[:140], raw) is not None
    assert alert_drafts.parse_suggestion("credit_hold", f"{long_company}:{ORDER}", raw) is None


def test_ref_of_strips_one_named_suffix_and_the_external_id_decides(raises: FakeErpnext, roster: SimpleNamespace) -> None:
    alert = _held(roster)
    assert alert.dedup_key.endswith(f":named:{roster.ben},{roster.cara}")
    assert alert_drafts._ref_of(alert) == (f"erpnext:credit_hold:{KEY}", "credit_hold", KEY)
    assert alert_drafts.suggestion_for(alert) is not None
    # An unnamed one (no reader matched) still reads.
    assert alert_drafts._ref_of(_alert([], month="2026-12")) == (f"erpnext:credit_hold:{KEY}", "credit_hold", KEY)
    # Another external id than the event's: nothing.
    other = SimpleNamespace(source="erpnext", topic_tags=alert.topic_tags, dedup_key=alert.dedup_key,
                            external_id="erpnext:credit_hold:other:2026-11")
    assert alert_drafts._ref_of(other) is None


# --------------------------------------------------------------------------- #
# Who may prepare
# --------------------------------------------------------------------------- #


def test_prepare_is_offered_to_the_named_people_and_the_principal_only(
    raises: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert = _held(roster)
    outsider = people_store.upsert_person(full_name="Otto Outsider", email="otto@co.example")
    label = [(alert.id, "Prepare the raise")]
    assert alert_drafts.open_suggestions() == label
    assert alert_drafts.open_suggestions(viewer=_person(roster.ben)) == label
    assert alert_drafts.open_suggestions(viewer=_person(roster.cara)) == label
    assert alert_drafts.open_suggestions(viewer=_person(outsider)) == []
    with pytest.raises(drafts.ApproveRefused) as refused:
        _prepare(alert, outsider)
    assert refused.value.status == 404
    # Off by default: nobody is offered it.
    monkeypatch.setenv("ERPNEXT_CREDIT_RAISE", "false")
    assert alert_drafts.open_suggestions() == []
    with pytest.raises(drafts.ApproveRefused) as refused:
        _prepare(alert, roster.cara)
    assert refused.value.code == "drafts_off"


def test_a_teammates_tap_is_refused_with_teammates_off_and_the_principals_goes_to_the_principal(
    raises: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert = _held(roster)
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "false")
    assert alert_drafts.open_suggestions(viewer=_person(roster.cara)) == [(alert.id, "Prepare the raise")]
    with pytest.raises(drafts.ApproveRefused) as refused:
        _prepare(alert, roster.cara)
    assert refused.value.code == "principal_only" and raises.calls == []
    raises.replies.append(_checked())
    prepared = _prepare(alert, roster.principal)
    card = _card(prepared.decision_id)
    assert card.approver_person_id == roster.principal
    assert _payload(card)["asked_by_person_id"] == roster.principal


def test_the_check_runs_as_the_tapper_with_code_built_arguments(raises: FakeErpnext, roster: SimpleNamespace) -> None:
    alert = _held(roster)
    raises.replies.append(_checked())
    prepared = _prepare(alert, roster.cara)
    tool, sent = raises.calls[0]
    assert tool == "erpnext__draft_check" and len(raises.calls) == 1
    assert (raises.claims()["sub"], raises.claims()["purpose"]) == (CARA, "draft")
    sent.pop(ASSERTION_ARG)
    assert sent == {"doctype": "Customer", "operation": "update", "name": CUSTOMER, "fields": FIELDS}
    payload = _payload(_card(prepared.decision_id))
    assert payload["operation"] == "credit" and payload["tool"] == "erpnext__draft_update"
    assert payload["fields"] == FIELDS and payload["name"] == CUSTOMER and payload["from_alert"] == alert.id
    assert payload["fingerprint"] == FINGERPRINT
    # Both identities, stored at propose.
    assert (payload["asked_by"], payload["asked_by_person_id"]) == (CARA, roster.cara)
    assert (payload["approver_email"], payload["approver_person_id"]) == (CARA, roster.cara)
    assert payload["view"]["fields"][0] == {"label": "Raise", "value": (
        f"Raise {CUSTOMER}'s credit limit at {COMPANY} from 20,000.00 to 31,000.00 USD (+11,000.00), "
        f"so order {ORDER} (12,500.00) fits; it owes 18,000.00."
    )}
    assert payload["view"]["notes"][-1] == "You'd still submit the order in ERPNext."
    assert payload["why"] == f"Raise {CUSTOMER}'s credit limit for order {ORDER} (ERPNext alert)"


@pytest.mark.parametrize("refusal", list(credit.NEEDS_AUTHORITY))
def test_an_order_owner_without_the_role_gets_no_card_and_who_can_prepare(
    raises: FakeErpnext, roster: SimpleNamespace, refusal: str,
) -> None:
    alert = _held(roster)
    raises.replies.append(json.dumps({"error": f"{refusal}: the Sales Master Manager role in ERPNext."}))
    prepared = _prepare(alert, roster.ben)
    assert prepared.decision_id is None and "Ask Cara Credit to prepare it." in prepared.message
    assert drafts.open_cards(roster.principal) == [] and drafts.open_cards(roster.ben) == []
    # Nobody with the authority in People: the credit controller.
    people_store.set_authority_scope(roster.cara, [])
    raises.replies.append(json.dumps({"error": refusal}))
    assert "Ask your credit controller to prepare it." in _prepare(alert, roster.ben).message


@pytest.mark.parametrize(("over", "said"), [
    ({"credit": {"order_fits": False}}, "still wouldn't fit"),
    ({"credit": {"order_fits": "yes"}}, "incomplete"),
    ({"sales_order": {"docstatus": 1}}, "no longer a draft"),
    ({"sales_order": {"docstatus": True}}, "no longer a draft"),
    ({"credit": {"limit_after": 40000.0}}, "different new limit"),
    ({"credit": {"outstanding": math.inf}}, "incomplete"),
    ({"credit": {"company": "Other Co"}}, "incomplete"),
    ({"sales_order": {"name": "SAL-ORD-OTHER"}}, "incomplete"),
    ({"sales_order": {"owner": 7}}, "incomplete"),
    ({"name": "Another Customer"}, "different customer"),
    ({"operation": "create"}, "different customer"),
])
def test_a_reading_that_isnt_this_raise_leaves_no_card(
    raises: FakeErpnext, roster: SimpleNamespace, over: dict[str, Any], said: str,
) -> None:
    alert = _held(roster)
    raises.replies.append(_checked(**over))
    with pytest.raises(drafts.ApproveRefused) as refused:
        _prepare(alert, roster.cara)
    assert said in refused.value.message and drafts.open_cards(roster.cara) == []


def test_no_fingerprint_no_card_and_the_orders_owner_is_an_optional_extra(
    raises: FakeErpnext, roster: SimpleNamespace,
) -> None:
    alert = _held(roster)
    raises.replies.append(json.dumps({"ok": True, "summary": _summary()}))
    with pytest.raises(drafts.ApproveRefused) as refused:
        _prepare(alert, roster.cara)
    assert "fingerprint" in refused.value.message
    raises.replies.append(_checked(sales_order={"owner": "ben@co.example"}))
    payload = _payload(_card(_prepare(alert, roster.cara).decision_id))
    assert "ben@co.example" not in json.dumps(payload["view"])


# --------------------------------------------------------------------------- #
# Who approves
# --------------------------------------------------------------------------- #


def test_the_approver_in_each_case(raises: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    principal, ben, cara = _person(roster.principal), _person(roster.ben), _person(roster.cara)
    # With the authority and separation off: themselves.
    assert credit.choose_approver(cara, principal) is cara
    assert credit.choose_approver(principal, principal) is principal
    # Without it: the principal.
    assert credit.choose_approver(ben, principal) is principal
    monkeypatch.setenv("ERPNEXT_DRAFT_SOD", "agent")
    # Separation on: never the asker, so the principal; the principal asking leaves none.
    assert credit.choose_approver(cara, principal) is principal
    said = credit.choose_approver(principal, principal)
    assert isinstance(said, str) and "customer credit authority" in said
    alert = _held(roster)
    with pytest.raises(drafts.ApproveRefused) as refused:
        _prepare(alert, roster.principal)
    assert refused.value.code == "no_approver" and raises.calls == []


def test_a_card_left_for_the_principal_says_who_prepared_it(raises: FakeErpnext, roster: SimpleNamespace) -> None:
    alert = _held(roster)
    raises.replies.append(_checked())
    prepared = _prepare(alert, roster.ben)
    card = _card(prepared.decision_id)
    assert card.approver_person_id == roster.principal
    assert "Olivia Owner" in prepared.message
    payload = _payload(card)
    assert {"label": "Prepared by", "value": "Ben Teammate"} in payload["view"]["fields"]
    assert (payload["asked_by"], payload["approver_email"]) == (TEAM, OWNER)
    (listed,) = drafts.cards(_person(roster.principal))
    assert listed.operation == "credit"


def test_one_card_per_alert_across_approvers_and_a_cap_per_asker(raises: FakeErpnext, roster: SimpleNamespace) -> None:
    alert = _held(roster)
    raises.replies.append(_checked())
    first = _prepare(alert, roster.ben)
    # A second named person's tap is that card, unchecked, and no button shows.
    second = _prepare(alert, roster.cara)
    assert (second.decision_id, second.new) == (first.decision_id, False) and len(raises.calls) == 1
    assert alert_drafts.open_suggestions(viewer=_person(roster.cara)) == []
    # Ben may leave at most PER_ASKER_MAX raises waiting on someone else.
    for number in range(1, credit.PER_ASKER_MAX):
        order = f"SAL-ORD-2026-0090{number}"
        other = _held(roster, key=f"{COMPANY}:{order}", suggest={**SUGGEST, "sales_order": order})
        raises.replies.append(_checked(sales_order={"name": order}))
        assert _prepare(other, roster.ben).new is True
    order = "SAL-ORD-2026-00999"
    extra = _held(roster, key=f"{COMPANY}:{order}", suggest={**SUGGEST, "sales_order": order})
    with pytest.raises(drafts.ApproveRefused) as refused:
        _prepare(extra, roster.ben)
    assert refused.value.code == "too_many"


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


def _cara_card(raises: FakeErpnext, roster: SimpleNamespace) -> Any:
    raises.replies.append(_checked())
    return _card(_prepare(_held(roster), roster.cara).decision_id)


def test_approval_sends_exactly_the_raise_as_the_approver(raises: FakeErpnext, roster: SimpleNamespace) -> None:
    card = _cara_card(raises, roster)
    raises.replies.append(json.dumps({"ok": True, "operation": "update", "doctype": "Customer", "name": CUSTOMER,
                                      "already_done": False, "docstatus": 0}))
    made = _approve(card)
    tool, sent = raises.calls[-1]
    assert tool == "erpnext__draft_update"
    assert (raises.claims()["sub"], raises.claims()["purpose"]) == (CARA, "draft")
    sent.pop(ASSERTION_ARG)
    assert sent == {
        "doctype": "Customer", "name": CUSTOMER, "credit": FIELDS["credit"], "idempotency_key": card.idempotency_key,
        "asked_by": CARA, "expected_fingerprint": FINGERPRINT,
    }
    assert made["operation"] == "credit" and made["name"] == CUSTOMER
    assert made["credit"] == {"company": COMPANY, "limit": "31,000.00 USD"}


def test_a_raise_prepared_by_someone_else_is_sent_as_the_approver_naming_the_asker(
    raises: FakeErpnext, roster: SimpleNamespace,
) -> None:
    raises.replies.append(_checked())
    card = _card(_prepare(_held(roster), roster.ben).decision_id)
    raises.replies.append(json.dumps({"ok": True, "operation": "update", "doctype": "Customer", "name": CUSTOMER,
                                      "docstatus": 0}))
    _approve(card)
    assert raises.claims()["sub"] == OWNER and raises.calls[-1][1]["asked_by"] == TEAM


@pytest.mark.parametrize("refusal", [
    f"{drafts.CHANGED_SINCE_PROPOSED}: the document is not what the card showed.",
    f"{credit.SOD_REFUSED}: you asked for this credit-limit raise, so someone else approves it.",
])
def test_changed_since_proposed_and_sod_refused_close_the_card(
    raises: FakeErpnext, roster: SimpleNamespace, refusal: str,
) -> None:
    card = _cara_card(raises, roster)
    raises.replies.append(json.dumps({"error": refusal}))
    with pytest.raises(drafts.ApproveRefused) as refused:
        _approve(card)
    assert refused.value.code in ("changed_since_proposed", "separation_of_duties")
    assert _card(card.id).status == "failed" and drafts.open_cards(roster.cara) == []


def _refused(card: Any) -> drafts.ApproveRefused:
    with pytest.raises(drafts.ApproveRefused) as refused:
        _approve(card)
    return refused.value


def test_approval_refuses_when_the_switch_is_off(
    raises: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _cara_card(raises, roster)
    monkeypatch.setenv("ERPNEXT_CREDIT_RAISE", "false")
    assert _refused(card).code == "drafts_off" and raises.calls[-1][0] == "erpnext__draft_check"


def test_approval_refuses_an_approver_who_lost_the_authority_or_is_a_demo_person(
    raises: FakeErpnext, roster: SimpleNamespace, db: Path,
) -> None:
    import sqlite3

    card = _cara_card(raises, roster)
    people_store.set_authority_scope(roster.cara, [])
    assert _refused(card).code == "no_authority"
    people_store.set_authority_scope(roster.cara, [AuthorityScope.CUSTOMER_CREDIT])
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE people SET directory_demo = 1 WHERE id = ?", (roster.cara,))
    assert _refused(card).code == "demo"
    assert all(tool == "erpnext__draft_check" for tool, _ in raises.calls)


def test_approval_refuses_a_renamed_approver_or_asker(raises: FakeErpnext, roster: SimpleNamespace) -> None:
    raises.replies.append(_checked())
    card = _card(_prepare(_held(roster), roster.ben).decision_id)
    # The asker's People email changed: the card was checked as someone else.
    people_store.upsert_person(person_id=roster.ben, full_name="Ben Teammate", email="ben.new@co.example")
    assert _refused(card).code == "changed"
    people_store.upsert_person(person_id=roster.ben, full_name="Ben Teammate", email=TEAM)
    # The approver's email changed.
    people_store.upsert_person(person_id=roster.principal, full_name="Olivia Owner", is_principal=True,
                               email="olivia.new@co.example")
    assert _refused(card).code in ("changed", "not_yours")
    assert all(tool == "erpnext__draft_check" for tool, _ in raises.calls)


def test_approval_refuses_anyone_but_the_cards_approver(raises: FakeErpnext, roster: SimpleNamespace) -> None:
    from openexecutive.memory.decision_ledger import update_open_payload

    card = _cara_card(raises, roster)
    payload = _payload(card)
    assert update_open_payload(card.id, {**payload, "approver_person_id": roster.principal},
                               expected_json=card.proposed_payload_json)
    assert _refused(_card(card.id)).code == "not_yours"


def test_separation_on_refuses_the_asker_approving_their_own(
    raises: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _cara_card(raises, roster)
    monkeypatch.setenv("ERPNEXT_DRAFT_SOD", "agent")
    assert _refused(card).code == "separation_of_duties"


def test_the_model_never_proposes_a_raise(raises: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFT_UPDATE_DOCTYPES", "Purchase Invoice,Customer")
    tool = drafts.draft_tools()[0]
    assert "Customer" not in tool["input_schema"]["properties"]["doctype"]["enum"]
    assert "credit" not in tool["input_schema"]["properties"]["operation"]["enum"]
    for operation, doctype, fields in (
        ("credit", "Customer", FIELDS),
        ("update", "Customer", {"set": {"credit_limit": 1}}),
        ("update", "Customer", FIELDS),
    ):
        result = _propose(_owner_turn(SimpleNamespace(principal=roster.principal)), {
            "why": "raise it", "doctype": doctype, "operation": operation, "name": CUSTOMER, "fields": fields,
        })
        assert "error" in result
    assert raises.calls == []


# --------------------------------------------------------------------------- #
# Read again on open
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    app = FastAPI()
    app.include_router(drafts_route.router)
    return TestClient(app)


def test_the_card_list_marks_drift_and_approval_refuses_it(
    raises: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], client: TestClient,
) -> None:
    card = _cara_card(raises, roster)
    calls = len(raises.calls)
    # Home's count reads only the database.
    assert drafts_route._waiting_drafts(_person(roster.cara)) == 1 and len(raises.calls) == calls
    raises.replies.append(_checked(fingerprint=OTHER_FINGERPRINT))
    (shown,) = client.get("/erpnext/drafts", headers={"x-caller-email": CARA}).json()["cards"]
    assert shown["changed"] is True and shown["operation"] == "credit"
    tool, sent = raises.calls[-1]
    assert tool == "erpnext__draft_check" and raises.claims()["sub"] == CARA
    sent.pop(ASSERTION_ARG)
    assert sent == {"doctype": "Customer", "operation": "update", "name": CUSTOMER, "fields": FIELDS}
    # Cached a minute: no second read.
    assert client.get("/erpnext/drafts", headers={"x-caller-email": CARA}).json()["cards"][0]["changed"] is True
    assert len(raises.calls) == calls + 1
    # The GET changed nothing; approval refuses and closes it, sending nothing.
    assert _card(card.id).status == "proposed"
    assert _refused(card).code == "changed_since_proposed"
    assert len(raises.calls) == calls + 1 and drafts.open_cards(roster.cara) == []


def test_the_fresh_read_is_the_approvers_own_and_errors_change_nothing(
    raises: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], client: TestClient,
) -> None:
    _cara_card(raises, roster)
    calls = len(raises.calls)
    # Not known to be Cara: nothing is read.
    signed["refusal"] = "caller_signing_required"
    assert client.get("/erpnext/drafts", headers={"x-caller-email": CARA}).json()["cards"][0]["changed"] is False
    assert len(raises.calls) == calls
    signed["refusal"] = None
    raises.replies.append(json.dumps({"error": "ERPNext is busy"}))
    assert client.get("/erpnext/drafts", headers={"x-caller-email": CARA}).json()["cards"][0]["changed"] is False
    # A read that matches marks nothing.
    raises.replies.append(_checked())
    assert client.get("/erpnext/drafts", headers={"x-caller-email": CARA}).json()["cards"][0]["changed"] is False
    assert len(raises.calls) == calls + 2


def test_the_fresh_read_is_capped_least_recently_read_first(
    raises: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    ids = []
    for number in range(credit.RECHECK_PER_REQUEST + 1):
        order = f"SAL-ORD-2026-0080{number}"
        alert = _held(roster, key=f"{COMPANY}:{order}", suggest={**SUGGEST, "sales_order": order})
        raises.replies.append(_checked(sales_order={"name": order}))
        ids.append(_prepare(alert, roster.cara).decision_id)
    cara = _person(roster.cara)
    calls = len(raises.calls)
    listed = drafts.cards(cara)
    asyncio.run(credit.recheck(cara, listed, None))
    assert len(raises.calls) == calls + credit.RECHECK_PER_REQUEST
    first = set(credit._RECHECKED)
    (left,) = set(ids) - first
    # Next time (once those are stale) the one not read yet goes first.
    for read in credit._RECHECKED.values():
        read.at -= credit.RECHECK_TTL_S + 1
    asyncio.run(credit.recheck(cara, drafts.cards(cara), None))
    assert credit._RECHECKED[left].approver_id == roster.cara
    assert len(raises.calls) == calls + 2 * credit.RECHECK_PER_REQUEST
    # Served only to its approver.
    assert not credit.drifted(left, roster.principal)


def test_the_recheck_marker_covers_only_its_own_call() -> None:
    args = {"doctype": "Customer", "operation": "update", "name": CUSTOMER, "fields": FIELDS}
    with erpnext_caller.draft_recheck_as(args, CARA):
        marker = erpnext_caller.draft_call_for("erpnext__draft_check", args)
        assert marker is not None and marker.caller == erpnext_caller.ErpnextCaller("user", CARA)
        assert erpnext_caller.draft_call_for("erpnext__draft_update", args) is None
        assert erpnext_caller.draft_call_for("erpnext__draft_check", {**args, "name": "Other"}) is None
    assert erpnext_caller.draft_call_for("erpnext__draft_check", args) is None
    with pytest.raises(ValueError):
        erpnext_caller.draft_recheck_as(args, "nobody")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def test_the_routes_offer_and_take_a_named_persons_tap(
    raises: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert = _held(roster)
    outsider = {"x-caller-email": "otto@co.example"}
    people_store.upsert_person(full_name="Otto Outsider", email="otto@co.example")
    cara = {"x-caller-email": CARA}
    assert client.get("/erpnext/alert-drafts", headers=cara).json()["suggestions"] == [
        {"alert_id": alert.id, "label": "Prepare the raise"},
    ]
    assert client.get("/erpnext/alert-drafts", headers=outsider).json()["suggestions"] == []
    assert client.post(f"/erpnext/alert-drafts/{alert.id}", headers=outsider).status_code == 404
    # Without signed sign-ins a teammate gets no button.
    monkeypatch.setattr("openexecutive.api.caller.signing_on", lambda: False)
    assert client.get("/erpnext/alert-drafts", headers=cara).json()["suggestions"] == []
    monkeypatch.setattr("openexecutive.api.caller.signing_on", lambda: True)
    raises.replies.append(json.dumps({"error": credit.NEEDS_AUTHORITY[0]}))
    reply = client.post(f"/erpnext/alert-drafts/{alert.id}", headers={"x-caller-email": TEAM})
    assert reply.status_code == 200 and reply.json()["decision_id"] is None
    assert "Cara Credit" in reply.json()["message"]
    raises.replies.append(_checked())
    reply = client.post(f"/erpnext/alert-drafts/{alert.id}", headers=cara)
    assert reply.status_code == 200 and reply.json()["already_waiting"] is False
    assert [c.id for c in drafts.open_cards(roster.cara)] == [reply.json()["decision_id"]]

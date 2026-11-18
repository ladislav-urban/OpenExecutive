"""What GET /today/activity shows each caller (today._activity_audience).

The principal reads the whole feed. A teammate reads their own rows (the
scheduled actions they queued, the runs they started, the cards routed to
them) and what is company-public (decisions, initiatives, team runs, shared
alerts). A demo registrant reads only their own rows. A signed-in email on no
People entry, and a call with no caller while signed callers are on, read
nothing. Nobody is refused: the demo Home reads this route.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.memory import decision_ledger, episodic
from openexecutive.workflows import persistence as wf_persistence

from .principal_gate_support import app_client, call, make_roster

PATH = "/today/activity?limit=100"

DECISION = "Decided to open the Ohio office"
INITIATIVE = "Warehouse move"
ADVICE = "Raise prices before the renewal round"
NUDGE = "Reminded about the board pack"
PRINCIPAL_ACTION = "Follow up with the bank on the loan"
TEAMMATE_ACTION = "Send Ben's supplier recap"
DEMO_ACTION = "Send Dana the trial checklist"
TEAM_RUN = "Market scan for Q4"
TEAMMATE_RUN = "Ben's competitor teardown"
DEMO_RUN = "Dana's sample report"
PRINCIPAL_CARD = "Book the investor dinner"
TEAMMATE_CARD = "Order the new laptops"
DEMO_CARD = "Approve the demo invoice"
SHARED_ALERT = "Stock is low on SKU 42"
DEMO_ALERT = "Your demo quote is ready"

EVERY_ROW = {
    DECISION, INITIATIVE, ADVICE, NUDGE, PRINCIPAL_ACTION, TEAMMATE_ACTION, DEMO_ACTION,
    TEAM_RUN, TEAMMATE_RUN, DEMO_RUN, PRINCIPAL_CARD, TEAMMATE_CARD, DEMO_CARD,
    SHARED_ALERT, DEMO_ALERT,
}


def _action(text: str, by: int | None, *, kind: str = "ad_hoc") -> None:
    episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="email", channel_ref="someone@co.example",
        intent_text=text, kind=kind, status="done", created_by_person_id=by,
        scope_key="nudge:test:1" if kind == "proactive_nudge" else None,
    )


def _run(run_id: str, title: str, owner: int | None) -> None:
    wf_persistence.create_run(run_id, "custom_flow", title, {}, owner_person_id=owner)
    wf_persistence.complete_run(run_id, "")


def _card(title: str, approver: int) -> None:
    instance = decision_ledger.create_decision_instance(
        decision_class="test_class", department="", originating_session_id=None,
        proposed_payload={"title": title}, idempotency_key=None, gate_mode="propose",
        approver_person_id=approver, confidence=None,
    )
    assert decision_ledger.mark_resolved(instance, "approved_unchanged", resolver_person_id=approver)


def _alert(headline: str, routed_to: int | None) -> None:
    alerts_store.insert_alert(
        source="test", external_id=headline, severity="medium", headline=headline,
        body="", routed_to_person_id=routed_to,
    )


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    ids = make_roster(tmp_path, monkeypatch)
    alerts_store.initialize_db()
    episodic.store_decision(domain="strategy", summary=DECISION)
    episodic.store_initiative(INITIATIVE, "active")
    episodic.store_advice("strategy", "Should we raise prices?", ADVICE)
    _action(NUDGE, None, kind="proactive_nudge")
    _action(PRINCIPAL_ACTION, ids["principal"])
    _action(TEAMMATE_ACTION, ids["teammate"])
    _action(DEMO_ACTION, ids["demo"])
    _run("run-team", TEAM_RUN, None)
    _run("run-teammate", TEAMMATE_RUN, ids["teammate"])
    _run("run-demo", DEMO_RUN, ids["demo"])
    _card(PRINCIPAL_CARD, ids["principal"])
    _card(TEAMMATE_CARD, ids["teammate"])
    _card(DEMO_CARD, ids["demo"])
    _alert(SHARED_ALERT, None)
    _alert(DEMO_ALERT, ids["demo"])
    return ids


def _feed(monkeypatch: pytest.MonkeyPatch, signed: bool, who: str) -> set[str]:
    client = app_client(monkeypatch, signed=signed)
    r = call(client, signed, who, "GET", PATH)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    found = {i["summary"] for i in items}
    # A resolved card reads "Approved: <title>"; name it by its title.
    return {s.removeprefix("Approved: ") for s in found} & EVERY_ROW


@pytest.mark.parametrize("signed", [False, True])
def test_the_principal_reads_the_whole_feed(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    # A run a teammate started is theirs alone, even to the principal.
    assert _feed(monkeypatch, signed, "principal") == EVERY_ROW - {TEAMMATE_RUN, DEMO_RUN}


@pytest.mark.parametrize("signed", [False, True])
def test_a_teammate_reads_their_own_rows_and_the_company_public_ones(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    assert _feed(monkeypatch, signed, "teammate") == {
        DECISION, INITIATIVE, TEAMMATE_ACTION, TEAM_RUN, TEAMMATE_RUN, TEAMMATE_CARD, SHARED_ALERT,
    }


@pytest.mark.parametrize("signed", [False, True])
def test_a_demo_registrant_reads_only_their_own_rows(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    assert _feed(monkeypatch, signed, "demo") == {DEMO_ACTION, DEMO_RUN, DEMO_CARD, DEMO_ALERT}


@pytest.mark.parametrize("signed", [False, True])
def test_an_email_on_no_people_entry_reads_nothing(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    assert _feed(monkeypatch, signed, "stranger") == set()


def test_a_call_with_no_caller_reads_nothing_when_callers_are_signed(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _feed(monkeypatch, True, "unsigned") == set()


def test_local_login_reads_the_whole_feed(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _feed(monkeypatch, True, "operator") == EVERY_ROW - {TEAMMATE_RUN, DEMO_RUN}


def test_a_suspended_teammate_reads_only_their_own_rows(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    assert people_store.suspend_person(ids["teammate"], by="principal")
    people_registry.invalidate()
    assert _feed(monkeypatch, False, "teammate") <= {TEAMMATE_ACTION, TEAMMATE_RUN, TEAMMATE_CARD}

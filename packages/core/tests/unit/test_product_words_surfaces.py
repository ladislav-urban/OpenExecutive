# ruff: noqa: F811 — pytest fixtures imported from test_answer_check share their names with the test parameters.
"""What code adds to a reply or a person-facing surface names the product's
parts in the product's words when the install has a name (OE_PRODUCT_NAME,
an eval's product_name_override): "the ERP", never "ERPNext". Eval
erpnext_094 (Nov 18, 2026): the answer check's projection footer said
"checked against ERPNext's records" in 14 of 15 runs. Without a name every
string stays as it was.

Made-up companies, customers and amounts."""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from openexecutive.integrations import channel_cards
from openexecutive.memory.decision_ledger import DecisionInstance
from openexecutive.orchestrator import answer_check, answer_correct, schedule_tools
from openexecutive.orchestrator.answer_review import CANNOT_TELL, Review
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.session import Session
from openexecutive.people.models import Person
from openexecutive.scheduler import runner
from openexecutive.utils.product_name import product_name_override

from .test_answer_check import _reviews, _rewrites, _turn, audit_rows  # noqa: F401

PACKAGE = Path(__file__).resolve().parents[2] / "openexecutive"
OUTLOOK = (
    "Receivables come to $48,200.\n\n"
    "| Week | Cash |\n"
    "|---|---|\n"
    "| Week 1 | $12,900 |\n"
    "| Week 2 | $37,560 |\n"
)
PROJECTION = "What will cash look like week by week for the next 13 weeks?"
# A call that asks Executive.chat for the model's own words.
_TAKES_OWN_WORDS = re.compile(r"^\s*product_words=False\b", re.M)


@pytest.fixture
def named() -> Iterator[None]:
    token = product_name_override.set("Dynocortex")
    yield
    product_name_override.reset(token)


@pytest.fixture
def unnamed() -> Iterator[None]:
    # An override with no usable name is an install without one.
    token = product_name_override.set("")
    yield
    product_name_override.reset(token)


def _ask(text: str, question: str) -> str:
    return asyncio.run(answer_check.check_reply(text, _turn(), question=question, model="m"))


# -- the answer check's own sentences ---------------------------------------------


def test_the_projection_note_is_in_the_products_words(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any], named: None,
) -> None:
    _reviews(monkeypatch, Review("wrong", 12500.0), *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    out = _ask(OUTLOOK, PROJECTION)
    assert "ERPNext" not in out
    assert out.endswith("_The projected figures above are estimates; they couldn't be checked against the ERP's records._")
    assert "$12,500" in out


def test_the_projection_note_is_unchanged_without_a_name(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any], unnamed: None,
) -> None:
    _reviews(monkeypatch, Review("wrong", 12500.0), *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    assert _ask(OUTLOOK, PROJECTION).endswith(answer_check.PROJECTION_NOTE)
    assert "ERPNext's records" in answer_check.PROJECTION_NOTE


def test_the_whole_reply_sentence_is_in_the_products_words(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any], named: None,
) -> None:
    monkeypatch.setenv("ERPNEXT_ANSWER_CHECK", "correct")
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    turn = _turn()
    text = "Receivables are $77,777. Most of it is South's."
    out = asyncio.run(answer_check.check_reply(text, turn, question="Who owes us?", model="m"))
    assert out == "I couldn't get those figures from the ERP reliably. Open the report to see them."
    assert turn.whole_replaced
    # The record keeps the decided sentence; a second call returns the same.
    assert asyncio.run(answer_check.check_reply(text, turn, question="Who owes us?", model="m")) == out


def test_the_whole_reply_sentence_is_unchanged_without_a_name(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any], unnamed: None,
) -> None:
    monkeypatch.setenv("ERPNEXT_ANSWER_CHECK", "correct")
    _reviews(monkeypatch, *[CANNOT_TELL] * 10)
    _rewrites(monkeypatch, None)
    assert _ask("Receivables are $77,777. Most of it is South's.", "Who owes us?") == answer_correct.WHOLE_REPLY


# -- every chat app's reply: Executive.chat ---------------------------------------

REPLY = "Metro owes $4,710, as recorded in ERPNext."


def _chat(**kwargs: Any) -> str:
    async def stream(**_: Any) -> Any:
        yield REPLY

    executive = Executive.__new__(Executive)
    executive.stream_chat = stream  # type: ignore[method-assign]
    return asyncio.run(executive.chat(user_message="Who owes us?", session=Session(), **kwargs))


def test_a_chat_apps_reply_is_in_the_products_words(named: None) -> None:
    assert _chat() == "Metro owes $4,710, as recorded in the ERP."
    # The evals judge the model's own words (erpnext_027).
    assert _chat(product_words=False) == REPLY


def test_a_chat_apps_reply_is_unchanged_without_a_name(unnamed: None) -> None:
    assert _chat() == REPLY


@pytest.mark.parametrize(
    "path",
    [
        "integrations/slack_bot.py",
        "integrations/telegram_bot.py",
        "integrations/discord_bot.py",
        "integrations/google_chat.py",
        "integrations/email_poller.py",
        "integrations/erpnext_panel.py",
        "scheduler/runner.py",
        "mcp_server/server.py",
    ],
)
def test_every_channel_takes_chats_reply_in_the_products_words(path: str) -> None:
    source = (PACKAGE / path).read_text(encoding="utf-8")
    assert re.search(r"\.chat\(", source), f"{path} no longer answers through Executive.chat"
    assert not _TAKES_OWN_WORDS.search(source)


def test_only_the_evals_take_the_models_own_words() -> None:
    takers = sorted(
        str(p.relative_to(PACKAGE)).replace("\\", "/")
        for p in PACKAGE.rglob("*.py")
        if _TAKES_OWN_WORDS.search(p.read_text(encoding="utf-8"))
    )
    assert takers == ["evals/runner.py"]


# -- what the scheduler sends a person -------------------------------------------


def _telegram(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []

    async def fake_send(payload: dict[str, Any]) -> str:
        sent.append(payload["text"])
        return json.dumps({"status": "sent", "message_id": 1})

    monkeypatch.setattr(schedule_tools, "handle_send_telegram_message", fake_send)
    monkeypatch.setattr("openexecutive.integrations.conversations.record_delivery", lambda *a, **k: None)
    return sent


def test_an_alert_push_carries_its_record_values_as_read(monkeypatch: pytest.MonkeyPatch, named: None) -> None:
    # Security re-review of this branch: the send path reworded every text,
    # record values in an alert included.
    from openexecutive.alerts.dispatcher import _format_broadcast_body
    from openexecutive.alerts.models import Alert

    sent = _telegram(monkeypatch)
    alert = Alert(
        id=7, source="web_form", external_id="f-1", severity="high", headline="New enquiry from Open Executive Support",
        body="Customer: ERPNext Foundation\nAsks about OpenExecutive pricing.", suggested_action="",
        created_at="2026-11-18T00:00:00+00:00",
    )
    text = _format_broadcast_body(alert)
    person = Person(id=7, full_name="Dana", is_principal=True, telegram_chat_id="4242")
    delivery = asyncio.run(runner.deliver_to_person(person, text, label="Alert", chat_only=True))
    assert delivery.ok and sent == [text]
    assert "Open Executive Support" in sent[0] and "Customer: ERPNext Foundation" in sent[0]


def test_a_brief_is_in_the_products_words_and_a_held_message_as_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: None,
) -> None:
    from . import test_principal_briefs as briefs

    delivered: list[str] = []

    async def deliver(text: str, **_kw: object) -> runner.PrincipalDelivery:
        delivered.append(text)
        return runner.PrincipalDelivery(True, "telegram → 1", "delivered", "telegram")

    monkeypatch.setattr(
        runner, "_with_held_messages",
        lambda kind, artifact: (f"{artifact}\n\nWhile you were away\n- From ERPNext Foundation: call me.", []),
    )
    workflow = briefs._fake_brief_workflow("fp-1", "Receivables are up, as ERPNext's reports show.")
    briefs._run_brief(tmp_path, monkeypatch, workflow=workflow, deliver=deliver)
    assert delivered == [
        "Receivables are up, as the ERP's reports show.\n\n"
        "While you were away\n- From ERPNext Foundation: call me."
    ]


def test_a_report_of_stored_titles_goes_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: None,
) -> None:
    # Security check of this branch: the weekly review is code's rows of stored
    # area and project titles, so the runner sends it as written: the real
    # weekly-review action, the set unpatched (review of 60963a02).
    from . import test_principal_briefs as briefs

    # The weekly review is solo only; this test is about its wording.
    monkeypatch.setattr(runner, "_team_for_sure", lambda kind: False)
    delivered: list[str] = []

    async def deliver(text: str, **_kw: object) -> runner.PrincipalDelivery:
        delivered.append(text)
        return runner.PrincipalDelivery(True, "telegram → 1", "delivered", "telegram")

    monkeypatch.setattr(runner, "_with_held_messages", lambda kind, artifact: (artifact, []))
    workflow = briefs._fake_brief_workflow("fp-2", "### ERPNext rollout\n- **Migrate invoices to ERPNext**")
    briefs._run_brief(tmp_path, monkeypatch, workflow=workflow, deliver=deliver, kind=runner.WEEKLY_REVIEW_KIND)
    assert delivered == ["### ERPNext rollout\n- **Migrate invoices to ERPNext**"]


def test_the_clients_digest_is_sent_as_given() -> None:
    # Code builds the digest from stored client names and roles: no rewording.
    import inspect

    source = inspect.getsource(runner)
    start = source.index('label="Across your clients"')
    assert "in_product_reply" not in source[start - 400 : start]


def _metric_line(monkeypatch: pytest.MonkeyPatch) -> str | None:
    from datetime import date

    from openexecutive.briefing import erp_metrics, erp_numbers

    async def compute(report: Any, company: str, today: Any) -> str:
        return "41 days."

    monkeypatch.setitem(erp_metrics.METRICS, "dso", erp_metrics.Metric("Days sales outstanding", "", compute))
    monkeypatch.setattr(erp_numbers, "brief_company", lambda: "ERPNext Foundation")
    monkeypatch.setattr(erp_metrics, "_connected", lambda: object())
    return asyncio.run(erp_metrics.compute("dso", date(2026, 11, 18)))


def test_a_metric_line_is_in_the_products_words_and_the_company_as_read(
    monkeypatch: pytest.MonkeyPatch, named: None,
) -> None:
    assert _metric_line(monkeypatch) == "Days sales outstanding (ERPNext Foundation, from the ERP's own reports): 41 days."


def test_a_metric_line_is_unchanged_without_a_name(monkeypatch: pytest.MonkeyPatch, unnamed: None) -> None:
    assert _metric_line(monkeypatch) == "Days sales outstanding (ERPNext Foundation, from ERPNext's own reports): 41 days."


# -- a card in a chat app ---------------------------------------------------------


def _card(monkeypatch: pytest.MonkeyPatch, decision_class: str, payload: dict[str, Any]) -> str:
    instance = DecisionInstance(
        id=5, decision_class=decision_class, created_at="2026-11-18T00:00:00+00:00", department="executive",
        originating_session_id=None, proposed_payload_json=json.dumps(payload), idempotency_key=None,
        gate_mode="propose", approver_person_id=1, confidence=None, status="proposed", resolved_at=None,
        resolver_person_id=None, final_payload_json=None, external_event_id=None, reversal_reason=None, severity="",
    )
    from openexecutive.api.routes import decisions
    from openexecutive.memory import decision_ledger

    monkeypatch.setattr(decision_ledger, "get_decision_instance", lambda _id: instance)
    monkeypatch.setattr(decisions, "shown_in_chat", lambda _i: True)
    text = channel_cards.card_text(5)
    assert text is not None
    return text


def test_a_card_shows_what_a_tap_sends_or_saves_as_stored(monkeypatch: pytest.MonkeyPatch, named: None) -> None:
    # Security review of this branch: a reworded subject would show one
    # thing while the stored one goes out.
    reply = _card(monkeypatch, "delegation_reply", {
        "from_name": "ERPNext Support", "draft_to": ["erpnext@vendor.example"],
        "draft_subject": "Re: ERPNext migration", "draft_body": "I've asked for your ERPNext login.",
    })
    for stored in ("To ERPNext Support", "erpnext@vendor.example", "\nRe: ERPNext migration\n",
                   "I've asked for your ERPNext login."):
        assert stored in reply
    meeting = _card(monkeypatch, "meeting_scheduling", {
        "title": "ERPNext rollout review", "start": "2026-11-20T10:00:00", "end": "2026-11-20T11:00:00",
        "attendee_emails": ["erpnext-team@acme.example"],
    })
    assert "ERPNext rollout review" in meeting and "erpnext-team@acme.example" in meeting
    invite = _card(monkeypatch, "delegation_invite", {"title": "ERPNext training", "from_name": "Sam"})
    assert "ERPNext training" in invite
    saved = _card(monkeypatch, "app_action", {"card": "Rename the workflow to ERPNext month end"})
    assert "Rename the workflow to ERPNext month end" in saved
    held = _card(monkeypatch, "department_action", {
        "tool": "google_workspace__send_gmail_message",
        "input": {"to": "dana@acme.example", "subject": "Close", "body": "Post it in ERPNext today."},
        "summary": "Email Dana about the ERPNext close", "reason": "it names ERPNext's figures",
    })
    assert "Email Dana about the ERPNext close" in held and "It waited because it names ERPNext's figures." in held
    assert "Post it in ERPNext today." in held


def test_a_taps_refusal_is_in_the_products_words(named: None) -> None:
    said = channel_cards.outcome_text(True, "This draft changed in ERPNext since the card was left.")
    assert said == "Not done: This draft changed in the ERP since the card was left."


def test_a_taps_refusal_is_unchanged_without_a_name(unnamed: None) -> None:
    said = channel_cards.outcome_text(True, "This draft changed in ERPNext since the card was left.")
    assert said == "Not done: This draft changed in ERPNext since the card was left."


# -- a watch job's notices --------------------------------------------------------


def _debtor_notice() -> str:
    from openexecutive.watch import registry
    from openexecutive.watch.jobs import collections
    from openexecutive.watch.runner import notice_body

    spec = registry.CardSpec(
        key="overdue", summary="Customers to chase",
        items=(registry.CardSpec(
            key="customer:ERPNext Foundation", summary="ERPNext Foundation",
            values=(("Overdue", "4,710.00 USD"),),
        ),),
        notes=("Chase them in ERPNext.",),
    )
    return notice_body(collections.JOB, "2026-11-18", spec, "")


def test_a_notice_keeps_erpnexts_record_values_and_words_its_own(named: None) -> None:
    body = _debtor_notice()
    # The debtor's name is ERPNext's record, shown as read.
    assert "1. ERPNext Foundation" in body and "the ERP Foundation" not in body
    # Code's own preamble and notes are the product's words.
    assert "found this in the ERP on 2026-11-18" in body and "Chase them in the ERP." in body


def test_a_notice_is_unchanged_without_a_name(unnamed: None) -> None:
    body = _debtor_notice()
    assert "found this in ERPNext on 2026-11-18" in body and "Chase them in ERPNext." in body


# -- the product name itself ------------------------------------------------------


@pytest.mark.parametrize("name", ["OpenExecutive Cloud", "Open Executive", "Acme ERPNext Suite", "ERPNext"])
def test_a_name_that_holds_a_part_is_treated_as_unset(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.prompts.connected_systems import in_product_reply
    from openexecutive.utils import product_name as product

    # Read the warning off the logger call, not caplog: in a full run other
    # tests may change logging's handlers or levels.
    warned: list[str] = []
    monkeypatch.setattr(product.logger, "warning", lambda msg, *args, **kw: warned.append(msg % args))
    product._WARNED.discard(name)
    token = product_name_override.set(name)
    try:
        assert product.configured_product_name() is None
        assert any("treated as unset" in w for w in warned)
        assert in_product_reply(REPLY) == REPLY
    finally:
        product_name_override.reset(token)


@pytest.mark.parametrize("name", ["Dynocortex", "Acme Desk", "ERP Cloud"])
def test_a_second_pass_changes_nothing(name: str) -> None:
    from openexecutive.prompts.connected_systems import in_product_reply

    token = product_name_override.set(name)
    try:
        text = "ERPNext's records, in Open Executive and OpenExecutive. The ERPNext list. This ERPNext user."
        once = in_product_reply(text)
        assert once != text and in_product_reply(once) == once
    finally:
        product_name_override.reset(token)

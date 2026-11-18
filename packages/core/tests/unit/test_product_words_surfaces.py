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


# -- what the scheduler sends the principal ---------------------------------------


def _send(monkeypatch: pytest.MonkeyPatch, text: str) -> str:
    sent: list[str] = []

    async def fake_send(payload: dict[str, Any]) -> str:
        sent.append(payload["text"])
        return json.dumps({"status": "sent", "message_id": 1})

    monkeypatch.setattr(schedule_tools, "handle_send_telegram_message", fake_send)
    monkeypatch.setattr("openexecutive.integrations.conversations.record_delivery", lambda *a, **k: None)
    principal = Person(id=7, full_name="Dana", is_principal=True, telegram_chat_id="4242")
    delivery = asyncio.run(runner._send_on_plan(principal, ["telegram"], text, label="DSO"))
    assert delivery.ok
    return sent[0]


def test_a_scheduled_message_is_in_the_products_words(monkeypatch: pytest.MonkeyPatch, named: None) -> None:
    line = "Days sales outstanding (Acme, from ERPNext's own reports): 41 days."
    assert _send(monkeypatch, line) == "Days sales outstanding (Acme, from the ERP's own reports): 41 days."


def test_a_scheduled_message_is_unchanged_without_a_name(monkeypatch: pytest.MonkeyPatch, unnamed: None) -> None:
    line = "Days sales outstanding (Acme, from ERPNext's own reports): 41 days."
    assert _send(monkeypatch, line) == line


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


def test_a_cards_own_words_are_the_products_and_what_it_sends_is_as_is(
    monkeypatch: pytest.MonkeyPatch, named: None,
) -> None:
    text = _card(monkeypatch, "department_action", {
        "tool": "google_workspace__send_gmail_message",
        "input": {"to": "dana@acme.example", "subject": "Close", "body": "Post it in ERPNext today."},
        "summary": "Email Dana about the ERPNext close",
        "reason": "it names ERPNext's figures",
    })
    head, _, details = text.partition("\n\n")
    assert "ERPNext" not in head and "the ERP close" in head
    # The email goes out as written: its card shows it as it will be sent.
    assert "Post it in ERPNext today." in details

    reply = _card(monkeypatch, "delegation_reply", {
        "from_name": "Sam", "draft_to": ["sam@acme.example"], "draft_subject": "ERPNext access",
        "draft_body": "I've asked for your ERPNext login.",
    })
    assert "\nThe ERP access\n" in reply and reply.endswith("\n\nI've asked for your ERPNext login.")


def test_a_cards_words_are_unchanged_without_a_name(monkeypatch: pytest.MonkeyPatch, unnamed: None) -> None:
    text = _card(monkeypatch, "department_action", {
        "tool": "google_workspace__send_gmail_message",
        "input": {"to": "dana@acme.example", "subject": "Close", "body": "Post it in ERPNext today."},
        "summary": "Email Dana about the ERPNext close",
    })
    assert "Email Dana about the ERPNext close" in text


def test_a_taps_refusal_is_in_the_products_words(named: None) -> None:
    said = channel_cards.outcome_text(True, "This draft changed in ERPNext since the card was left.")
    assert said == "Not done: This draft changed in the ERP since the card was left."


# -- a watch job's notices --------------------------------------------------------


def test_a_notice_is_in_the_products_words_and_erpnexts_values_as_read(named: None) -> None:
    from openexecutive.watch import registry
    from openexecutive.watch.jobs import bank_matching
    from openexecutive.watch.runner import notice_body

    spec = registry.CardSpec(
        key="person:2026-11-18", summary="Bank lines to match in ERPNext's Banking app",
        values=(("Bank account", "ERPNext Holdings - Main"),), notes=(bank_matching.PERSON_LINE,),
    )
    body = notice_body(bank_matching.JOB, "2026-11-18", spec, "")
    assert "found this in the ERP on 2026-11-18" in body and "Banking app" in body
    # ERPNext's own value stays as the job read it.
    assert body.count("ERPNext") == 1 and "ERPNext Holdings - Main" in body


def test_a_notice_is_unchanged_without_a_name(unnamed: None) -> None:
    from openexecutive.watch import registry
    from openexecutive.watch.jobs import bank_matching
    from openexecutive.watch.runner import notice_body

    spec = registry.CardSpec(key="k", summary="Bank lines to match in ERPNext's Banking app")
    assert "found this in ERPNext on" in notice_body(bank_matching.JOB, "2026-11-18", spec, "")

"""A billing or spend-limit refusal from the model provider (providers.refusal).

The pilot ran out of Anthropic credit on Nov 3, 2026: every turn failed with
a 400 and people were told "something went wrong; please try again", while
nobody heard why. These pin what happens instead: one fixed reply, no retry,
and one alert an hour for the principal.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest

from openexecutive.providers import refusal
from openexecutive.providers.anthropic_provider import AnthropicProvider

# The real one, before the autouse fixture stubs it out for every other test.
_REAL_ALERT = refusal.alert_principal

CREDIT = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."
SPEND = "You have reached your specified API usage limits. You will regain access on 2026-12-01 at 00:00 UTC."
OK_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-5-5",
    "content": [{"type": "text", "text": "Hello."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 2},
}


def _error_body(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


class _Upstream:
    """A fake Anthropic API: answers each request from ``replies`` in turn."""

    def __init__(self, *replies: tuple[int, dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        status, body = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(status, json=body, headers={"request-id": f"req_{self.requests}"})


def _provider(upstream: _Upstream) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="sk-ant-test")  # gitleaks:allow
    provider._client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",  # gitleaks:allow
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        max_retries=2,  # the production default: a refusal must still go once
    )
    return provider


async def _status_error(status: int, body: dict[str, Any]) -> anthropic.APIStatusError:
    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",  # gitleaks:allow
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(status, json=body))),
        max_retries=0,
    )
    try:
        await client.messages.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "hi"}])
    except anthropic.APIStatusError as exc:
        return exc
    raise AssertionError("expected an error")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Any:
    refusal.reset_for_tests()
    alerted: list[tuple[str, str]] = []

    async def _alert(cause: str, provider: str, at: datetime) -> None:
        alerted.append((cause, provider))

    monkeypatch.setattr(refusal, "alert_principal", _alert)
    yield alerted
    refusal.reset_for_tests()


# --- Telling a refusal apart --------------------------------------------------


async def test_the_credit_and_spend_limit_refusals_are_told_apart_from_other_errors() -> None:
    credit = await _status_error(400, _error_body("invalid_request_error", CREDIT))
    spend = await _status_error(400, _error_body("invalid_request_error", SPEND))
    billing_type = await _status_error(400, _error_body("billing_error", "Billing problem."))
    payment = await _status_error(402, _error_body("invalid_request_error", "Payment required."))
    other_400 = await _status_error(400, _error_body("invalid_request_error", "max_tokens: must be at least 1"))
    rate = await _status_error(429, _error_body("rate_limit_error", "Too many requests; your usage limit resets soon."))
    assert isinstance(credit, anthropic.BadRequestError)
    assert refusal.classify(credit) == ("billing", "Anthropic")
    assert refusal.classify(spend) == ("spend_limit", "Anthropic")
    assert refusal.classify(billing_type) == ("billing", "Anthropic")
    assert refusal.classify(payment) == ("billing", "Anthropic")
    assert refusal.classify(other_400) is None
    # A rate limit passes on a retry, so the SDK's own retry handles it.
    assert refusal.classify(rate) is None
    assert refusal.classify(ValueError("credit balance is too low")) is None


async def test_a_refusal_is_found_under_what_the_turn_re_raised() -> None:
    credit = await _status_error(400, _error_body("invalid_request_error", CREDIT))
    try:
        try:
            raise credit
        except anthropic.APIStatusError as inner:
            raise RuntimeError("turn failed") from inner
    except RuntimeError as outer:
        assert refusal.classify(outer) == ("billing", "Anthropic")
    group = ExceptionGroup("tools", [ValueError("x"), credit])
    assert refusal.classify(group) == ("billing", "Anthropic")


def test_openrouter_out_of_credit_is_billing() -> None:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(402, request=request, json={"error": {"message": "Insufficient credits"}})
    exc = httpx.HTTPStatusError("402", request=request, response=response)
    assert refusal.classify(exc) == ("billing", "OpenRouter")


def test_the_reply_names_the_product(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.utils import product_name

    monkeypatch.setattr(product_name, "configured_product_name", lambda: "Dynocortex")
    assert refusal.reply_text() == "Dynocortex can't answer right now; the team has been told."


# --- Not retrying -------------------------------------------------------------


async def test_a_refused_call_is_sent_once_and_the_next_ones_not_at_all(_fresh: list[Any]) -> None:
    upstream = _Upstream((400, _error_body("invalid_request_error", CREDIT)))
    provider = _provider(upstream)
    kwargs = {"model": "claude-haiku-5-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}

    with pytest.raises(anthropic.BadRequestError):
        await provider.messages_create(**kwargs)
    assert upstream.requests == 1  # no retry
    # A turn's next calls (specialists, reviewers, the next iteration) fail
    # here without spending a request on the same refusal.
    with pytest.raises(refusal.ProviderRefused):
        await provider.messages_create(**kwargs)
    with pytest.raises(refusal.ProviderRefused):
        async with provider.messages_stream(**kwargs):
            pass
    assert upstream.requests == 1
    await asyncio.sleep(0)
    assert _fresh == [("billing", "Anthropic")]
    assert refusal.last_refusal() is not None


async def test_a_refused_stream_is_noted_when_it_is_entered() -> None:
    upstream = _Upstream((400, _error_body("invalid_request_error", SPEND)))
    provider = _provider(upstream)
    kwargs = {"model": "claude-haiku-5-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(anthropic.BadRequestError):
        async with provider.messages_stream(**kwargs):
            pass
    assert upstream.requests == 1
    last = refusal.last_refusal()
    assert last is not None and last.cause == "spend_limit"


async def test_an_answer_after_the_top_up_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = _Upstream((400, _error_body("invalid_request_error", CREDIT)), (200, OK_MESSAGE))
    provider = _provider(upstream)
    kwargs = {"model": "claude-haiku-5-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(anthropic.BadRequestError):
        await provider.messages_create(**kwargs)
    # A minute on, the next call goes out again.
    monkeypatch.setattr(refusal, "_blocked_until", 0.0)
    reply = await provider.messages_create(**kwargs)
    assert reply.content[0].text == "Hello."
    assert refusal.last_refusal() is None
    assert upstream.requests == 2


async def test_another_error_is_not_held_against_the_next_call() -> None:
    upstream = _Upstream((400, _error_body("invalid_request_error", "max_tokens: bad")), (200, OK_MESSAGE))
    provider = _provider(upstream)
    kwargs = {"model": "claude-haiku-5-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(anthropic.BadRequestError):
        await provider.messages_create(**kwargs)
    assert (await provider.messages_create(**kwargs)).content[0].text == "Hello."
    assert refusal.last_refusal() is None


# --- Telling the principal, once an hour --------------------------------------


async def test_the_principal_is_alerted_at_most_once_an_hour(_fresh: list[Any]) -> None:
    start = datetime(2026, 11, 3, 15, 33, tzinfo=UTC)
    refusal.note_refusal("billing", now=start)
    refusal.note_refusal("billing", now=start + timedelta(minutes=5))
    refusal.note_refusal("spend_limit", now=start + timedelta(minutes=59))
    await asyncio.sleep(0)
    assert _fresh == [("billing", "Anthropic")]
    refusal.note_refusal("billing", now=start + timedelta(minutes=61))
    await asyncio.sleep(0)
    assert _fresh == [("billing", "Anthropic"), ("billing", "Anthropic")]


@pytest.fixture
def alerts_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from openexecutive.alerts import store as alert_store
    from openexecutive.people import store as people_store

    db = tmp_path / "refusal.db"
    monkeypatch.setattr(alert_store, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    alert_store.initialize_db(db)
    people_store.initialize_db(db)
    people_store.upsert_person(full_name="Pat Principal", is_principal=True, email="pat@co.com", db_path=db)
    return db


async def test_the_card_is_the_principals_alone_and_names_the_cause_and_time(
    alerts_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.alerts import dispatcher
    from openexecutive.alerts import store as alert_store
    from openexecutive.alerts.models import PRIVATE_ALERT_TAG
    from openexecutive.people.store import find_principal_person
    from openexecutive.scheduler import runner

    pushed: list[int] = []
    sent: list[tuple[str, str]] = []

    async def _push(alert: Any) -> bool:
        pushed.append(alert.id)
        return True

    async def _deliver(person: Any, text: str, *, label: str = "Update") -> Any:
        sent.append((person.email, text))
        return runner.PrincipalDelivery(True, "slack_dm → U1", "delivered", "slack_dm")

    monkeypatch.setattr(dispatcher, "dispatch_web", _push)
    monkeypatch.setattr(runner, "deliver_to_person", _deliver)
    at = datetime(2026, 11, 3, 15, 33, tzinfo=UTC)
    await _REAL_ALERT("billing", "Anthropic", at)
    # Within the hour, even after a restart (nothing in memory): no second card.
    await _REAL_ALERT("billing", "Anthropic", at + timedelta(minutes=30))

    with alert_store._get_conn(alerts_db) as conn:
        rows = conn.execute(
            "SELECT id, headline, body, topic_tags, routed_to_person_id, severity FROM alerts WHERE source = ?",
            (refusal.SOURCE,),
        ).fetchall()
    assert len(rows) == 1
    card_id, headline, body, tags, routed, severity = rows[0]
    principal = find_principal_person()
    assert principal is not None and routed == principal.id
    assert PRIVATE_ALERT_TAG in json.loads(tags)
    assert severity == "urgent"
    assert "out of credit" in headline
    assert "15:33 UTC on 03 Nov 2026" in body and "credit balance is too low" in body
    assert pushed == [card_id]
    assert len(sent) == 1 and sent[0][0] == "pat@co.com" and "15:33 UTC" in sent[0][1]


async def test_setup_page_says_why_nobody_gets_an_answer() -> None:
    from types import SimpleNamespace

    from openexecutive.api import setup_checks

    refusal.note_refusal("billing", now=datetime(2026, 11, 3, 15, 33, tzinfo=UTC))
    snap = SimpleNamespace(settings=SimpleNamespace(anthropic_api_key="sk-ant-real"))  # gitleaks:allow
    check = await setup_checks.check_ai_model(snap)  # type: ignore[arg-type]
    assert check.state == "error"
    assert "15:33 UTC on 03 Nov" in check.summary and "credit balance is too low" in check.summary
    assert check.fix is not None and "console.anthropic.com" in check.fix

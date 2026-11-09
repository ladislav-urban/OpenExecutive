"""Cost caps (audit.cost_caps, providers.capped): a limit per person a day,
per job a day and a month's, checked before every hosted model call.

S7's gate: a capped job stops before its next call, the counter survives a
restart, and the principal is told.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from openexecutive.audit import cost_caps
from openexecutive.orchestrator.session import Session

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
MODEL = "claude-sonnet-5"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from openexecutive.alerts import store as alert_store
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    path = tmp_path / "caps.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(alert_store, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    alert_store.initialize_db(path)
    people_store.initialize_db(path)
    principal = people_store.upsert_person(full_name="Pat Principal", is_principal=True, email="pat@co.com", db_path=path)
    teammate = people_store.upsert_person(full_name="Sara Teammate", email="sara@co.com", db_path=path)
    monkeypatch.setattr(cost_caps, "_zone", lambda: ZoneInfo("UTC"))
    told: list[cost_caps.CostCapReached] = []
    monkeypatch.setattr(cost_caps, "_tell_principal", lambda exc, now: told.append(exc))
    return SimpleNamespace(path=path, principal=principal, teammate=teammate, told=told)


def _caps(monkeypatch: pytest.MonkeyPatch, person: float = 0, job: float = 0, monthly: float = 0) -> None:
    monkeypatch.setattr(cost_caps, "caps", lambda: cost_caps.Caps(person, job, monthly))


def _as(session: Session | None) -> Any:
    from openexecutive.orchestrator.schedule_tools import current_session

    return current_session.set(session)


def _spend(usd: float, *, session: Session | None = None, job: str | None = None) -> None:
    from openexecutive.orchestrator.schedule_tools import current_session

    token = current_session.set(session)
    try:
        if job:
            with cost_caps.bind_job(job):
                cost_caps.add(MODEL, {"cost_usd": usd}, now=NOW)
        else:
            cost_caps.add(MODEL, {"cost_usd": usd}, now=NOW)
    finally:
        current_session.reset(token)


def _check(session: Session | None = None, job: str | None = None, max_tokens: int = 10) -> None:
    from openexecutive.orchestrator.schedule_tools import current_session

    kwargs = {"model": MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": "hi"}]}
    token = current_session.set(session)
    try:
        if job:
            with cost_caps.bind_job(job):
                cost_caps.check(MODEL, kwargs, now=NOW)
        else:
            cost_caps.check(MODEL, kwargs, now=NOW)
    finally:
        current_session.reset(token)


def test_with_every_cap_off_nothing_is_read_and_every_call_goes(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    _caps(monkeypatch)
    monkeypatch.setattr(cost_caps, "_connect", lambda: (_ for _ in ()).throw(AssertionError("read")))
    _check(Session(caller_person_id=db.teammate))


def test_a_teammate_over_their_daily_cap_is_refused_and_the_principal_told(
    db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _caps(monkeypatch, person=1.0)
    sara = Session(caller_person_id=db.teammate)
    _spend(0.5, session=sara)
    _check(sara)
    _spend(0.6, session=sara)
    with pytest.raises(cost_caps.CostCapReached) as refused:
        _check(sara)
    assert refused.value.scope == "person" and refused.value.name == "Sara Teammate"
    assert "today's usage limit" in refused.value.reply()
    assert [e.scope for e in db.told] == ["person"]
    # Someone else still goes.
    _check(Session(caller_person_id=db.principal))


def test_the_principal_is_never_held_to_the_person_cap_but_is_to_the_months(
    db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pat = Session(caller_person_id=db.principal)
    _caps(monkeypatch, person=1.0)
    _spend(5.0, session=pat)
    _check(pat)
    _caps(monkeypatch, person=1.0, monthly=4.0)
    with pytest.raises(cost_caps.CostCapReached) as refused:
        _check(pat)
    assert refused.value.scope == "total"
    assert refused.value.resets_at == datetime(2026, 12, 1, tzinfo=UTC)


def test_a_capped_job_stops_before_its_next_call(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.providers.capped import CappedProvider

    _caps(monkeypatch, job=2.0)
    _spend(2.0, job="scheduled:principal_brief_morning")
    sent: list[dict[str, Any]] = []

    class _Backend:
        async def messages_create(self, **kwargs: Any) -> Any:
            sent.append(kwargs)
            return SimpleNamespace()

    provider = CappedProvider(_Backend())  # type: ignore[arg-type]

    async def _call() -> None:
        await provider.messages_create(model=MODEL, max_tokens=10, messages=[{"role": "user", "content": "hi"}])

    with cost_caps.bind_job("scheduled:principal_brief_morning"), pytest.raises(cost_caps.CostCapReached):
        asyncio.run(_call())
    assert sent == []
    # Another job has its own budget.
    with cost_caps.bind_job("alert-triage"):
        asyncio.run(_call())
    assert len(sent) == 1


def test_a_call_whose_worst_case_would_pass_the_cap_is_refused(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    _caps(monkeypatch, monthly=1.0)
    _spend(0.9)
    _check(max_tokens=10)
    with pytest.raises(cost_caps.CostCapReached):
        _check(max_tokens=1_000_000)


def test_the_counter_survives_a_restart(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    _spend(1.25, session=Session(caller_person_id=db.teammate), job="alert-triage")
    # A new process has nothing in memory: the counts are rows in the DB.
    with sqlite3.connect(str(db.path)) as conn:
        rows = dict(((s, k), c) for s, k, c in conn.execute(
            "SELECT scope, key, cost_usd FROM usage_counters WHERE day = '2026-11-09'",
        ))
    assert rows == {("total", "all"): 1.25, ("person", str(db.teammate)): 1.25, ("job", "alert-triage"): 1.25}
    assert cost_caps.spend_today("person", str(db.teammate), now=NOW) == 1.25


def test_a_teammates_queued_run_counts_against_that_teammate(db: SimpleNamespace) -> None:
    _spend(0.4, session=Session(unattended=True, run_for_person_id=db.teammate))
    assert cost_caps.spend_today("person", str(db.teammate), now=NOW) == 0.4
    assert cost_caps.spend_today("job", cost_caps.UNNAMED_JOB, now=NOW) == 0.4
    assert cost_caps.spend_today("person", str(db.principal), now=NOW) == 0.0


def test_a_day_runs_in_the_companys_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cost_caps, "_zone", lambda: ZoneInfo("Asia/Tokyo"))
    # 20:00 UTC on Nov 9 is already Nov 10 in Tokyo.
    assert cost_caps.day_key(datetime(2026, 11, 9, 20, 0, tzinfo=UTC)) == "2026-11-10"
    assert cost_caps._next_day(datetime(2026, 11, 9, 20, 0, tzinfo=UTC)) == datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


def test_a_counter_that_cant_be_read_refuses_the_call(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    _caps(monkeypatch, monthly=100.0)

    def _broken(*_a: Any, **_k: Any) -> float:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(cost_caps, "_spent", _broken)
    with pytest.raises(cost_caps.CostCapReached) as refused:
        _check()
    assert refused.value.scope == "unreadable"


def test_an_unknown_cost_counts_at_the_highest_price(monkeypatch: pytest.MonkeyPatch) -> None:
    unpriced = cost_caps.charge("some-unknown-model", {"cost_usd": None, "output_tokens": 1_000_000})
    top = cost_caps._top_price()
    assert top is not None and unpriced == pytest.approx(top.output)
    assert cost_caps.charge(MODEL, {"cost_usd": 0.03}) == 0.03


def test_the_principal_is_told_once_per_cap_a_day(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.alerts import store as alert_store
    from openexecutive.alerts.models import PRIVATE_ALERT_TAG
    from openexecutive.scheduler import runner

    sent: list[str] = []

    async def _deliver(person: Any, text: str, *, label: str = "Update") -> Any:
        sent.append(text)
        return runner.PrincipalDelivery(True, "slack_dm → U1", "delivered", "slack_dm")

    monkeypatch.setattr(runner, "deliver_to_person", _deliver)
    exc = cost_caps.CostCapReached("person", str(db.teammate), 1.0, 1.1, datetime(2026, 11, 10, tzinfo=UTC), name="Sara Teammate")
    asyncio.run(cost_caps._alert_principal(exc, NOW))
    asyncio.run(cost_caps._alert_principal(exc, NOW))
    with alert_store._get_conn(db.path) as conn:
        rows = conn.execute(
            "SELECT headline, topic_tags, routed_to_person_id FROM alerts WHERE source = ?", (cost_caps.SOURCE,),
        ).fetchall()
    assert len(rows) == 1 and len(sent) == 1
    assert rows[0][0] == "Sara Teammate reached today's usage limit"
    assert PRIVATE_ALERT_TAG in rows[0][1] and rows[0][2] == db.principal


def test_a_route_tells_the_asker_the_limit_not_an_error() -> None:
    from openexecutive.providers.refusal import refusal_reply

    exc = cost_caps.CostCapReached("person", "2", 1.0, 1.0, NOW)
    try:
        try:
            raise exc
        except cost_caps.CostCapReached as inner:
            raise RuntimeError("turn failed") from inner
    except RuntimeError as wrapped:
        assert refusal_reply(wrapped) == exc.reply()


def test_a_capped_scheduled_action_waits_for_the_reset_without_spending_an_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.scheduler import runner

    moved: list[tuple[int, datetime]] = []
    monkeypatch.setattr(runner, "reschedule_action", lambda action_id, at: moved.append((action_id, at)) or True)
    monkeypatch.setattr(runner, "mark_action_failed_or_retry", lambda *_a: pytest.fail("counted as a failure"))
    resets = datetime(2026, 11, 10, tzinfo=UTC)
    assert runner._failed_or_retry(7, cost_caps.CostCapReached("job", "x", 1.0, 1.0, resets)) == "deferred"
    assert moved == [(7, resets)]


def test_every_hosted_backend_is_capped_and_a_local_one_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.providers import registry
    from openexecutive.providers.capped import CappedProvider

    monkeypatch.setattr(registry, "_anthropic", lambda: SimpleNamespace())
    monkeypatch.setattr(registry, "_local", lambda: SimpleNamespace())
    settings = SimpleNamespace(openrouter_enabled=False, local_models_enabled=True, local_models=["llama-local"])
    monkeypatch.setattr(registry, "get_settings", lambda: settings)
    assert isinstance(registry.get_provider(MODEL), CappedProvider)
    assert not isinstance(registry.get_provider("llama-local"), CappedProvider)


def test_a_finished_calls_audit_row_names_its_person_and_job(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.audit import usage
    from openexecutive.orchestrator.schedule_tools import current_session

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(usage, "log_event", lambda *a, **k: rows.append(k["details"]))
    message = SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=5), stop_reason="end_turn")
    token = current_session.set(Session(caller_person_id=db.teammate))
    try:
        with cost_caps.bind_job("take-the-lead"):
            usage.log_model_usage(message, model=MODEL, actor="executive")
    finally:
        current_session.reset(token)
    assert rows[0]["person_id"] == db.teammate and rows[0]["job_id"] == "take-the-lead"


def _provider(stream_usage: Any = None, *, create_usage: Any = None) -> Any:
    from openexecutive.providers.capped import CappedProvider

    class _Stream:
        current_message_snapshot = SimpleNamespace(usage=stream_usage) if stream_usage is not None else None

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    class _Backend:
        async def messages_create(self, **_k: Any) -> Any:
            return SimpleNamespace(usage=create_usage)

        def messages_stream(self, **_k: Any) -> Any:
            return _Stream()

    return CappedProvider(_Backend())  # type: ignore[arg-type]


KW = {"model": MODEL, "max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}]}


def test_a_stream_closed_before_it_reported_usage_stays_counted_at_its_worst_case(
    db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _caps(monkeypatch, monthly=100.0)
    provider = _provider(stream_usage=None)

    async def _closed_early() -> None:
        async with provider.messages_stream(**KW):
            raise asyncio.CancelledError  # the person closed the tab

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_closed_early())
    assert cost_caps.spend_today("total", "all") == pytest.approx(cost_caps.worst_case(MODEL, KW))


def test_an_answered_call_settles_to_what_it_cost(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    _caps(monkeypatch, monthly=100.0)
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100)
    asyncio.run(_provider(create_usage=usage).messages_create(**KW))
    spent = cost_caps.spend_today("total", "all")
    assert 0 < spent < cost_caps.worst_case(MODEL, KW)
    # With every cap off it is still counted, once, at its cost.
    _caps(monkeypatch)
    asyncio.run(_provider(create_usage=usage).messages_create(**KW))
    assert cost_caps.spend_today("total", "all") == pytest.approx(2 * spent)


def test_calls_made_together_cant_all_pass_on_the_same_spend(
    db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    worst = cost_caps.worst_case(MODEL, KW)
    _caps(monkeypatch, monthly=worst * 2.5)
    passed = refused = 0
    for _ in range(5):
        try:
            cost_caps.reserve(MODEL, KW, now=NOW)
            passed += 1
        except cost_caps.CostCapReached:
            refused += 1
    assert (passed, refused) == (2, 3)


def test_an_unauthenticated_email_sender_is_nobody_on_people(db: SimpleNamespace) -> None:
    spoofed = Session(origin_channel="email", caller_person_id=db.principal)
    assert cost_caps._person_id(spoofed) is None
    assert cost_caps._person_key(spoofed) == "outside:email"
    proven = Session(origin_channel="email", caller_person_id=db.principal, email_authenticated=True)
    assert cost_caps._person_id(proven) == db.principal


def test_senders_off_people_share_a_capped_bucket_per_channel_and_hear_no_budget(
    db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.orchestrator.schedule_tools import current_session

    _caps(monkeypatch, person=1.0)
    space = Session(origin_channel="google_chat")
    _spend(1.0, session=space)
    token = current_session.set(space)
    try:
        with pytest.raises(cost_caps.CostCapReached) as refused:
            cost_caps.check(MODEL, KW, now=NOW)
        assert refused.value.key == "outside:google_chat"
        assert "limit" not in refused.value.reply()
    finally:
        current_session.reset(token)


def test_counters_that_cant_be_read_are_retried_not_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.scheduler import runner

    monkeypatch.setattr(runner, "reschedule_action", lambda *_a: pytest.fail("deferred a fault"))
    monkeypatch.setattr(runner, "mark_action_failed_or_retry", lambda *_a: "pending")
    sick = cost_caps.CostCapReached("unreadable", "OperationalError", 0.0, 0.0, NOW)
    assert runner._failed_or_retry(7, sick) == "pending"


def test_an_openrouter_stream_settles_to_its_real_cost(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.providers.capped import CappedProvider

    _caps(monkeypatch, monthly=100.0)

    class _OpenRouterLikeStream:
        # No current_message_snapshot: the finished message is kept here.
        _finalized = SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=100))

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    class _Backend:
        def messages_stream(self, **_k: Any) -> Any:
            return _OpenRouterLikeStream()

    async def _run() -> None:
        async with CappedProvider(_Backend()).messages_stream(**KW):  # type: ignore[arg-type]
            pass

    asyncio.run(_run())
    assert 0 < cost_caps.spend_today("total", "all") < cost_caps.worst_case(MODEL, KW)


def test_a_call_the_provider_refused_costs_nothing(db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx

    from openexecutive.providers.capped import CappedProvider

    _caps(monkeypatch, monthly=100.0)
    request = httpx.Request("POST", "https://api.example/v1/messages")

    class _Backend:
        async def messages_create(self, **_k: Any) -> Any:
            try:
                raise httpx.ConnectError("refused", request=request)
            except httpx.ConnectError as cause:
                raise anthropic.APIConnectionError(request=request) from cause

    with pytest.raises(anthropic.APIConnectionError):
        asyncio.run(CappedProvider(_Backend()).messages_create(**KW))  # type: ignore[arg-type]
    assert cost_caps.spend_today("total", "all") == 0.0


def test_a_bound_spender_is_who_a_nameless_session_spends_for(db: SimpleNamespace) -> None:
    from openexecutive.orchestrator.schedule_tools import current_session

    panel = Session(origin_channel="erpnext")
    token = current_session.set(panel)
    try:
        with cost_caps.bind_spender(db.teammate):
            assert cost_caps._person_key(panel) == str(db.teammate)
        with cost_caps.bind_spender(f"{cost_caps.OUTSIDE_PREFIX}a2a"):
            assert cost_caps._person_key(Session()) == "outside:a2a"
    finally:
        current_session.reset(token)
    # A workflow's steps are spent for its creator.
    from openexecutive.workflows.chat_tools import in_workflow

    with in_workflow("wf", db.teammate):
        assert cost_caps._person_key(Session(unattended=True)) == str(db.teammate)


def test_a_workflow_is_spent_for_whoever_asked_not_its_owner(db: SimpleNamespace) -> None:
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.workflows.chat_tools import in_workflow

    token = current_session.set(Session(caller_person_id=db.teammate))
    try:
        # A workflow the principal saved, run by the teammate.
        with in_workflow("wf", db.principal):
            assert cost_caps._person_key(Session(unattended=True)) == str(db.teammate)
    finally:
        current_session.reset(token)


def test_a_call_held_back_after_a_billing_refusal_costs_nothing(
    db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.providers.capped import CappedProvider
    from openexecutive.providers.refusal import ProviderRefused

    _caps(monkeypatch, monthly=100.0)

    class _Backend:
        async def messages_create(self, **_k: Any) -> Any:
            raise ProviderRefused("billing")

    with pytest.raises(ProviderRefused):
        asyncio.run(CappedProvider(_Backend()).messages_create(**KW))  # type: ignore[arg-type]
    assert cost_caps.spend_today("total", "all") == 0.0


def test_a_refusal_in_the_worker_tells_the_principal_on_the_loop(
    db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from openexecutive.providers.capped import CappedProvider

    _caps(monkeypatch, monthly=0.000001)
    _spend(1.0)
    where: list[bool] = []
    monkeypatch.setattr(
        cost_caps, "_tell_principal", lambda exc, now: where.append(threading.current_thread() is threading.main_thread()),
    )

    class _Backend:
        async def messages_create(self, **_k: Any) -> Any:
            raise AssertionError("sent")

    with pytest.raises(cost_caps.CostCapReached):
        asyncio.run(CappedProvider(_Backend()).messages_create(**KW))  # type: ignore[arg-type]
    assert where == [True]

"""What a model call costs (audit.pricing) and what ERPNext work costs per
unit (AuditLogger.erpnext_usage, GET /audit/usage?erpnext=1)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import audit as audit_route
from openexecutive.audit import pricing
from openexecutive.audit import usage as au
from openexecutive.audit.logger import ERPNEXT_USAGE_KINDS, AuditLogger, set_audit_logger


@pytest.fixture(autouse=True)
def _own_people_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.people import store as people_store

    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_store.initialize_db()


@pytest.fixture(autouse=True)
def _shipped_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICES_PATH", raising=False)


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    logger = AuditLogger(tmp_path / "audit.db")
    set_audit_logger(logger)
    yield logger  # type: ignore[misc]
    set_audit_logger(None)


def _message(**usage: object) -> SimpleNamespace:
    return SimpleNamespace(usage=SimpleNamespace(**usage), stop_reason="end_turn")


# --------------------------------------------------------------------------- #
# The price table
# --------------------------------------------------------------------------- #


def test_every_model_the_app_can_call_directly_has_a_price() -> None:
    from openexecutive.config import get_settings
    from openexecutive.providers.registry import ANTHROPIC_DIRECT_MODELS

    settings = get_settings()
    configured = {
        settings.default_model, settings.deep_reasoning_model,
        settings.routing_model, settings.research_model,
    }
    for model in [*ANTHROPIC_DIRECT_MODELS, *configured]:
        assert pricing.price_for(model) is not None, model


@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("claude-opus-5-20260315", "claude-opus-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("anthropic/claude-sonnet-5.5", "claude-sonnet-5-5"),
        ("anthropic/claude-opus-5", "claude-opus-5"),
        ("openai/gpt-6-astra", None),
        ("m", None),
    ],
)
def test_price_key_maps_pins_and_openrouter_slugs(model: str, key: str | None) -> None:
    assert pricing.price_key(model) == key


def test_cost_from_tokens_prices_each_token_class() -> None:
    # Opus 5.5: $4 input, $5 5m writes, $8 1h writes, $0.20 reads, $20 output.
    counts = {
        "input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
        "cache_creation_1h_input_tokens": 250_000, "cache_read_input_tokens": 1_000_000,
        "output_tokens": 100_000, "web_search_requests": 3,
    }
    expected = 4.00 + 0.75 * 5.00 + 0.25 * 8.00 + 0.20 + 0.1 * 20.00 + 3 * 0.01
    assert pricing.cost_from_tokens("claude-opus-5-5", counts) == pytest.approx(expected)


def test_cache_writes_without_the_1h_split_price_at_the_5m_rate() -> None:
    counts = {"cache_creation_input_tokens": 1_000_000}
    assert pricing.cost_from_tokens("claude-sonnet-5-5", counts) == pytest.approx(2.50)


def test_haiku_5_5_prompts_over_100k_pay_the_long_rate() -> None:
    short = {"input_tokens": 60_000, "cache_read_input_tokens": 40_000}
    long = {"input_tokens": 60_000, "cache_read_input_tokens": 40_001}
    # Costs are rounded to the millionth of a dollar.
    assert pricing.cost_from_tokens("claude-haiku-5-5", short) == pytest.approx(
        (60_000 * 0.10 + 40_000 * 0.01) / 1_000_000, abs=1e-6
    )
    assert pricing.cost_from_tokens("claude-haiku-5-5", long) == pytest.approx(
        (60_000 * 0.50 + 40_001 * 0.05) / 1_000_000, abs=1e-6
    )


def test_cost_from_tokens_is_none_for_a_model_without_a_price() -> None:
    assert pricing.cost_from_tokens("openai/gpt-6-astra", {"input_tokens": 10}) is None


def test_the_shipped_price_file_is_valid_and_dated() -> None:
    import json

    table = pricing.parse_table(json.loads(pricing.SHIPPED_PRICES.read_text(encoding="utf-8")))
    assert table.prices_as_of == pricing.prices_as_of()
    assert table.web_search_usd == pytest.approx(0.01)


def _record_errors(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The pricing logger's error lines, read off the logger itself: another
    module's logging setup can stop them reaching caplog in a full run."""
    errors: list[str] = []
    monkeypatch.setattr(pricing.logger, "error", lambda msg, *a, **_k: errors.append(msg % a))
    return errors


def _write_prices(path: Path, as_of: str, opus_input: float) -> None:
    import json

    path.write_text(json.dumps({
        "prices_as_of": as_of, "web_search_per_search": 0,
        "models": {"claude-opus-5-5": {"input": opus_input, "cache_write_5m": 0,
                                       "cache_write_1h": 0, "cache_read": 0, "output": 0}},
    }), encoding="utf-8")


def test_an_operator_price_file_replaces_the_shipped_one_and_is_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    path = tmp_path / "prices.json"
    _write_prices(path, "2027-01-01", 1.0)
    monkeypatch.setenv("MODEL_PRICES_PATH", str(path))
    assert pricing.prices_as_of() == "2027-01-01"
    assert pricing.cost_from_tokens("claude-opus-5-5", {"input_tokens": 1_000_000}) == 1.0
    # A model the operator's file leaves out is unpriced, not priced from the shipped file.
    assert pricing.cost_from_tokens("claude-sonnet-5-5", {"input_tokens": 1}) is None
    _write_prices(path, "2027-02-01", 3.0)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert pricing.prices_as_of() == "2027-02-01"
    assert pricing.cost_from_tokens("claude-opus-5-5", {"input_tokens": 1_000_000}) == 3.0


@pytest.mark.parametrize("content", ["not json", '{"prices_as_of": "soon", "models": {}}',
                                     '{"prices_as_of": "2027-01-01", "models": {"gpt-6": {}}}'])
def test_a_bad_operator_file_falls_back_to_the_shipped_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str,
) -> None:
    import json

    errors = _record_errors(monkeypatch)
    path = tmp_path / "prices.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("MODEL_PRICES_PATH", str(path))
    shipped = json.loads(pricing.SHIPPED_PRICES.read_text(encoding="utf-8"))["prices_as_of"]
    assert pricing.prices_as_of() == shipped
    assert len(errors) == 1 and "can't be used" in errors[0]


def test_a_broken_shipped_file_leaves_calls_unpriced_but_still_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit: AuditLogger,
) -> None:
    broken = tmp_path / "model_prices.json"
    broken.write_text("{", encoding="utf-8")
    monkeypatch.setattr(pricing, "SHIPPED_PRICES", broken)
    assert pricing.prices_as_of() == ""
    au.log_model_usage(_message(input_tokens=10), model="claude-opus-5-5", actor="executive")
    row = audit.query(event_type="cache_event")[0]
    assert row.details["input_tokens"] == 10 and row.details["cost_source"] == "unpriced"
    data = audit.erpnext_usage()
    assert data["prices_as_of"] == ""


def test_a_pricing_fault_never_costs_the_row(
    monkeypatch: pytest.MonkeyPatch, audit: AuditLogger,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("bad table")

    monkeypatch.setattr(pricing, "price_table", _boom)
    assert au.log_model_usage(_message(input_tokens=3), model="claude-opus-5-5", actor="x")
    row = audit.query(event_type="cache_event")[0]
    assert row.details["cost_usd"] is None and row.details["cost_source"] == "unpriced"


@pytest.mark.parametrize("price", [1e308, 10_001, -1])
def test_prices_out_of_range_are_refused(price: float) -> None:
    with pytest.raises(ValueError):
        pricing.parse_table({"prices_as_of": "2027-01-01", "models": {
            "claude-opus-5-5": {"input": price, "cache_write_5m": 0, "cache_write_1h": 0,
                                "cache_read": 0, "output": 0}}})


def test_a_bad_operator_file_is_logged_once_per_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = _record_errors(monkeypatch)
    path = tmp_path / "prices.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("MODEL_PRICES_PATH", str(path))
    for _ in range(3):
        pricing.price_table()
    assert len(errors) == 1


def test_a_missing_operator_file_falls_back_to_the_shipped_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_PRICES_PATH", str(tmp_path / "nowhere.json"))
    assert pricing.price_for("claude-opus-5-5") is not None


# --------------------------------------------------------------------------- #
# Recording a call's cost
# --------------------------------------------------------------------------- #


def test_a_direct_anthropic_call_records_its_price_table_cost(audit: AuditLogger) -> None:
    msg = _message(
        input_tokens=1000, output_tokens=200, cache_read_input_tokens=5000,
        cache_creation_input_tokens=400,
        cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=100),
    )
    counts = au.log_model_usage(msg, model="claude-sonnet-5-5", actor="executive")
    expected = (1000 * 2.00 + 300 * 2.50 + 100 * 4.00 + 5000 * 0.10 + 200 * 10.00) / 1_000_000
    assert counts is not None and counts["cost_usd"] == pytest.approx(expected)
    row = audit.query(event_type="cache_event")[0]
    assert row.details["cost_usd"] == pytest.approx(expected)
    assert row.details["cost_source"] == "price_table"
    assert row.details["prices_as_of"] == pricing.prices_as_of()
    assert row.details["cache_creation_1h_input_tokens"] == 100


def test_a_provider_reported_cost_is_kept(audit: AuditLogger) -> None:
    msg = _message(input_tokens=1000, output_tokens=200, cost="0.5")
    au.log_model_usage(msg, model="anthropic/claude-sonnet-5.5", actor="executive")
    row = audit.query(event_type="cache_event")[0]
    assert row.details["cost_usd"] == 0.5 and row.details["cost_source"] == "provider"
    assert "prices_as_of" not in row.details


def test_a_call_to_a_model_without_a_price_records_no_cost(audit: AuditLogger) -> None:
    au.log_model_usage(_message(input_tokens=10), model="openai/gpt-6-astra", actor="executive")
    row = audit.query(event_type="cache_event")[0]
    assert row.details["cost_usd"] is None and row.details["cost_source"] == "unpriced"


# --------------------------------------------------------------------------- #
# ERPNext work, per unit
# --------------------------------------------------------------------------- #


def _call(audit: AuditLogger, turn: str, *, session: str = "s", model: str = "m",
          cost: float | None = 0.01, actor: str = "executive", private: bool = False,
          **tokens: int) -> None:
    details: dict[str, object] = {"model": model, "input_tokens": 100, "output_tokens": 10, **tokens}
    if cost is not None:
        details["cost_usd"] = cost
        details["cost_source"] = "price_table"
    audit.log("cache_event", "call", session_id=session, turn_id=turn, actor=actor,
              details=details, private=private)


def _erp_tool(audit: AuditLogger, turn: str, tool: str = "erpnext__run_report") -> None:
    audit.log("tool_invocation", f"mcp:{tool}", session_id="s", turn_id=turn,
              actor="executive", details={"tool": tool, "kind": "mcp"})


def _kinds(data: dict) -> dict[str, dict]:
    return {k["kind"]: k for k in data["kinds"]}


def test_each_turn_counts_once_under_its_kind(audit: AuditLogger) -> None:
    # A chat question that read ERPNext: two calls (the Executive and a specialist).
    _erp_tool(audit, "t-q")
    _call(audit, "t-q", cost=0.02)
    _call(audit, "t-q", cost=0.03, actor="specialist")
    # A turn that read ERPNext and left a draft card is a draft, not a question.
    _erp_tool(audit, "t-d")
    audit.log("erpnext_draft_proposed", "card", session_id="s", turn_id="t-d",
              details={"doctype": "Material Request"})
    _call(audit, "t-d", cost=0.10)
    # A panel turn, whether or not it called a tool.
    _call(audit, "t-p", session="erpnext:3:abc", cost=0.04)
    # The brief and an ERPNext alert's triage, under their own turn ids.
    _call(audit, "brief-1", actor="briefing_narrative", cost=0.05)
    _call(audit, "brief-1", actor="briefing_grounding", cost=0.01)
    _call(audit, "erpnext-alert:erpnext:overdue:X:2026-10", actor="triage", cost=0.002)
    # A turn that never touched ERPNext, and a tool that isn't ERPNext's.
    _call(audit, "t-other", cost=9.0)
    audit.log("tool_invocation", "mcp:google__send", session_id="s", turn_id="t-mail",
              details={"tool": "google__send", "kind": "mcp"})
    _call(audit, "t-mail", cost=9.0)

    data = audit.erpnext_usage()
    assert data["prices_as_of"] == pricing.prices_as_of()
    assert [k["kind"] for k in data["kinds"]] == list(ERPNEXT_USAGE_KINDS)
    kinds = _kinds(data)
    assert kinds["question"]["count"] == 1 and kinds["question"]["calls"] == 2
    assert kinds["question"]["cost_usd"] == pytest.approx(0.05)
    assert kinds["draft"]["count"] == 1 and kinds["draft"]["cost_usd"] == pytest.approx(0.10)
    assert kinds["panel_question"]["count"] == 1
    assert kinds["morning_brief"]["count"] == 1
    assert kinds["morning_brief"]["cost_usd"] == pytest.approx(0.06)
    assert kinds["alert_triage"]["count"] == 1
    assert sum(k["count"] for k in data["kinds"]) == 5


def test_median_and_p90_are_of_one_unit(audit: AuditLogger) -> None:
    for i, cost in enumerate([0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.50]):
        _erp_tool(audit, f"t{i}")
        _call(audit, f"t{i}", cost=cost, input_tokens=100 * (i + 1))
    q = _kinds(audit.erpnext_usage())["question"]
    assert q["count"] == 10
    assert q["cost_per"]["median"] == pytest.approx(0.05)
    assert q["cost_per"]["p90"] == pytest.approx(0.09)
    assert q["tokens"]["median"] == 500 + 10  # input plus output of the 5th
    assert q["tokens"]["p90"] == 900 + 10


def test_rows_from_before_cost_capture_are_estimated_and_unknown_models_unpriced(
    audit: AuditLogger,
) -> None:
    _erp_tool(audit, "t-old")
    audit.log("cache_event", "old", session_id="s", turn_id="t-old",
              details={"model": "claude-sonnet-5-5", "input_tokens": 1_000_000})
    _erp_tool(audit, "t-gpt")
    audit.log("cache_event", "gpt", session_id="s", turn_id="t-gpt",
              details={"model": "openai/gpt-6-astra", "input_tokens": 10,
                       "cost_usd": None, "cost_source": "unpriced"})
    q = _kinds(audit.erpnext_usage())["question"]
    assert q["count"] == 2
    assert q["estimated_calls"] == 1 and q["unpriced_calls"] == 1
    assert q["cost_usd"] == pytest.approx(2.00)


def test_the_window_bounds_the_turns_marked_as_erpnext(audit: AuditLogger) -> None:
    _erp_tool(audit, "t1")
    _call(audit, "t1")
    data = audit.erpnext_usage(since="2999-01-01T00:00:00")
    assert _kinds(data)["question"]["count"] == 0
    assert _kinds(audit.erpnext_usage(since="2000-01-01T00:00:00"))["question"]["count"] == 1


def test_private_calls_are_left_out_unless_the_caller_may_see_them(audit: AuditLogger) -> None:
    _erp_tool(audit, "t1")
    _call(audit, "t1", private=True)
    assert _kinds(audit.erpnext_usage(include_private=False))["question"]["count"] == 0
    assert _kinds(audit.erpnext_usage(include_private=True))["question"]["count"] == 1


def test_a_private_row_does_not_say_what_a_turn_was(audit: AuditLogger) -> None:
    # The draft row is always private; its turn's public calls and tool row stay
    # readable. A caller who can't read the draft row sees a question.
    _erp_tool(audit, "t1")
    audit.log("erpnext_draft_proposed", "card", session_id="s", turn_id="t1",
              details={"doctype": "ToDo"}, private=True)
    _call(audit, "t1")
    hidden = _kinds(audit.erpnext_usage(include_private=False))
    assert hidden["draft"]["count"] == 0 and hidden["question"]["count"] == 1
    seen = _kinds(audit.erpnext_usage(include_private=True))
    assert seen["draft"]["count"] == 1 and seen["question"]["count"] == 0


def test_a_non_finite_cost_counts_as_unpriced(audit: AuditLogger) -> None:
    _erp_tool(audit, "t1")
    _call(audit, "t1", cost=0.02)
    audit.log("cache_event", "bad", session_id="s", turn_id="t1",
              details={"model": "m", "input_tokens": 1, "cost_usd": "nan",
                       "cost_source": "provider"})
    q = _kinds(audit.erpnext_usage())["question"]
    assert q["cost_usd"] == pytest.approx(0.02) and q["unpriced_calls"] == 1


def test_a_provider_cost_of_nan_is_replaced_by_the_price_table() -> None:
    counts = au.usage_counts(_message(input_tokens=1, cost="nan"))
    assert counts is not None and counts["cost_usd"] is None


def test_empty_log_reports_every_kind_at_zero(audit: AuditLogger) -> None:
    data = audit.erpnext_usage()
    assert [k["count"] for k in data["kinds"]] == [0] * len(ERPNEXT_USAGE_KINDS)
    assert all(k["cost_per"] == {"median": 0.0, "p90": 0.0} for k in data["kinds"])


def test_usage_endpoint_adds_erpnext_only_when_asked(tmp_path: Path) -> None:
    app = FastAPI()
    audit = AuditLogger(tmp_path / "audit.db")
    app.state.audit = audit
    set_audit_logger(audit)
    app.include_router(audit_route.router)
    client = TestClient(app)
    try:
        _erp_tool(audit, "t1")
        _call(audit, "t1", cost=0.02)
        assert client.get("/audit/usage").json()["erpnext"] is None
        data = client.get("/audit/usage", params={"erpnext": 1}).json()["erpnext"]
        assert data["prices_as_of"] == pricing.prices_as_of()
        question = {k["kind"]: k for k in data["kinds"]}["question"]
        assert question["count"] == 1 and question["cost_usd"] == pytest.approx(0.02)
        assert question["cost_per"]["median"] == pytest.approx(0.02)
    finally:
        set_audit_logger(None)


# --------------------------------------------------------------------------- #
# Unattended work gets turn ids of its own
# --------------------------------------------------------------------------- #


def test_an_erpnext_alerts_triage_runs_under_its_own_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.alerts import pipeline
    from openexecutive.audit.context import get_active_ids
    from openexecutive.integrations import erpnext_events

    seen: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(pipeline, "schedule_evaluation", lambda event: seen.append(get_active_ids()))
    erpnext_events._schedule({"kind": next(iter(erpnext_events.KINDS)), "key": "DOC-1",
                              "title": "t", "text": "x"})
    assert len(seen) == 1
    _, turn = seen[0]
    assert turn is not None and turn.startswith("erpnext-alert:erpnext:")
    assert get_active_ids() == (None, None)


def test_an_absurd_token_count_is_capped(audit: AuditLogger) -> None:
    _erp_tool(audit, "t1")
    audit.log("cache_event", "huge", session_id="s", turn_id="t1",
              details={"model": "claude-opus-5-5", "input_tokens": 10**400})
    q = _kinds(audit.erpnext_usage())["question"]
    assert q["input_tokens"] == pricing.MAX_TOKENS

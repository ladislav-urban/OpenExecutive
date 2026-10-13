"""ERPNext phase 2: the signed caller the gateway adds to every ``erpnext__*``
call (orchestrator/erpnext_caller.py, mcp_gateway's ERPNext gate)."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openexecutive.orchestrator import erpnext_caller
from openexecutive.orchestrator.erpnext_caller import (
    ASSERTION_ARG,
    CallerRefused,
    ErpnextCaller,
    caller_for,
    mint,
)
from openexecutive.orchestrator.mcp_gateway import MCPGateway
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.prompts.connected_systems import render_connected_systems

# A fixed key, so the token below is byte for byte what oe_connector's
# tests/test_caller.py verifies (Ed25519 signatures are deterministic).
VECTOR_PRIVATE_KEY = "vec:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
VECTOR_PUBLIC_KEY = "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
VECTOR_ARGUMENTS = {"report_name": "Gross Profit", "filters": {"company": "Acme", "from_date": "2026-09-01"}}
VECTOR_TOKEN = (
    "v1.eyJhcmdzIjoiNjI1ZGMzNjBjYmUxMmRhMmMxYjUyNmZlY2FmZDk0OGVmYTY0NDFkNmZjYWQ5ZjlhNzdiOGUyZWI2NzA1NDM1M"
    "CIsImF1ZCI6Im9lX2Nvbm5lY3RvciIsImV4cCI6MTgwMDAwMDA2MCwiaWF0IjoxODAwMDAwMDAwLCJqdGkiOiJ2ZWN0b3Itand0L"
    "WlkLTAwMDAwMSIsImtpZCI6InZlYyIsImtpbmQiOiJ1c2VyIiwic3ViIjoiYXNrZXJAZXhhbXBsZS5jb20iLCJ0b29sIjoicnVuX"
    "3JlcG9ydCJ9.1ycH-9fl-BykmbYqXc05zPy72xk_gYhYw3MrZbU6F7TeJ-EOkp2OvSGTysDmNppfTqaGfhsIYcI5i8RxUQh5Bw"
)

PRINCIPAL = SimpleNamespace(email="Boss@Example.com", is_principal=True)
TEAMMATE = SimpleNamespace(email="ana@example.com", is_principal=False)


def _b64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _claims(token: str) -> dict[str, Any]:
    """Verify ``token`` against the vector's public key; its claims."""
    version, payload, signature = token.split(".")
    Ed25519PublicKey.from_public_bytes(_b64(VECTOR_PUBLIC_KEY)).verify(
        _b64(signature), f"{version}.{payload}".encode("ascii")
    )
    claims: dict[str, Any] = json.loads(_b64(payload))
    return claims


def test_mints_the_vector_oe_connector_verifies() -> None:
    token = mint(
        VECTOR_PRIVATE_KEY,
        ErpnextCaller("user", "asker@example.com"),
        tool="run_report",
        arguments=VECTOR_ARGUMENTS,
        now=1_800_000_000,
        jti="vector-jwt-id-000001",
    )
    assert token == VECTOR_TOKEN


def test_an_assertion_binds_the_tool_and_its_exact_arguments() -> None:
    caller = ErpnextCaller("user", "asker@example.com")
    sent = erpnext_caller.with_assertion(
        VECTOR_PRIVATE_KEY, caller, tool="run_report",
        arguments={**VECTOR_ARGUMENTS, ASSERTION_ARG: "written-by-the-model"},
    )
    claims = _claims(sent.pop(ASSERTION_ARG))
    assert sent == VECTOR_ARGUMENTS
    assert claims["args"] == erpnext_caller.arguments_digest(VECTOR_ARGUMENTS)
    assert (claims["aud"], claims["tool"], claims["kind"], claims["sub"]) == (
        "oe_connector", "run_report", "user", "asker@example.com",
    )
    assert claims["exp"] - claims["iat"] == erpnext_caller.LIFETIME_S
    again = erpnext_caller.with_assertion(VECTOR_PRIVATE_KEY, caller, tool="run_report", arguments=VECTOR_ARGUMENTS)
    assert _claims(again[ASSERTION_ARG])["jti"] != claims["jti"]


# ── who is asking ───────────────────────────────────────────────────────────


@pytest.fixture
def roster(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Who ``principal_speaking`` and ``teammate_on_verified_surface`` find."""
    from openexecutive.orchestrator import content_trust, people_tools
    from openexecutive.people import store

    found: dict[str, Any] = {"principal_speaking": False, "teammate": None, "principal": PRINCIPAL}
    monkeypatch.setattr(content_trust, "principal_speaking", lambda _s: found["principal_speaking"])
    monkeypatch.setattr(people_tools, "teammate_on_verified_surface", lambda _s: found["teammate"])
    monkeypatch.setattr(store, "find_principal_person", lambda: found["principal"])
    return found


def _session(**fields: Any) -> SimpleNamespace:
    base = {"unattended": False, "from_web_chat": False, "private_chat": False}
    return SimpleNamespace(**{**base, **fields})


def _refusal(session: Any) -> str:
    with pytest.raises(CallerRefused) as refused:
        caller_for(session)
    return refused.value.reason


def test_the_principal_asks_as_their_people_email(roster: dict[str, Any]) -> None:
    roster["principal_speaking"] = True
    # On any surface that proved it, shared threads included: as in phase 1.
    assert caller_for(_session()) == ErpnextCaller("user", "boss@example.com")
    roster["principal"] = SimpleNamespace(email=None)
    assert _refusal(_session()) == "principal_no_email"


def test_a_teammate_asks_only_where_nobody_else_reads(roster: dict[str, Any]) -> None:
    roster["teammate"] = TEAMMATE
    assert caller_for(_session(private_chat=True)) == ErpnextCaller("user", "ana@example.com")
    assert caller_for(_session(origin_channel="telegram")) == ErpnextCaller("user", "ana@example.com")
    assert _refusal(_session(origin_channel="slack", session_id="slack-thread-1")) == "shared_conversation"
    assert _refusal(_session()) == "shared_conversation"


def test_a_teammate_in_the_web_app_needs_signed_sign_ins(
    roster: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api import caller

    roster["teammate"] = TEAMMATE
    signed_in = _session(from_web_chat=True, web_caller_signed_in=True)
    monkeypatch.setattr(caller, "signing_on", lambda: False)
    # Without signed callers the email is whatever the shared secret's holder sends.
    assert _refusal(signed_in) == "web_unsigned"
    monkeypatch.setattr(caller, "signing_on", lambda: True)
    assert caller_for(signed_in) == ErpnextCaller("user", "ana@example.com")
    assert _refusal(_session(from_web_chat=True)) == "web_unsigned"


def test_an_unverified_speaker_and_no_turn_are_refused(roster: dict[str, Any]) -> None:
    assert _refusal(_session(from_web_chat=True)) == "unverified"
    assert _refusal(None) == "no_session"


def test_a_run_nobody_watches_is_refused(roster: dict[str, Any]) -> None:
    # A scheduled follow-up's text and recipient come from whoever scheduled
    # it: signed as the service user, it would hand them that user's view.
    roster["principal_speaking"] = True
    assert _refusal(_session(unattended=True)) == "unattended"


def test_a_failed_roster_read_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import content_trust

    def boom(_session: Any) -> bool:
        raise RuntimeError("roster unreadable")

    monkeypatch.setattr(content_trust, "principal_speaking", boom)
    assert _refusal(_session(from_web_chat=True)) == "lookup_failed"


# ── the gateway ─────────────────────────────────────────────────────────────


def _result(text: str) -> MagicMock:
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    return r


def _gateway(replies: list[str]) -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=[_result(t) for t in replies])
    gateway._session = session
    gateway.server_names = ("erpnext",)
    return gateway, session.call_tool


@pytest.fixture
def phase2(monkeypatch: pytest.MonkeyPatch, roster: dict[str, Any]) -> Any:
    monkeypatch.setenv("ERPNEXT_CALLER_PRIVATE_KEY", VECTOR_PRIVATE_KEY)
    audited: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event, *_a, **k: audited.append({"event": event, **k.get("details", {})}),
    )
    token = current_session.set(_session(from_web_chat=True))
    yield SimpleNamespace(roster=roster, audited=audited)
    current_session.reset(token)


def test_each_erpnext_call_carries_the_askers_assertion(phase2: Any) -> None:
    phase2.roster["principal_speaking"] = True
    gateway, forwarded = _gateway(['{"rows": []}'])
    args = {"report_name": "Gross Profit", ASSERTION_ARG: "v1.forged.by-the-model"}
    out = asyncio.run(gateway.call_tool({"name": "erpnext__run_report", "arguments": args}))
    assert out == '{"rows": []}'
    sent = forwarded.await_args.args[1]["arguments"]
    claims = _claims(sent.pop(ASSERTION_ARG))
    assert sent == {"report_name": "Gross Profit"}
    assert (claims["tool"], claims["sub"]) == ("run_report", "boss@example.com")
    # The model's own arguments are left as it wrote them.
    assert args[ASSERTION_ARG] == "v1.forged.by-the-model"
    # The turn's reply now quotes ERPNext: it stays out of shared memory.
    assert current_session.get().read_erpnext is True


def test_a_refused_call_does_not_mark_the_turn(phase2: Any) -> None:
    gateway, _forwarded = _gateway([])
    asyncio.run(gateway.call_tool({"name": "erpnext__run_report", "arguments": {}}))
    assert getattr(current_session.get(), "read_erpnext", False) is False


def test_a_retried_call_gets_a_fresh_assertion(phase2: Any) -> None:
    phase2.roster["principal_speaking"] = True
    name = "erpnext__get_record"
    undiscovered = f"Error: Tool '{name}' has not been discovered via search_tools."
    hit = f"## {name}\n**Description:** x\n**Parameters:**\n```json\n{{}}\n```\n"
    gateway, forwarded = _gateway([undiscovered, hit, '{"doc": {}}'])
    args = {"doctype": "Customer", "name": "Acme"}
    assert asyncio.run(gateway.call_tool({"name": name, "arguments": args})) == '{"doc": {}}'
    first, _search, second = (c.args[1].get("arguments") for c in forwarded.await_args_list)
    assert _claims(first[ASSERTION_ARG])["jti"] != _claims(second[ASSERTION_ARG])["jti"]


def test_a_refused_asker_never_reaches_erpnext(phase2: Any) -> None:
    phase2.roster["teammate"] = TEAMMATE
    current_session.set(_session())  # a shared Slack thread
    gateway, forwarded = _gateway(['{"rows": []}'])
    out = asyncio.run(gateway.call_tool({"name": "erpnext__run_report", "arguments": {}}))
    assert "private conversation" in json.loads(out)["error"]
    assert forwarded.await_count == 0
    assert phase2.audited == [{
        "event": "integration_outbound_blocked", "tool": "erpnext__run_report",
        "field": "caller", "address": "<shared_conversation>",
    }]


def test_a_malformed_key_refuses_every_erpnext_call(phase2: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    phase2.roster["principal_speaking"] = True
    monkeypatch.setenv("ERPNEXT_CALLER_PRIVATE_KEY", "not-a-key")
    gateway, forwarded = _gateway(['{"rows": []}'])
    out = asyncio.run(gateway.call_tool({"name": "erpnext__run_report", "arguments": {}}))
    assert "ERPNEXT_CALLER_PRIVATE_KEY" in json.loads(out)["error"]
    assert "not-a-key" not in out
    assert forwarded.await_count == 0


def test_phase_one_drops_an_assertion_the_model_wrote(roster: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ERPNEXT_CALLER_PRIVATE_KEY", raising=False)
    roster["principal_speaking"] = True
    gateway, forwarded = _gateway(['{"rows": []}'])
    asyncio.run(gateway.call_tool({
        "name": "erpnext__run_report",
        "arguments": {"report_name": "Gross Profit", ASSERTION_ARG: "v1.x.y"},
    }))
    assert forwarded.await_args.args[1]["arguments"] == {"report_name": "Gross Profit"}


def test_an_erpnext_call_needs_object_arguments(phase2: Any) -> None:
    phase2.roster["principal_speaking"] = True
    gateway, forwarded = _gateway([])
    out = asyncio.run(gateway.call_tool({"name": "erpnext__run_report", "arguments": ["x"]}))
    assert "JSON object" in out
    assert forwarded.await_count == 0


# ── what the prompt says ────────────────────────────────────────────────────


def test_the_prompt_says_whose_view_each_answer_is(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.config import get_settings

    monkeypatch.delenv("ERPNEXT_CALLER_PRIVATE_KEY", raising=False)
    phase1 = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    monkeypatch.setenv("ERPNEXT_CALLER_PRIVATE_KEY", VECTOR_PRIVATE_KEY)
    phase2 = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    assert "for the principal only" in phase1
    assert "runs as the person asking" in phase2 and "for the principal only" not in phase2
    assert "not available in runs nobody is watching" in phase2
    assert VECTOR_PRIVATE_KEY.split(":")[1] not in phase2


def test_a_conversation_that_read_erpnext_stays_out_of_shared_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    # A later turn can restate the figures from history, and a channel
    # adapter builds a new Session per message: the audit log remembers.
    from openexecutive.orchestrator.executive import _conversation_read_erpnext

    rows: dict[str, list[Any]] = {"telegram:42": [object()]}
    asked: list[dict[str, Any]] = []

    class _Audit:
        def query(self, **kw: Any) -> list[Any]:
            asked.append(kw)
            return rows.get(kw["session_id"], [])

    monkeypatch.setattr("openexecutive.audit.logger.get_audit_logger", lambda: _Audit())
    fresh = SimpleNamespace(session_id="telegram:42", read_erpnext=False)
    assert _conversation_read_erpnext(fresh) is True
    assert fresh.read_erpnext is True
    assert asked[0]["q"] == "mcp:erpnext__" and asked[0]["event_type"] == "tool_invocation"
    assert _conversation_read_erpnext(SimpleNamespace(session_id="web-1", read_erpnext=False)) is False

    def broken() -> Any:
        raise RuntimeError("db locked")

    monkeypatch.setattr("openexecutive.audit.logger.get_audit_logger", broken)
    assert _conversation_read_erpnext(SimpleNamespace(session_id="web-1", read_erpnext=False)) is True

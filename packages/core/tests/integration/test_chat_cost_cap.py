"""The web chat turn at a cost cap (audit.cost_caps): the SSE route binds one
turn for the whole stream, a turn the cap stops mid-loop streams what it
wrote and the stop sentence once, and nothing after the stop replaces it."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.audit import cost_caps
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator import executive as exec_mod
from openexecutive.providers.capped import CappedProvider

BEFORE = "I will check actual stock in every warehouse first, then compare it with the reorder level."


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    return db_path


@pytest.fixture()
def patched_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder
    from openexecutive.utils import session_title

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_k: "")

    async def _no_title(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_title, "generate_session_title", _no_title)
    monkeypatch.setattr(cost_caps, "caps", lambda: cost_caps.Caps(0, 0, 0, turn=1.5))
    monkeypatch.setattr(cost_caps, "worst_case", lambda model, kwargs: 1.0)
    monkeypatch.setattr(cost_caps, "_write", lambda *a, **k: None)
    monkeypatch.setattr(cost_caps, "_record", lambda exc: None)


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def _executive(monkeypatch: pytest.MonkeyPatch, stream: Any) -> None:
    class _Fake:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        stream_chat = stream

    monkeypatch.setattr(exec_mod, "Executive", _Fake)


class _Stream:
    def __init__(self, final: Any) -> None:
        self._final = final
        self._deltas = [
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=b.text))
            for b in final.content if b.type == "text"
        ]

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        if not self._deltas:
            raise StopAsyncIteration
        return self._deltas.pop(0)

    async def get_final_message(self) -> Any:
        return self._final


class _Backend:
    def __init__(self, finals: list[Any]) -> None:
        self.finals = finals

    def messages_stream(self, **_kw: Any) -> _Stream:
        return _Stream(self.finals.pop(0))


def test_an_sse_turn_capped_mid_loop_streams_one_reply_ending_with_the_stop_sentence(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = exec_mod.Executive
    text = SimpleNamespace(type="text", text=BEFORE)
    tool = SimpleNamespace(type="tool_use", id="tu1", name="no_such_tool", input={})
    rounds = [
        SimpleNamespace(content=[text, tool], stop_reason="tool_use", usage=None),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="Never sent.")], stop_reason="end_turn", usage=None),
    ]
    provider = CappedProvider(_Backend(rounds))  # type: ignore[arg-type]
    monkeypatch.setattr(exec_mod, "get_provider", lambda model: provider)
    turns: list[Any] = []

    async def stream(self: Any, **_kw: Any) -> AsyncIterator[Any]:
        turns.append(cost_caps.current_turn())
        async for item in real()._stream_agent_loop(
            system_blocks=[], messages=[{"role": "user", "content": "x"}],
            model="claude-test", workspace_mode="team", principal_role_tag="", turn_id="t-1",
        ):
            yield item
        # A step after the stop fails: the reply already said it stopped.
        raise RuntimeError("after the stop")

    _executive(monkeypatch, stream)
    app = FastAPI()
    app.include_router(chat_route.router)
    events = _events(TestClient(app).post("/chat", json={"message": "Stock?"}).text)
    reply = "".join(e["content"] for e in events if e["type"] == "chunk")
    assert reply == f"{BEFORE}\n\n{cost_caps.TURN_STOP_REPLY}"
    assert [e["type"] for e in events].count("error") == 0
    assert [e["type"] for e in events].count("done") == 1
    assert turns[0] is not None and turns[0].closed and turns[0].stopped_by_cap

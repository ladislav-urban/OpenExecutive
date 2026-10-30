"""The web chat's reply names the product, not its parts, when the install has
a name (connected_systems.ProductReplyStream in the `/chat` SSE route). Pilot,
Oct 30, 2026, in the desk's Chats: "worth about $38.4k, in ERPNext". The panel
had the backstop (#107); the streamed chat didn't."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.prompts.connected_systems import ProductReplyStream, in_product_reply

REPLIES = [
    "This is from 10 lost quotations this year, worth about $38.4k, in ERPNext. Project cancelled: 5.",
    "ERPNext only records what was entered. Ask in Open Executive or OpenExecutive itself.",
    "Done. ERPNext's records show two. the   ERPNext list too.",
    "See https://docs.erpnext.com/x, support@erpnext.com, ERPNEXT-01 and 'ERPNext Services Ltd'.",
    "An ERPNextual word stays, and so does ERPNext-01, but ERPNext",
    "Run `bench install-app erpnext`, see https://x.example/search?q=ERPNext&a=1 and #ERPNext.",
    "Long enough to be trimmed before the word. " * 4 + "Then\n\nERPNext and the\n\nERPNext.",
]


@pytest.mark.parametrize(
    ("reply", "shown"),
    [
        # The name as written only: a command, a path or a query stays.
        ("pip install erpnext; docker exec erpnext bash", "pip install erpnext; docker exec erpnext bash"),
        ("See https://x.example/search?q=ERPNext&scope=all.", "See https://x.example/search?q=ERPNext&scope=all."),
        ("See https://x.example/#ERPNext now.", "See https://x.example/#ERPNext now."),
        # "The" at a sentence's start joins the word, as "the" does.
        ("The ERPNext records show two. The ERPNext list too.", "The ERP records show two. The ERP list too."),
        # A paragraph break before the word is kept.
        ("Read it in the\n\nERPNext screens.", "Read it in the\n\nThe ERP screens."),
    ],
)
def test_code_urls_and_breaks_stay(monkeypatch: pytest.MonkeyPatch, reply: str, shown: str) -> None:
    _named(monkeypatch, "Dynocortex")
    assert in_product_reply(reply) == shown


def test_a_long_reply_in_tiny_chunks_stays_linear() -> None:
    import time

    reply = "Some ordinary text, with ERPNext now and then. " * 6000
    t0 = time.monotonic()
    _streamed(list(reply))
    assert time.monotonic() - t0 < 3.0


def _named(monkeypatch: pytest.MonkeyPatch, name: str | None) -> None:
    from openexecutive.utils import product_name as product

    monkeypatch.setattr(product, "configured_product_name", lambda: name)


def _streamed(chunks: list[str]) -> str:
    stream = ProductReplyStream("Dynocortex")
    return "".join(stream.feed(c) for c in chunks) + stream.flush()


@pytest.mark.parametrize("reply", REPLIES)
def test_any_split_gives_the_same_reply_as_the_whole(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    _named(monkeypatch, "Dynocortex")
    whole = in_product_reply(reply)
    if reply is REPLIES[0]:
        assert whole.endswith("worth about $38.4k, in the ERP. Project cancelled: 5.")
    for at in range(len(reply) + 1):
        assert _streamed([reply[:at], reply[at:]]) == whole, at
    assert _streamed(list(reply)) == whole


def test_the_stream_holds_back_only_a_word(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = ProductReplyStream("Dynocortex")
    first = stream.feed("A long first sentence with nothing to change in it. Then ERPN")
    assert first.startswith("A long first sentence") and "ERPN" not in first
    second = stream.feed("ext says so.")
    assert "ERP" not in second
    assert first + second + stream.flush() == (
        "A long first sentence with nothing to change in it. Then the ERP says so."
    )


def test_a_long_reply_streams_quickly() -> None:
    import time

    reply = "ERPNext. " * 3000
    t0 = time.monotonic()
    out = _streamed([reply[i:i + 7] for i in range(0, len(reply), 7)])
    assert time.monotonic() - t0 < 1.0
    assert out.count("The ERP.") == 3000


# ── The route ────────────────────────────────────────────────────────────────


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

    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())

    from openexecutive.knowledge import retriever

    monkeypatch.setattr(retriever, "retrieve", lambda query, specialist_name=None, store=None, **_k: "")

    from openexecutive.utils import session_title as _title_mod

    async def _no_title(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(_title_mod, "generate_session_title", _no_title)


CHIP: dict[str, Any] = {"type": "activity", "label": "Reading the ERP…", "tool": "x", "iteration": 1}


def _client(monkeypatch: pytest.MonkeyPatch, items: list[Any]) -> TestClient:
    from openexecutive.orchestrator import executive as exec_mod

    class _FakeExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[Any]:
            for item in items:
                yield item

    monkeypatch.setattr(exec_mod, "Executive", _FakeExecutive)
    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def _saved(db: Path) -> str:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT content FROM chat_messages WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return str(row[0])


def test_the_streamed_reply_and_the_saved_one_name_the_product(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _named(monkeypatch, "Dynocortex")
    client = _client(monkeypatch, ["Worth about $38.4k, in ERP", "Next. Ask Open", " Executive."])
    events = _events(client.post("/chat", json={"message": "Why do we lose deals?"}).text)
    shown = "".join(e["content"] for e in events if e.get("type") == "chunk")
    assert shown == "Worth about $38.4k, in the ERP. Ask Dynocortex."
    assert _saved(temp_db) == shown


def test_held_back_text_goes_out_before_the_next_step(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _named(monkeypatch, "Dynocortex")
    client = _client(monkeypatch, ["Let me check ERPNext", CHIP, " for you."])
    events = _events(client.post("/chat", json={"message": "hi"}).text)
    kinds = [e["type"] for e in events]
    step = kinds.index("activity")
    before = "".join(e["content"] for e in events[:step] if e["type"] == "chunk")
    after = "".join(e["content"] for e in events[step:] if e["type"] == "chunk")
    assert (before, after) == ("Let me check the ERP", " for you.")


def test_without_a_name_the_reply_streams_as_written(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _named(monkeypatch, None)
    client = _client(monkeypatch, ["Worth about $38.4k, in ERP", "Next."])
    events = _events(client.post("/chat", json={"message": "hi"}).text)
    assert [e["content"] for e in events if e.get("type") == "chunk"] == ["Worth about $38.4k, in ERP", "Next."]
    assert _saved(temp_db) == "Worth about $38.4k, in ERPNext."

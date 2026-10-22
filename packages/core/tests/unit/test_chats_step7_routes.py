"""What the Chats screens read from the API (oe_connector's single-UI step 7):
who may write in a conversation, its lines as a model, the list's groups in
the caller's zone, the topic layout, and the attachment limits."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openexecutive.memory import chat_topics, session_store
from openexecutive.people import store as people_store

from .test_sessions_route_scoping import client  # noqa: F401  (the fixture)

ME = {"x-caller-email": "alex@example.com"}


@pytest.fixture()
def people(client: TestClient) -> dict[str, int]:  # noqa: F811
    return {
        "alex": people_store.upsert_person(full_name="Alex", is_principal=True, email="alex@example.com"),
        "sabin": people_store.upsert_person(full_name="Sabin", email="sabin@example.com"),
    }


def _chat(session_id: str, owner: int, *, host: bool = False, days_ago: int = 0) -> None:
    when = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    session_store.create_session(session_id, session_id, when, caller_person_id=owner)
    session_store.save_message(session_id, "user", "Hello", sender_surface="host" if host else None)
    session_store.save_message(session_id, "assistant", "Hi there")


def test_host_started_says_which_conversations_a_host_may_write_in(client: TestClient, people: dict[str, int]) -> None:  # noqa: F811
    _chat("mine", people["alex"])
    _chat("from-the-desk", people["alex"], host=True)
    listed = {s["session_id"]: s for s in client.get("/sessions", headers=ME).json()}
    assert listed["mine"]["host_started"] is False
    assert listed["from-the-desk"]["host_started"] is True
    assert client.get("/sessions/from-the-desk", headers=ME).json()["host_started"] is True
    found = client.get("/sessions", params={"q": "Hello"}, headers=ME).json()
    assert {s["session_id"]: s["host_started"] for s in found} == {"mine": False, "from-the-desk": True}


def test_messages_have_a_model_and_mark_a_hosts_lines(client: TestClient, people: dict[str, int]) -> None:  # noqa: F811
    _chat("from-the-desk", people["alex"], host=True)
    lines = client.get("/sessions/from-the-desk/messages", headers=ME).json()
    assert [(m["role"], m.get("from_host")) for m in lines] == [("user", True), ("assistant", None)]
    assert isinstance(lines[1]["id"], int)
    _chat("mine", people["alex"])
    assert "from_host" not in client.get("/sessions/mine/messages", headers=ME).json()[0]


def test_each_chat_has_its_date_group_in_the_callers_zone(client: TestClient, people: dict[str, int]) -> None:  # noqa: F811
    _chat("today", people["alex"])
    _chat("last-week", people["alex"], days_ago=5)
    _chat("old", people["alex"], days_ago=90)
    session_store.set_pinned("old", True)
    groups = {
        s["session_id"]: s["date_group"]
        for s in client.get("/sessions", params={"tz": "Europe/Prague"}, headers=ME).json()
    }
    assert groups["today"] == {"key": "today", "label": "Today", "order": 1}
    assert groups["last-week"]["key"] == "prev7"
    assert groups["old"]["key"] == "pinned"


def test_the_topic_layout_covers_only_the_callers_own_chats(client: TestClient, people: dict[str, int]) -> None:  # noqa: F811
    for sid in ("a", "b", "c"):
        _chat(sid, people["alex"])
    _chat("theirs", people["sabin"])
    for sid in ("a", "b"):
        chat_topics.file_session(sid, people["alex"], name="Acme renewal")
    body = client.post(
        "/sessions/topics/layout", params={"tz": "UTC"}, json={"session_ids": ["a", "b", "c", "theirs", "nope"]},
        headers=ME,
    ).json()
    entries = [e for band in body["bands"] for e in band["entries"]]
    assert [(e["label"], sorted(e["session_ids"])) for e in entries] == [("Acme renewal", ["a", "b"])]
    assert entries[0]["active_label"] == "Today"
    assert body["other"]["session_ids"] == ["c"]
    # Someone else's id, or one that doesn't exist, is simply left out.
    assert "theirs" not in str(body) and "nope" not in str(body)
    assert client.post("/sessions/topics/layout", json={"session_ids": ["a"]},
                       headers={"x-caller-email": "stranger@example.com"}).json() == {"bands": []}


def test_the_attachment_limits_come_from_the_backend() -> None:
    from openexecutive.api.routes import chat as chat_route
    from openexecutive.integrations.attachments import EXTRACTABLE_SUFFIXES, SUPPORTED_IMAGE_TYPES

    limits = asyncio.run(chat_route.chat_limits())
    assert limits.max_files == chat_route._MAX_FILES_PER_TURN == 5
    assert limits.max_bytes_per_file == chat_route._MAX_BYTES_PER_FILE
    assert set(limits.image_types) == SUPPORTED_IMAGE_TYPES
    assert set(limits.document_suffixes) == EXTRACTABLE_SUFFIXES
    assert limits.accept.split(",") == [*limits.image_types, *limits.document_suffixes]


def test_the_chat_stream_events_are_in_the_api_description() -> None:
    from openexecutive.api.chat_events import EVENT_TYPES
    from openexecutive.api.main import create_app

    spec = create_app().openapi()
    for path in ("/chat", "/chat/upload"):
        content = spec["paths"][path]["post"]["responses"]["200"]["content"]
        assert list(content) == ["text/event-stream"]
    union = spec["components"]["schemas"]["ChatStreamEventSchema"]
    named = {ref["$ref"].rsplit("/", 1)[1] for ref in union["oneOf"]}
    types = {
        spec["components"]["schemas"][name]["properties"]["type"]["const"]
        for name in named
    }
    assert types == set(EVENT_TYPES)
    messages = spec["paths"]["/sessions/{session_id}/messages"]["get"]["responses"]["200"]["content"]
    assert messages["application/json"]["schema"]["items"]["$ref"].endswith("/ChatMessage")


def test_tmp_db_is_used(client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    # The fixture works in tmp_path, so nothing here touches a real database.
    assert Path.cwd() == tmp_path

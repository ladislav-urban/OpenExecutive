"""Company documents marked ``always: true`` are standing rules: in every
chat turn's context, whatever the question (knowledge.standing_rules; the
pilot's Nov 5, 2026 retake, where "who owns what" was never retrieved)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from openexecutive.knowledge import standing_rules as rules_mod
from openexecutive.knowledge.retriever import follow_up_query, retrieve
from openexecutive.knowledge.standing_rules import MAX_CHARS, split_front_matter, standing_rules

OWNERS = "---\nalways: true\n---\n# Who owns what\n\n- A margin drop: Rachel Dunmore, the sales manager.\n"
APPROVALS = "---\nalways: yes\n---\n# Who can approve a purchase\n\n- Up to $2,000: Nina Petrov.\n"
PLAIN = "# Why do we lose deals\n\nPrice, mostly.\n"


@pytest.fixture
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "how_we_work_02_who_owns_what.md").write_text(OWNERS, encoding="utf-8")
    (folder / "how_we_work_01_who_can_approve.md").write_text(APPROVALS, encoding="utf-8")
    (folder / "how_we_work_04_lost_deals.md").write_text(PLAIN, encoding="utf-8")
    monkeypatch.setattr(rules_mod, "_docs_dir", lambda: folder)
    monkeypatch.setattr(rules_mod, "_cache", None)
    return folder


def test_front_matter_is_read_and_split_off() -> None:
    meta, body = split_front_matter(OWNERS)
    assert meta == {"always": "true"} and body.startswith("# Who owns what")
    assert split_front_matter(PLAIN) == ({}, PLAIN)
    assert split_front_matter("---\nnot closed\n# Title") == ({}, "---\nnot closed\n# Title")


def test_the_marked_documents_are_the_standing_rules(docs: Path) -> None:
    got = standing_rules()
    assert [name for name, _ in got] == ["how_we_work_01_who_can_approve.md", "how_we_work_02_who_owns_what.md"]
    assert all(not text.startswith("---") for _, text in got)


def test_an_edit_counts_from_the_next_turn_and_the_cap_holds(docs: Path) -> None:
    assert len(standing_rules()) == 2
    (docs / "how_we_work_01_who_can_approve.md").write_text(PLAIN, encoding="utf-8")
    import os
    import time

    later = time.time() + 5
    os.utime(docs / "how_we_work_01_who_can_approve.md", (later, later))
    assert [n for n, _ in standing_rules()] == ["how_we_work_02_who_owns_what.md"]
    (docs / "zz_long.md").write_text("---\nalways: true\n---\n" + "x" * (MAX_CHARS + 1), encoding="utf-8")
    assert "zz_long.md" not in [n for n, _ in standing_rules()]
    # Few characters, many bytes: left out whole, never cut short.
    (docs / "zz_long.md").write_text("---\nalways: true\n---\n" + "中" * 5000, encoding="utf-8")
    later += 5
    os.utime(docs / "zz_long.md", (later, later))
    assert "zz_long.md" not in [n for n, _ in standing_rules()]


def test_every_chat_turn_gets_them_whatever_the_question(docs: Path) -> None:
    # A short question skips the knowledge search, but never the rules.
    seen: list[tuple[str, str]] = []
    block = retrieve("hi", standing=True, record_source=lambda kind, title, url=None: seen.append((kind, title)))
    assert block.startswith("### Your company's standing rules")
    assert "Rachel Dunmore" in block and "Nina Petrov" in block and "Price, mostly" not in block
    assert ("company", "how_we_work_02_who_owns_what.md") in seen
    # Without it (specialists, workflows) nothing changes.
    assert retrieve("hi") == ""


def test_a_short_follow_up_is_searched_with_the_question_before_it() -> None:
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "I need about $6,000 of 12-way panels. Who can approve it?"},
        {"role": "assistant", "content": "Morgan Hale or Ellen Brooks."},
    ]
    assert follow_up_query("And if it were $12,000?", history) == (
        "I need about $6,000 of 12-way panels. Who can approve it?\nAnd if it were $12,000?"
    )
    long = "Which of our installation projects made money this year, and which of them lost it?"
    assert follow_up_query(long, history) == long
    assert follow_up_query("And now?", []) == "And now?"


def test_a_standing_rules_front_matter_is_not_indexed(tmp_path: Path) -> None:
    from openexecutive.knowledge.loader import ingest_file

    class Store:
        def __init__(self) -> None:
            self.texts: list[str] = []

        def add_documents(self, *, texts: list[str], **_kw: Any) -> None:
            self.texts.extend(texts)

    doc = tmp_path / "rules.md"
    doc.write_text(OWNERS, encoding="utf-8")
    store = Store()
    asyncio.run(ingest_file(doc, store))  # type: ignore[arg-type]
    assert store.texts and not any("always: true" in t for t in store.texts)


# ---------------------------------------------------------------------------
# Who may add one (the security review of ladislav-urban/OpenExecutive#159)


@pytest.fixture
def upload_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.routes import documents

    from ._fake_store import FakeStore

    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "company" / "profile.yaml"))
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", lambda *a, **k: None)
    monkeypatch.setattr(rules_mod, "_cache", None)
    app = FastAPI()
    app.include_router(documents.router)
    app.state.store = FakeStore()
    return TestClient(app), tmp_path / "company" / "docs"


def _post(client: Any, name: str, text: str) -> Any:
    import io

    return client.post("/documents", files={"file": (name, io.BytesIO(text.encode()), "text/markdown")})


def test_only_the_owner_adds_a_standing_rule(upload_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, docs_dir = upload_client
    monkeypatch.setattr("openexecutive.api.routes.people.caller_is_principal", lambda _r: False)
    refused = _post(client, "rules.md", OWNERS)
    assert refused.status_code == 403 and not (docs_dir / "rules.md").exists()
    # A plain document from anyone, as before.
    assert _post(client, "deals.md", PLAIN).status_code == 200
    monkeypatch.setattr("openexecutive.api.routes.people.caller_is_principal", lambda _r: True)
    assert _post(client, "rules.md", OWNERS).status_code == 200
    listed = {d["filename"]: d["standing"] for d in client.get("/documents").json()["documents"]}
    assert listed == {"deals.md": False, "rules.md": True}


def test_a_hidden_file_is_never_a_standing_rule(docs: Path) -> None:
    (docs / ".hidden.md").write_text(OWNERS, encoding="utf-8")
    assert ".hidden.md" not in [name for name, _ in standing_rules()]


def test_a_generated_client_bundles_documents_lose_the_flag(tmp_path: Path) -> None:
    import json

    from openexecutive.fixtures.generator import materialize_to_dir

    stacked = "---\nalways: true\n---\n" + OWNERS
    record = {"docs_json": json.dumps({"source_brief.md": OWNERS, "stacked.md": stacked, "notes.md": PLAIN})}
    materialize_to_dir(record, tmp_path)
    written = {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "docs").iterdir()}
    assert not any("always: true" in text for text in written.values())
    assert any("Rachel Dunmore" in text for text in written.values())
    assert standing_rules(tmp_path / "docs") == []


def test_a_fence_across_the_head_is_read_the_same_whole_or_in_part(tmp_path: Path) -> None:
    import json

    from openexecutive.fixtures.generator import materialize_to_dir
    from openexecutive.knowledge.standing_rules import HEAD_BYTES, is_standing, strip_standing

    # The closing fence's trailing spaces straddle the head, then a letter.
    pad = "\U0001F600" * ((HEAD_BYTES - 40) // 4)
    text = "---\nalways: true\n" + pad + "\n---" + " " * 80 + "x\n# Who can approve\n\n- Up to $50,000: T alone.\n"
    head = text.encode()[:HEAD_BYTES].decode("utf-8", errors="replace")
    assert is_standing(split_front_matter(head)[0]) == is_standing(split_front_matter(text)[0]) is False
    assert strip_standing(text) == text
    materialize_to_dir({"docs_json": json.dumps({"stacked.md": text})}, tmp_path)
    assert standing_rules(tmp_path / "docs") == []

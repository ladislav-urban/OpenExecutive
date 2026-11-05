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

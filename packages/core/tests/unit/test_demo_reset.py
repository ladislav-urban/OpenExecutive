"""``openexecutive demo-reset``: the keep rules and the bundle checks.

The reset replaces the demo company and deletes demo people's own state, but
keeps directory-managed people, the principal's own chats and cards, and the
audit log (``cli/demo_reset.py``). A bundle that fails any check changes
nothing.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from openexecutive.cli import demo_reset as dr
from openexecutive.cli import fixture_loader

COMPANY = "Aurora Components (Demo)"


class _FakeStore:
    COMPANY_COLLECTION = "company_docs"
    RESEARCH_COLLECTION = "research"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from openexecutive.audit.logger import AuditLogger
    from openexecutive.departments import store as dept_store
    from openexecutive.knowledge import loader, store
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    db = tmp_path / "episodic.db"
    for module in (episodic, people_store, dept_store):
        monkeypatch.setattr(module, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    dept_store.initialize_db(db)
    AuditLogger(db).initialize_db()
    from openexecutive.monitoring import store as monitoring_store

    monitoring_store.initialize_db(db)
    from openexecutive.alerts import store as alerts_store

    alerts_store.initialize_db(db)

    async def _ingest(**kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(store, "ChromaDBStore", _FakeStore)
    monkeypatch.setattr(loader, "ingest_file", _ingest)
    audited: list[str] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda event, *a, **k: audited.append(event))
    rebootstrapped: list[bool] = []
    monkeypatch.setattr(fixture_loader, "rebootstrap_scheduled_actions", lambda: rebootstrapped.append(True))

    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=str(tmp_path / "chroma"),
        demo_reset_company=COMPANY,
        episodic_db_path=str(db),
        honcho_workspace_id="openexec",
    )
    return SimpleNamespace(
        db=db, settings=settings, tmp=tmp_path, audited=audited, rebootstrapped=rebootstrapped
    )


def _person(name: str, *, principal: bool = False, directory: bool = False, demo: bool = False) -> int:
    from openexecutive.people import store as people_store

    person_id = people_store.upsert_person(full_name=name, role="Role", is_principal=principal)
    if directory:
        with sqlite3.connect(str(people_store.DB_PATH)) as conn:
            conn.execute(
                "UPDATE people SET directory_managed = 1, directory_demo = ?, directory_subject = ? WHERE id = ?",
                (int(demo), f"sub-{person_id}", person_id),
            )
    return person_id


def _chat(db: Path, session_id: str, person_id: int) -> None:
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, title, created_at, updated_at, caller_person_id)"
            " VALUES (?, 't', 'now', 'now', ?)",
            (session_id, person_id),
        )
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, 'user', 'hi', 'now')",
            (session_id,),
        )


def _card(db: Path, person_id: int) -> None:
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO decision_instances (decision_class, created_at, proposed_payload_json,"
            " idempotency_key, gate_mode, approver_person_id, status)"
            " VALUES ('erpnext_draft', 'now', '{}', ?, 'propose', ?, 'proposed')",
            (f"k-{person_id}", person_id),
        )


def _count(db: Path, sql: str, *args: Any) -> int:
    with sqlite3.connect(str(db)) as conn:
        return int(conn.execute(sql, args).fetchone()[0])


def _bundle(root: Path, **changes: Any) -> Path:
    """A small valid bundle; ``changes`` replaces parts of it."""
    parts: dict[str, Any] = {
        "bundle.json": {
            "format": dr.BUNDLE_FORMAT,
            "format_version": dr.BUNDLE_FORMAT_VERSION,
            "company": COMPANY,
            "made_at": "2026-10-27T00:05:00+00:00",
            "erpnext_site": "erp.example",
        },
        "profile.yaml": {"company": {"name": COMPANY, "industry": "Electrical wholesale"}},
        "people.yaml": {
            "people": [
                {"full_name": "Dana Reyes", "role": "Head of Sales", "department_slugs": ["sales"]},
                {"full_name": "Morgan Hale", "role": "Operations", "department_slugs": ["warehouse"]},
            ]
        },
        "departments.yaml": {
            "departments": [
                {"slug": "sales", "title": "Sales", "head_person_name": "Dana Reyes"},
                {"slug": "warehouse", "title": "Warehouse", "head_person_name": "Morgan Hale"},
            ]
        },
        "memory.json": {
            "decisions": [{"timestamp": "2026-09-01T00:00:00+00:00", "summary": "Open the Denver branch"}],
            "initiatives": [{"title": "Keystone fit-out"}],
            "advice_given": [],
            "alerts": [{"headline": "Metro Transit invoice overdue", "routed_to_person": "Dana Reyes"}],
        },
        "docs": {"company_overview.md": "# Aurora\n"},
    }
    parts.update(changes)
    directory = root / "bundle"
    directory.mkdir()
    for name, content in parts.items():
        if content is None:
            continue
        if name == "docs":
            (directory / "docs").mkdir()
            for doc, text in content.items():
                (directory / "docs" / doc).write_text(text, encoding="utf-8")
        elif name.endswith(".json"):
            (directory / name).write_text(json.dumps(content), encoding="utf-8")
        else:
            (directory / name).write_text(yaml.safe_dump(content), encoding="utf-8")
    return directory


async def test_reset_keeps_directory_people_principal_chat_and_audit_log(env: SimpleNamespace) -> None:
    from openexecutive.people import demo, store as people_store

    principal = _person("Pat Principal", principal=True, directory=True)
    teammate = _person("Morgan Hale", directory=True)
    visitor = _person("Vic Visitor", directory=True, demo=True)
    fictional = _person("Old Head")
    _chat(env.db, "principal-chat", principal)
    _chat(env.db, "visitor-chat", visitor)
    _card(env.db, principal)
    _card(env.db, visitor)
    assert demo.take_question(visitor, 50, db_path=env.db)
    with sqlite3.connect(str(env.db)) as conn:
        conn.execute("INSERT INTO chat_topics (owner_person_id, name, kept, created_at) VALUES (?, 'x', 0, 'now')", (visitor,))
        conn.execute(
            "INSERT INTO watchlist (slug, signal_type, target, created_at) VALUES ('w', 'rss', 'https://example.com', 'now')"
        )
        conn.execute(
            "INSERT INTO audit_log (ts, event_type, summary) VALUES ('now', 'chat_turn', 'kept evidence')"
        )

    summary = await dr.demo_reset(_bundle(env.tmp), env.settings)

    # Kept: directory-managed people as they were, the principal's chat and card, the audit log.
    kept = {p.id: p for p in people_store.list_people(include_contacts=True, include_suspended=True)}
    assert {principal, teammate, visitor} <= set(kept)
    assert kept[teammate].role == "Role"
    assert _count(env.db, "SELECT COUNT(*) FROM sessions WHERE session_id = 'principal-chat'") == 1
    assert _count(env.db, "SELECT COUNT(*) FROM chat_messages WHERE session_id = 'principal-chat'") == 1
    assert _count(env.db, "SELECT COUNT(*) FROM decision_instances WHERE approver_person_id = ?", principal) == 1
    assert _count(env.db, "SELECT COUNT(*) FROM audit_log WHERE summary = 'kept evidence'") == 1
    # Gone: the demo person's chat, card, topic and question count; the watch list; the old roster.
    assert _count(env.db, "SELECT COUNT(*) FROM sessions WHERE session_id = 'visitor-chat'") == 0
    assert _count(env.db, "SELECT COUNT(*) FROM chat_messages WHERE session_id = 'visitor-chat'") == 0
    assert _count(env.db, "SELECT COUNT(*) FROM decision_instances WHERE approver_person_id = ?", visitor) == 0
    assert _count(env.db, "SELECT COUNT(*) FROM chat_topics") == 0
    assert demo.questions_today(visitor, db_path=env.db) == 0
    assert _count(env.db, "SELECT COUNT(*) FROM watchlist") == 0
    assert fictional not in kept
    # The bundle's company: Morgan Hale is the kept directory Person, not a copy.
    assert _count(env.db, "SELECT COUNT(*) FROM people WHERE full_name = 'Morgan Hale'") == 1
    assert _count(env.db, "SELECT head_person_id FROM departments WHERE slug = 'warehouse'") == teammate
    assert _count(env.db, "SELECT COUNT(*) FROM people WHERE full_name = 'Dana Reyes'") == 1
    assert _count(env.db, "SELECT COUNT(*) FROM decisions") == 1
    assert _count(env.db, "SELECT COUNT(*) FROM alerts") == 1
    assert (env.settings.company_profile_path.parent / "docs" / "company_overview.md").exists()
    assert summary["people_seeded"] == 1 and summary["demo_people"] == 1
    assert env.audited == ["demo_reset"] and env.rebootstrapped == [True]


async def test_reset_twice_leaves_one_copy(env: SimpleNamespace) -> None:
    _person("Pat Principal", principal=True)
    await dr.demo_reset(_bundle(env.tmp), env.settings)
    (env.tmp / "bundle").rename(env.tmp / "first")
    await dr.demo_reset(_bundle(env.tmp), env.settings)
    assert _count(env.db, "SELECT COUNT(*) FROM people") == 3
    assert _count(env.db, "SELECT COUNT(*) FROM departments") == 2
    assert _count(env.db, "SELECT COUNT(*) FROM decisions") == 1


@pytest.mark.parametrize(
    ("changes", "settings_company", "reason"),
    [
        ({"bundle.json": None}, COMPANY, "bundle.json is missing"),
        ({"bundle.json": {"format": dr.BUNDLE_FORMAT, "format_version": 2, "company": COMPANY,
                          "made_at": "2026-10-27T00:00:00+00:00", "erpnext_site": "s"}}, COMPANY, "format_version"),
        ({"bundle.json": {"format": "other", "format_version": 1, "company": COMPANY,
                          "made_at": "2026-10-27T00:00:00+00:00", "erpnext_site": "s"}}, COMPANY, "format"),
        ({}, "Another Company", "not this install's"),
        ({}, "", "DEMO_RESET_COMPANY is not set"),
        ({"profile.yaml": {"company": {"name": "Someone Else"}}}, COMPANY, "profile.yaml is for"),
        ({"people.yaml": {"people": [{"full_name": "Dana Reyes", "email": "dana@example.com"}]}}, COMPANY,
         "does not validate"),
        ({"people.yaml": {"people": [{"full_name": "Dana Reyes", "is_principal": True}]}}, COMPANY, "principal"),
        ({"memory.json": {"scheduled_actions": [{"run_at": "-1h", "channel_ref": "x@example.com"}]}}, COMPANY,
         "does not validate"),
        ({"departments.yaml": {"departments": [{"slug": "s", "title": "S", "head_person_name": "Nobody"}]}},
         COMPANY, "head is not in people.yaml"),
        ({"memory.json": None}, COMPANY, "memory.json is missing"),
    ],
)
async def test_refused_bundle_changes_nothing(
    env: SimpleNamespace, changes: dict[str, Any], settings_company: str, reason: str
) -> None:
    fictional = _person("Old Head")
    env.settings.demo_reset_company = settings_company
    with pytest.raises(dr.BundleError, match=reason):
        await dr.demo_reset(_bundle(env.tmp, **changes), env.settings)
    assert _count(env.db, "SELECT COUNT(*) FROM people WHERE id = ?", fictional) == 1
    assert not env.settings.company_profile_path.exists()
    assert env.audited == []


def test_a_linked_document_is_refused(env: SimpleNamespace) -> None:
    bundle = _bundle(env.tmp)
    secret = env.tmp / "secret.txt"
    secret.write_text("key", encoding="utf-8")
    try:
        (bundle / "docs" / "leak.md").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable here")
    with pytest.raises(dr.BundleError, match="not a plain file"):
        dr.read_bundle(bundle, COMPANY)


def test_cli_exits_non_zero_on_refusal(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from openexecutive.cli import cli

    monkeypatch.setattr("openexecutive.config.get_settings", lambda: env.settings)
    result = CliRunner().invoke(cli, ["demo-reset", "--bundle", str(_bundle(env.tmp, **{"bundle.json": None}))])
    assert result.exit_code == 1
    assert "Not reset: bundle.json is missing" in result.output

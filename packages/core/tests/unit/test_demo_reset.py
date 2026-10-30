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
    calls: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: _FakeStore.calls.append(name)


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

    _FakeStore.calls = []
    monkeypatch.setattr(store, "ChromaDBStore", _FakeStore)
    monkeypatch.setattr(loader, "ingest_file", _ingest)
    audited: list[str] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda event, *a, **k: audited.append(event))
    rebootstrapped: list[bool] = []
    monkeypatch.setattr(fixture_loader, "rebootstrap_scheduled_actions", lambda: rebootstrapped.append(True))
    from openexecutive.memory import honcho_client

    forgotten: list[tuple[list[int], list[str]]] = []

    async def _forget(person_ids: list[int], session_ids: list[str]) -> dict[str, Any]:
        forgotten.append((person_ids, session_ids))
        return {"status": "disabled", "conclusions": 0, "sessions": 0, "failed": 0}

    monkeypatch.setattr(honcho_client, "forget_people", _forget)

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
        db=db, settings=settings, tmp=tmp_path, audited=audited, rebootstrapped=rebootstrapped,
        forgotten=forgotten,
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
    from openexecutive.people import demo
    from openexecutive.people import store as people_store

    principal = _person("Pat Principal", principal=True, directory=True)
    teammate = _person("Taylor Team", directory=True)
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
        conn.execute(
            "INSERT INTO audit_log (ts, event_type, summary)"
            " VALUES ('now', 'watchlist_research_periodic_ran', 'last scan')"
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
    # The bundle's company, its people resolved only to the ones it added.
    morgan = _count(env.db, "SELECT id FROM people WHERE full_name = 'Morgan Hale'")
    assert morgan not in {principal, teammate, visitor, fictional}
    assert _count(env.db, "SELECT head_person_id FROM departments WHERE slug = 'warehouse'") == morgan
    assert _count(env.db, "SELECT COUNT(*) FROM people WHERE full_name = 'Dana Reyes'") == 1
    # The principal's artifacts and attachments stay indexed.
    assert "delete_attachment_docs" not in _FakeStore.calls
    assert _count(env.db, "SELECT COUNT(*) FROM decisions") == 1
    assert _count(env.db, "SELECT COUNT(*) FROM alerts") == 1
    assert (env.settings.company_profile_path.parent / "docs" / "company_overview.md").exists()
    assert summary["people_seeded"] == 2 and summary["demo_people"] == 1
    assert env.audited == ["demo_reset"] and env.rebootstrapped == [True]
    # The research scan's run history goes, so the scan runs on the new company.
    assert _count(env.db, "SELECT COUNT(*) FROM audit_log WHERE event_type = 'watchlist_research_periodic_ran'") == 0
    # Honcho forgets the demo person and the old roster: their own sessions and the demo person's chat.
    assert env.forgotten == [
        (sorted([visitor, fictional]), sorted(["visitor-chat", f"person-{visitor}", f"person-{fictional}"]))
    ]


async def test_reset_twice_leaves_one_copy(env: SimpleNamespace) -> None:
    _person("Pat Principal", principal=True)
    await dr.demo_reset(_bundle(env.tmp), env.settings)
    (env.tmp / "bundle").rename(env.tmp / "first")
    await dr.demo_reset(_bundle(env.tmp), env.settings)
    assert _count(env.db, "SELECT COUNT(*) FROM people") == 3  # the principal and the bundle's two
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
        ({"departments.yaml": {"departments": [{"slug": "sales", "title": "Sales", "authority_level": "auto_execute"}]}},
         COMPANY, "auto_execute"),
        ({"people.yaml": {"people": [{"full_name": "Pat  principal"}]}}, COMPANY, "keeps: Pat  principal"),
    ],
)
async def test_refused_bundle_changes_nothing(
    env: SimpleNamespace, changes: dict[str, Any], settings_company: str, reason: str
) -> None:
    _person("Pat Principal", principal=True)
    fictional = _person("Old Head")
    env.settings.demo_reset_company = settings_company
    with pytest.raises(dr.BundleError, match=reason):
        await dr.demo_reset(_bundle(env.tmp, **changes), env.settings)
    assert _count(env.db, "SELECT COUNT(*) FROM people WHERE id = ?", fictional) == 1
    assert not env.settings.company_profile_path.exists()
    assert env.audited == []


@pytest.mark.parametrize("target", ["docs/leak.md", "memory.json", "docs"])
def test_a_link_is_refused_when_staging(env: SimpleNamespace, target: str) -> None:
    bundle = _bundle(env.tmp)
    secret = env.tmp / ("secret_dir" if target == "docs" else "secret.txt")
    if target == "docs":
        secret.mkdir()
        (secret / "x.md").write_text("key", encoding="utf-8")
    else:
        secret.write_text("key", encoding="utf-8")
    link = bundle / target
    if link.is_dir():
        for child in link.iterdir():
            child.unlink()
        link.rmdir()
    elif link.exists():
        link.unlink()
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable here")
    with pytest.raises(dr.BundleError):
        dr.stage_bundle(bundle, env.tmp / "staged")


def test_yaml_aliases_are_refused(env: SimpleNamespace) -> None:
    bundle = _bundle(env.tmp)
    lines = ["a: &a [x, x]", "b: &b [*a, *a]", "people: [{full_name: Dana Reyes}]"]
    (bundle / "people.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(dr.BundleError, match="aliases"):
        dr.read_bundle(bundle, COMPANY)


async def test_a_principal_marked_demo_keeps_their_chats(env: SimpleNamespace) -> None:
    principal = _person("Pat Principal", principal=True, directory=True, demo=True)
    _chat(env.db, "principal-chat", principal)
    await dr.demo_reset(_bundle(env.tmp), env.settings)
    assert _count(env.db, "SELECT COUNT(*) FROM chat_messages WHERE session_id = 'principal-chat'") == 1


async def test_a_demo_persons_business_profile_survives_the_reset(env: SimpleNamespace) -> None:
    from openexecutive.people import profile

    _person("Pat Principal", principal=True, directory=True)
    visitor = _person("Vic Visitor", directory=True, demo=True)
    kept = profile.PersonProfile(country="US", business_type="wholesale", industry="electrical", jurisdiction="US-TX")
    assert profile.set_profile(visitor, kept)
    await dr.demo_reset(_bundle(env.tmp), env.settings)
    # It comes from the directory, like the person, so it stays with them.
    assert profile.get_profile(visitor) == kept


async def test_refused_while_the_api_holds_its_lock(env: SimpleNamespace) -> None:
    pytest.importorskip("fcntl")
    fictional = _person("Old Head")
    with dr.api_lock(env.settings), pytest.raises(dr.BundleError, match="API"):
        await dr.demo_reset(_bundle(env.tmp), env.settings)
    assert _count(env.db, "SELECT COUNT(*) FROM people WHERE id = ?", fictional) == 1


async def test_a_failure_part_way_is_audited(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.knowledge import loader

    async def _broken(**kwargs: Any) -> int:
        raise RuntimeError("embedding failed")

    monkeypatch.setattr(loader, "ingest_file", _broken)
    with pytest.raises(RuntimeError):
        await dr.demo_reset(_bundle(env.tmp), env.settings)
    assert env.audited == ["demo_reset_failed"]


def test_cli_exits_non_zero_on_refusal(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from openexecutive.cli import cli

    monkeypatch.setattr("openexecutive.config.get_settings", lambda: env.settings)
    result = CliRunner().invoke(cli, ["demo-reset", "--bundle", str(_bundle(env.tmp, **{"bundle.json": None}))])
    assert result.exit_code == 1
    assert "Not reset: bundle.json is missing" in result.output


def test_a_named_pipe_is_refused_without_blocking(env: SimpleNamespace) -> None:
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("no named pipes here")
    bundle = _bundle(env.tmp)
    (bundle / "people.yaml").unlink()
    os.mkfifo(bundle / "people.yaml")
    with pytest.raises(dr.BundleError, match="not a plain file"):
        dr.stage_bundle(bundle, env.tmp / "staged")


async def test_people_added_by_hand_are_kept(env: SimpleNamespace) -> None:
    from openexecutive.people import store as people_store

    _person("Pat Principal", principal=True)
    with_email = people_store.upsert_person(full_name="Hana Hand", role="Buyer", email="hana@example.com")
    with_alias = _person("Alia Alias")
    people_store.add_person_email(with_alias, "alia@example.com")
    with_slack = people_store.upsert_person(full_name="Sam Slack", role="Ops", slack_user_id="U123")
    fictional = _person("Old Head")

    await dr.demo_reset(_bundle(env.tmp), env.settings)

    left = {r[0] for r in sqlite3.connect(str(env.db)).execute("SELECT id FROM people")}
    assert {with_email, with_alias, with_slack} <= left
    assert fictional not in left
    assert _count(env.db, "SELECT COUNT(*) FROM person_emails WHERE person_id = ?", with_alias) == 1


async def test_a_bundle_naming_a_person_added_by_hand_is_refused(env: SimpleNamespace) -> None:
    from openexecutive.people import store as people_store

    people_store.upsert_person(full_name="Dana Reyes", role="Real buyer", email="dana@example.com")
    with pytest.raises(dr.BundleError, match="keeps: Dana Reyes"):
        await dr.demo_reset(_bundle(env.tmp), env.settings)


async def test_roster_requests_point_at_no_replaced_person(env: SimpleNamespace) -> None:
    fictional = _person("Old Head")
    with sqlite3.connect(str(env.db)) as conn:
        for ref, status, suggested, resolved in (
            ("a@example.com", "pending", fictional, None),
            ("b@example.com", "resolved", None, fictional),
        ):
            request = conn.execute(
                "INSERT INTO roster_requests (channel, channel_ref, status, suggested_person_id,"
                " resolved_person_id, first_seen_at, last_seen_at, expires_at)"
                " VALUES ('email', ?, ?, ?, ?, 'now', 'now', 'later')",
                (ref, status, suggested, resolved),
            ).lastrowid
            conn.execute(
                "INSERT INTO roster_request_messages (request_id, external_id, received_at)"
                " VALUES (?, ?, 'now')",
                (request, f"m-{ref}"),
            )

    await dr.demo_reset(_bundle(env.tmp), env.settings)

    # The pending one stays, without the suggestion; the resolved one goes, with its messages.
    assert _count(env.db, "SELECT COUNT(*) FROM roster_requests WHERE channel_ref = 'a@example.com'"
                  " AND suggested_person_id IS NULL") == 1
    assert _count(env.db, "SELECT COUNT(*) FROM roster_requests WHERE channel_ref = 'b@example.com'") == 0
    assert _count(env.db, "SELECT COUNT(*) FROM roster_request_messages") == 1


async def test_the_api_lock_is_released_by_closing_its_handle(env: SimpleNamespace) -> None:
    pytest.importorskip("fcntl")
    fictional = _person("Old Head")
    handle = dr.take_api_lock(env.settings)
    assert handle is not None
    with pytest.raises(dr.BundleError, match="API"):
        await dr.demo_reset(_bundle(env.tmp), env.settings)
    handle.close()
    (env.tmp / "bundle").rename(env.tmp / "refused")
    await dr.demo_reset(_bundle(env.tmp), env.settings)
    assert _count(env.db, "SELECT COUNT(*) FROM people WHERE id = ?", fictional) == 0


class _AsyncList(list):  # type: ignore[type-arg]
    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for item in self:
                yield item

        return _gen()


def _fake_honcho(deleted: list[str]) -> SimpleNamespace:
    def _peer(peer_id: str, conclusion_ids: list[str]) -> SimpleNamespace:
        async def _list() -> _AsyncList:
            return _AsyncList(SimpleNamespace(id=c) for c in conclusion_ids)

        async def _delete(conclusion_id: str) -> None:
            deleted.append(f"conclusion {conclusion_id}")

        return SimpleNamespace(id=peer_id, conclusions=SimpleNamespace(aio=SimpleNamespace(list=_list, delete=_delete)))

    def _session(session_id: str) -> SimpleNamespace:
        async def _delete() -> None:
            deleted.append(f"session {session_id}")

        return SimpleNamespace(id=session_id, aio=SimpleNamespace(delete=_delete))

    async def _peers() -> _AsyncList:
        return _AsyncList([_peer("7", ["c1", "c2"]), _peer("8", ["kept"])])

    async def _sessions() -> _AsyncList:
        return _AsyncList([_session("visitor-chat"), _session("person-7"), _session("principal-chat")])

    return SimpleNamespace(aio=SimpleNamespace(peers=_peers, sessions=_sessions))


async def test_forget_people_deletes_their_conclusions_and_sessions_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import honcho_client

    deleted: list[str] = []
    fake = _fake_honcho(deleted)

    async def _client() -> Any:
        return fake

    monkeypatch.setattr(honcho_client, "get_settings", lambda: SimpleNamespace(honcho_enabled=True))
    monkeypatch.setattr(honcho_client, "_get_client", _client)
    monkeypatch.setattr(honcho_client, "_emit_peer_memory", lambda **kwargs: None)

    summary = await honcho_client.forget_people([7], ["visitor-chat", "person-7"])

    assert sorted(deleted) == ["conclusion c1", "conclusion c2", "session person-7", "session visitor-chat"]
    assert summary == {"status": "ok", "conclusions": 2, "sessions": 2, "failed": 0}


async def test_forget_people_without_honcho_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import honcho_client

    monkeypatch.setattr(honcho_client, "get_settings", lambda: SimpleNamespace(honcho_enabled=False))
    summary = await honcho_client.forget_people([7], [])
    assert summary["status"] == "disabled"


def test_a_demo_chat_named_like_someone_elses_honcho_session_is_not_forgotten(env: SimpleNamespace) -> None:
    principal = _person("Pat Principal", principal=True, directory=True)
    visitor = _person("Vic Visitor", directory=True, demo=True)
    _chat(env.db, "telegram:8519677317", principal)
    # Both are Honcho's "telegram_8519677317" and "person-<principal>" once sanitized.
    _chat(env.db, "telegram.8519677317", visitor)
    _chat(env.db, f"person-{principal}", visitor)
    _chat(env.db, "visitor-chat", visitor)
    assert dr.honcho_sessions([visitor], env.db) == sorted(["visitor-chat", f"person-{visitor}"])

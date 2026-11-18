"""/fixtures is served only where OE_FIXTURES_ENABLED is on (never on a public
deployment), and every route is the principal's alone (api.routes.fixtures),
refused before any work: a reset wipes the whole install, the principal
included."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .principal_gate_support import CASES, app_client, call, make_roster

ROUTES: list[tuple[str, str, Any]] = [
    ("GET", "/fixtures", None),
    ("GET", "/fixtures/status", None),
    ("POST", "/fixtures/snapshot", None),
    ("POST", "/fixtures/reset", None),
    ("POST", "/fixtures/unload", None),
    ("POST", "/fixtures/demo/load", None),
    ("POST", "/fixtures/generate", {"description": "a bakery"}),
    ("POST", "/fixtures", {"scenario_description": "", "bundle": {}}),
    ("DELETE", "/fixtures/demo", None),
]


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The roster, the routes on, and every fixture operation stubbed to
    record that it ran."""
    make_roster(tmp_path, monkeypatch)
    monkeypatch.setenv("OE_FIXTURES_ENABLED", "1")
    ran: list[str] = []
    from openexecutive.cli import fixture_loader

    def _stub(name: str, result: Any) -> Any:
        async def _async(*_a: Any, **_kw: Any) -> Any:
            ran.append(name)
            return result

        return _async

    def _sync(name: str, result: Any) -> Any:
        def _f(*_a: Any, **_kw: Any) -> Any:
            ran.append(name)
            return result

        return _f

    monkeypatch.setattr(fixture_loader, "reset_all_state", _stub("reset", {"ok": True}))
    monkeypatch.setattr(fixture_loader, "snapshot_user_state_async", _stub("snapshot", {"ok": True}))
    monkeypatch.setattr(fixture_loader, "unload_fixture", _stub("unload", {"ok": True}))
    monkeypatch.setattr(fixture_loader, "load_fixture_any", _stub("load", {"ok": True}))
    monkeypatch.setattr(fixture_loader, "list_all_fixtures", _sync("list", []))
    monkeypatch.setattr(fixture_loader, "get_fixture_status", _sync("status", {}))
    return ran


def test_every_fixtures_route_is_on_the_list() -> None:
    from openexecutive.api.routes import fixtures

    routed = {(m, r.path) for r in fixtures.router.routes for m in getattr(r, "methods", set())}
    listed = {(m, p.replace("/demo", "/{name}")) for m, p, _ in ROUTES}
    assert routed == listed


@pytest.mark.parametrize(("signed", "who"), CASES)
def test_anyone_but_the_principal_is_refused(
    work: list[str], monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    from openexecutive.api.demo_gate import DEMO_REFUSED
    from openexecutive.api.routes.fixtures import PRINCIPAL_ONLY_DETAIL

    client = app_client(monkeypatch, signed=signed)
    for method, path, body in ROUTES:
        r = call(client, signed, who, method, path, body)
        assert r.status_code == 403, (method, path, r.text)
        # A demo person's write stops at the demo write gate first.
        assert r.json()["detail"] in (PRINCIPAL_ONLY_DETAIL, DEMO_REFUSED)
    assert work == []


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("who", ["principal", "operator"])
def test_the_principal_still_resets_and_loads(
    work: list[str], monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    client = app_client(monkeypatch, signed=signed)
    for method, path in (("GET", "/fixtures"), ("GET", "/fixtures/status"),
                         ("POST", "/fixtures/reset"), ("POST", "/fixtures/demo/load"),
                         ("POST", "/fixtures/unload"), ("POST", "/fixtures/snapshot")):
        r = call(client, signed, who, method, path)
        assert r.status_code == 200, (method, path, r.text)
    assert work == ["list", "status", "reset", "load", "unload", "snapshot"]


@pytest.mark.parametrize("value", ["", "0", "off", "disabled", "none"])
def test_the_routes_are_not_served_unless_turned_on(
    work: list[str], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OE_FIXTURES_ENABLED", value)
    client = app_client(monkeypatch, signed=False)
    for method, path, body in ROUTES:
        assert client.request(method, path, json=body).status_code in (404, 405), (method, path)
    assert work == []


def test_never_on_a_public_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.utils.deployment import fixtures_enabled

    monkeypatch.setenv("OE_FIXTURES_ENABLED", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    assert fixtures_enabled()
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    assert not fixtures_enabled()
    monkeypatch.delenv("OE_FIXTURES_ENABLED")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT")
    assert not fixtures_enabled()

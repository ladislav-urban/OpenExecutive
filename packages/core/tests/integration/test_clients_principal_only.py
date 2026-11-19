"""Managing client companies (create, draft, save, switch, edit, delete) is the
principal's alone: it swaps or changes the live company context every turn
reads (api.routes.clients, api.principal_gate)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.api.routes import clients as clients_route

from .principal_gate_support import CASES, app_client, call, make_roster

ROUTES: list[tuple[str, str, Any]] = [
    ("POST", "/clients", {"display_name": "Acme", "source": "blank"}),
    ("POST", "/clients/generate", None),
    ("POST", "/clients/save", None),
    ("POST", "/clients/acme/activate", None),
    ("PATCH", "/clients/acme", {"status": "paused"}),
    ("DELETE", "/clients/acme", None),
]


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    make_roster(tmp_path, monkeypatch)
    from openexecutive.clients import slots

    seen: list[str] = []
    for name in ("create_client_slot", "save_active_client", "activate_client_slot",
                 "update_client_meta", "delete_client_slot"):
        async def _rec(*_a: Any, _n: str = name, **_kw: Any) -> dict:
            seen.append(_n)
            return {"ok": True}

        monkeypatch.setattr(slots, name, _rec)
    return seen


def test_every_write_route_is_gated() -> None:
    gated = {
        (m, r.path)
        for r in clients_route.router.routes
        for m in getattr(r, "methods", set())
        if any(getattr(d.call, "__qualname__", "").startswith("principal_only")
               for d in r.dependant.dependencies)
    }
    writes = {(m, r.path) for r in clients_route.router.routes
              for m in getattr(r, "methods", set()) if m != "GET"}
    assert gated == writes
    assert len(writes) == len(ROUTES)


@pytest.mark.parametrize(("signed", "who"), CASES)
def test_anyone_but_the_principal_is_refused(
    work: list[str], monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    from openexecutive.api.demo_gate import DEMO_REFUSED

    client = app_client(monkeypatch, signed=signed)
    for method, path, body in ROUTES:
        r = call(client, signed, who, method, path, body)
        assert r.status_code == 403, (method, path, r.text)
        assert r.json()["detail"] in (clients_route.PRINCIPAL_ONLY_DETAIL, DEMO_REFUSED)
    assert work == []


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("who", ["principal", "operator"])
def test_the_principal_still_manages_clients(
    work: list[str], monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    client = app_client(monkeypatch, signed=signed)
    for method, path, body in ROUTES:
        if path == "/clients/generate":
            continue  # a model call; the gate is the same dependency
        r = call(client, signed, who, method, path, body)
        assert r.status_code == 200, (method, path, r.text)
    assert work == ["create_client_slot", "save_active_client", "activate_client_slot",
                    "update_client_meta", "delete_client_slot"]

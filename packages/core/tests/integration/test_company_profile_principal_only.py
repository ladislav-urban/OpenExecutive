"""Editing the company profile is the principal's alone, as the chat tool
that edits it is: the profile is in every prompt (api.routes.company_profile,
api.principal_gate)."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.api.routes import company_profile as profile_route

from .principal_gate_support import CASES, app_client, call, make_roster


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    make_roster(tmp_path, monkeypatch)
    path = tmp_path / "company" / "profile.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("name: Northwind Tools\nindustry: Industrial supply\n")
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    return path


@pytest.mark.parametrize(("signed", "who"), CASES)
def test_anyone_but_the_principal_is_refused(
    profile: Path, monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    from openexecutive.api.demo_gate import DEMO_REFUSED

    before = profile.read_bytes()
    client = app_client(monkeypatch, signed=signed)
    r = call(client, signed, who, "PATCH", "/company-profile", {"name": "Taken Over Ltd"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] in (profile_route.PRINCIPAL_ONLY_DETAIL, DEMO_REFUSED)
    assert profile.read_bytes() == before


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("who", ["principal", "operator"])
def test_the_principal_still_edits_it(
    profile: Path, monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    client = app_client(monkeypatch, signed=signed)
    r = call(client, signed, who, "PATCH", "/company-profile", {"name": "Northwind Supply"})
    assert r.status_code == 200, r.text
    assert "Northwind Supply" in profile.read_text()
    # Reading it stays open to anyone signed in.
    assert call(client, signed, "teammate", "GET", "/company-profile").status_code == 200

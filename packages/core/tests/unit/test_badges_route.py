"""GET /badges: the navigation's counts for the caller, each the same number
the page it stands for shows, and a count that fails left out."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import badges as badges_route
from openexecutive.api.routes import review as review_route
from openexecutive.api.routes import today as today_route
from openexecutive.briefing import home_rules
from openexecutive.briefing import narrative as briefing_narrative
from openexecutive.monitoring import store as monitoring_store

from .test_today_home_rules_route import _as, board  # noqa: F401  (fixture)


def _make_client() -> TestClient:
    app = FastAPI()
    for module in (today_route, review_route, badges_route):
        app.include_router(module.router)
    return TestClient(app)


class _Reviews:
    def count_by_status(self) -> dict[str, int]:
        return {"pending": 3, "approved": 9, "rejected": 1, "needs_revision": 2, "total": 15}


@pytest.fixture(autouse=True)
def _stores(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_narrative(**_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _no_narrative)
    monkeypatch.setattr(review_route, "_store", _Reviews)
    monkeypatch.setattr(
        monitoring_store, "list_pending_suggestions", lambda db_path=None: [SimpleNamespace(), SimpleNamespace()],
    )


@pytest.mark.parametrize("email", ["pat@example.com", "dana@example.com", "stranger@example.com"])
def test_home_is_todays_needs_you_count_for_the_caller(board: dict[str, Any], email: str) -> None:  # noqa: F811
    home_rules.register_waiting_cards("replies", lambda person: 2 if person.full_name == "Pat Principal" else 0)
    client = _make_client()
    today = client.get("/today", headers=_as(email)).json()
    counts = client.get("/badges", headers=_as(email)).json()["counts"]
    assert counts["home"] == today["needs_you_count"]


def test_the_counts(board: dict[str, Any]) -> None:  # noqa: F811
    client = _make_client()
    stats = client.get("/review/stats").json()
    counts = client.get("/badges", headers=_as("pat@example.com")).json()["counts"]
    assert counts == {"home": 1, "knowledge": stats["pending"] + stats["needs_revision"], "watchlist": 2}


def test_a_count_that_fails_is_left_out(board: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    def _broken(db_path: object = None) -> list[Any]:
        raise RuntimeError("no monitoring store")

    monkeypatch.setattr(monitoring_store, "list_pending_suggestions", _broken)
    counts = _make_client().get("/badges", headers=_as("pat@example.com")).json()["counts"]
    assert counts == {"home": 1, "knowledge": 5}

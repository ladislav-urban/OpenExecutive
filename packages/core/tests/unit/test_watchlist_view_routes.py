"""What the Watch list screens read from the API (oe_connector's single-UI
step 7): each watch in words, its group, the decline menu and the quieter
version, a signal's outcome, the form, a preview, and a slug made for an
empty one."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from openexecutive.monitoring import store as ms
from openexecutive.monitoring.models import Signal

from .test_watchlist_routes import _suggest, client, db  # noqa: F401  (fixtures and a helper)


def _by_slug(client: TestClient) -> dict[str, dict[str, Any]]:  # noqa: F811
    return {r["slug"]: r for r in client.get("/watchlist").json()}


def test_each_watch_comes_with_its_words_group_and_menus(client: TestClient, db: Path) -> None:  # noqa: F811
    ms.insert_watchlist_item(
        slug="stock-aapl", signal_type="stock", target="AAPL", trigger={"abs_change_pct_gte": 5},
        severity_floor="low", db_path=db,
    )
    ms.insert_watchlist_item(slug="rss-blog", signal_type="rss", target="https://x.com/feed", origin="research", db_path=db)
    _suggest(db)
    rows = _by_slug(client)
    stock = rows["stock-aapl"]
    assert stock["trigger_words"] == "It moves 5% or more in a day, up or down"
    assert stock["cadence_words"] == "About every 15 minutes"
    assert stock["loudness_words"] == "Everything, even small changes"
    assert stock["group"] == {"key": "stock", "label": "Stock", "order": 0}
    assert stock["raise_the_bar"]["patch"] == {"trigger": {"abs_change_pct_gte": 10}}
    assert stock["is_suggestion"] is False and stock["decline"] is None and stock["added_by_executive"] is False
    suggestion = rows["rss-initech"]
    assert suggestion["is_suggestion"] is True and suggestion["decline"]["verb"] == "Decline"
    added = rows["rss-blog"]
    assert added["added_by_executive"] is True and added["decline"]["verb"] == "Stop watching"
    # The same fields on one watch, its changes and its approval.
    assert client.get("/watchlist/stock-aapl").json()["trigger_words"] == stock["trigger_words"]
    patched = client.patch("/watchlist/stock-aapl", json={"severity_floor": "high"}).json()
    assert patched["loudness_words"] == "Only big things"
    approved = client.post("/watchlist/rss-initech/approve").json()
    assert approved["is_suggestion"] is False and approved["decline"]["verb"] == "Stop watching"


def test_signals_say_their_outcome_in_words(client: TestClient, db: Path) -> None:  # noqa: F811
    watch_id = ms.insert_watchlist_item(slug="stock-aapl", signal_type="stock", target="AAPL", db_path=db)
    ms.insert_signal(
        Signal(
            watchlist_id=watch_id, source_kind="stock", source_external_id="1", captured_at="2026-10-22T08:00:00+00:00",
            normalized_summary="Up 9%", provenance_url="https://x", dedup_key="k1",
        ),
        db_path=db,
    )
    rows = client.get("/watchlist/stock-aapl/signals").json()
    assert [r["outcome_label"] for r in rows] == ["pending"]


def test_the_form_and_a_preview(client: TestClient) -> None:  # noqa: F811
    form = client.get("/watchlist/form").json()
    assert [s["value"] for s in form["signal_types"]][0] == "stock"
    assert form["defaults"]["cadence"] == "15min"
    assert [r["value"] for r in form["decline_reasons"]] == ["not_relevant", "too_noisy", "wrong_source"]
    preview = client.post("/watchlist/preview", json={"signal_type": "rss", "target": "https://www.acme.com/feed",
                                                      "trigger": {"keywords": ["pricing"]}}).json()
    assert preview == {
        "slug": "rss-acme-com-feed",
        "trigger_words": "There's a new post that mentions “pricing”",
        "cadence_words": "About every 15 minutes",
        "loudness_words": "Everything, even small changes",
    }


def test_an_empty_slug_is_made_from_the_type_and_target(client: TestClient) -> None:  # noqa: F811
    res = client.post("/watchlist", json={"signal_type": "stock", "target": "MSFT"})
    assert res.status_code == 201, res.text
    assert res.json()["slug"] == "stock-msft"
    assert res.json()["trigger_words"].startswith("It moves")

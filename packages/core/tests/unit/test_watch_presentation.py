"""How the Watch list shows a watch (monitoring.presentation).

The trigger words, raise-the-bar and slug cases are the web app's former
tests of the same rules (scripts/watchWords.test.mjs, watchSlug.test.mjs);
the trigger, cadence and loudness lines themselves are describe's
(test_watchlist_describe.py).
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from openexecutive.monitoring import presentation as pr

VALID_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


def test_raise_the_bar_doubles_a_stock_threshold() -> None:
    r = pr.raise_the_bar("stock", {"abs_change_pct_gte": 5}, "low")
    assert r is not None and r.patch == {"trigger": {"abs_change_pct_gte": 10}}
    assert r.label == "Only moves of 10%+" and r.done == "You'll only hear about moves of 10% or more."
    assert pr.raise_the_bar("stock", {}, "low").patch == {"trigger": {"abs_change_pct_gte": 10}}  # type: ignore[union-attr]
    assert pr.raise_the_bar("stock", {"abs_change_pct_gte": 0.4}, "low").patch == {  # type: ignore[union-attr]
        "trigger": {"abs_change_pct_gte": 1}
    }
    # At 100% it can't double: the floor goes up instead.
    assert pr.raise_the_bar("stock", {"abs_change_pct_gte": 100}, "low").patch == {"severity_floor": "medium"}  # type: ignore[union-attr]


def test_raise_the_bar_raises_the_floor_one_step_otherwise() -> None:
    r = pr.raise_the_bar("rss", {"keywords": ["x"]}, "medium")
    assert r is not None and r.patch == {"severity_floor": "high"}
    assert r.label == "Only big things" and r.done == "You'll only hear about big things from this."
    assert pr.raise_the_bar("query", {}, "urgent") is None


def test_suggest_slug_names_a_watch_from_its_type_and_target() -> None:
    assert pr.suggest_slug("stock", "AAPL") == "stock-aapl"
    assert pr.suggest_slug("edgar", "320193") == "edgar-320193"
    assert pr.suggest_slug("rss", "https://www.example.com/blog/feed.xml") == "rss-example-com-blog-feed-xml"
    assert pr.suggest_slug("query", "Acme Corp layoffs OR funding") == "search-acme-corp-layoffs-or-funding"
    assert pr.suggest_slug("vendor_status", "https://status.vendor.io/history.atom") == "status-status-vendor-io-history-atom"
    assert pr.suggest_slug("stock", "   ") == ""


def test_suggest_slug_always_fits_the_slug_rule() -> None:
    long = pr.suggest_slug("page_watch", "https://example.com/" + "pricing-page/" * 20)
    assert len(long) <= 61 and VALID_SLUG.match(long) and not long.endswith("-")
    assert VALID_SLUG.match(pr.suggest_slug("Custom_Kind", "Ünïcode — target!"))


def test_suggestions_and_the_decline_menu() -> None:
    assert pr.is_suggestion("research_proposed", "dry_run")
    assert not pr.is_suggestion("research_proposed", "active")
    assert not pr.is_suggestion("manual", "dry_run")
    menu = pr.decline_menu("research_proposed", "dry_run")
    assert menu is not None and menu.verb == "Decline"
    assert [r.value for r in menu.reasons] == ["not_relevant", "too_noisy", "wrong_source"]
    assert menu.reasons[0].menu_label == "Decline: not relevant (never suggest this company/topic again)"
    stop = pr.decline_menu("research", "active")
    assert stop is not None and stop.verb == "Stop watching"
    assert stop.reasons[1].menu_label == "Stop watching: too noisy (keep it, but only high-severity signals)"
    # Stopping as too noisy keeps it, quieter; the other reasons delete it.
    assert [r.patch for r in stop.reasons] == [None, {"severity_floor": "high"}, None]
    assert all(r.patch is None for r in menu.reasons)
    assert pr.decline_menu("manual", "active") is None


def test_groups_follow_the_forms_order_and_unknown_types_go_last() -> None:
    assert (pr.group_of("stock").label, pr.group_of("stock").order) == ("Stock", 0)
    assert (pr.group_of("query").label, pr.group_of("query").order) == ("Web search (billed)", 5)
    other = pr.group_of("satellite")
    assert (other.key, other.label, other.order) == ("satellite", "satellite", len(pr.SIGNAL_TYPES))


def test_outcome_label() -> None:
    assert pr.outcome_label(None) == "pending"
    assert pr.outcome_label("alerted") == "alerted"
    assert pr.outcome_label("suppressed_floor") == "suppressed: floor"


def test_form_initial_defaults_unknown_values() -> None:
    draft = SimpleNamespace(
        slug="x", signal_type="satellite", target="t", display_label="", trigger={"a": 1},
        cadence="every_minute", severity_floor="low", severity_ceiling="urgent", mode="paused", notes="",
    )
    initial = pr.form_initial(draft)
    assert (initial.signal_type, initial.cadence, initial.mode) == ("stock", "15min", "active")
    assert initial.trigger == {"a": 1}


def test_the_form_and_a_preview() -> None:
    form = pr.watch_form()
    assert [s.value for s in form.signal_types][:2] == ["stock", "rss"]
    assert [s.billed for s in form.signal_types if s.billed] == [True]
    assert form.cadences[3].label == "Once a day"
    assert form.severities[1].label == "Only things that matter"
    assert form.defaults.cadence == "15min"
    p = pr.preview("stock", "AAPL", {"abs_change_pct_gte": 8}, "daily", "medium", "high")
    assert p.slug == "stock-aapl"
    assert p.trigger_words == "It moves 8% or more in a day, up or down"
    assert (p.cadence_words, p.loudness_words) == ("Once a day", "Only things that matter, and never as urgent")

"""The host limit's own rules (api.host_limit)."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException

from openexecutive.api import host_limit


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    host_limit.reset()
    yield
    host_limit.reset()


def _refused(key: str, **kwargs: object) -> str:
    with pytest.raises(HTTPException) as caught:
        host_limit.take(key, **kwargs)  # type: ignore[arg-type]
    assert caught.value.status_code == 429
    return str(caught.value.detail)


def test_one_in_flight_per_key() -> None:
    slot = host_limit.take("person:1", hourly=True, stale_after_s=100, now=0)
    assert _refused("person:1", hourly=True, stale_after_s=100, now=1) == host_limit.IN_FLIGHT_MESSAGE
    # Someone else isn't held.
    host_limit.take("person:2", hourly=True, stale_after_s=100, now=1)
    slot.release()
    host_limit.take("person:1", hourly=True, stale_after_s=100, now=2)


def test_the_hourly_part_rolls_and_a_refusal_isnt_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_limit, "HOURLY", 2)
    host_limit.take("k", hourly=True, stale_after_s=1, now=0).release()
    host_limit.take("k", hourly=True, stale_after_s=1, now=10).release()
    assert _refused("k", hourly=True, stale_after_s=1, now=20) == host_limit.HOURLY_MESSAGE
    assert _refused("k", hourly=True, stale_after_s=1, now=3599) == host_limit.HOURLY_MESSAGE
    # The first turn left the hour; the refusals never counted.
    host_limit.take("k", hourly=True, stale_after_s=1, now=3600).release()
    assert _refused("k", hourly=True, stale_after_s=1, now=3605) == host_limit.HOURLY_MESSAGE


def test_without_the_hourly_part_only_in_flight_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_limit, "HOURLY", 1)
    for n in range(5):
        host_limit.take("demo", hourly=False, stale_after_s=1, now=n).release()


def test_a_stale_slot_stops_holding_and_its_late_release_frees_nothing() -> None:
    old = host_limit.take("k", hourly=False, stale_after_s=100, now=0)
    new = host_limit.take("k", hourly=False, stale_after_s=100, now=100)
    old.release()
    assert _refused("k", hourly=False, stale_after_s=100, now=101) == host_limit.IN_FLIGHT_MESSAGE
    new.release()
    new.release()
    assert host_limit._in_flight == {}

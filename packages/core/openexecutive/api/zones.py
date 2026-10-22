"""The time zone a caller sees times in, for what an API groups or words by
day (Home's snooze times, the Chats list's date groups).

The zone the client says it shows times in comes first (``?tz=``, an IANA
name such as the browser's own), then the person's own (their notification
settings'), then the workspace's. A name that can't be loaded is ignored.
"""
from __future__ import annotations

import logging
from datetime import tzinfo

from fastapi import Query

logger = logging.getLogger(__name__)

# The ``tz`` query a route takes for this.
TZ_QUERY = Query(None, max_length=64, description="IANA time zone the client shows times in")


def caller_zone(caller: int | None, tz: str | None = None) -> tzinfo:
    from openexecutive.memory.workspace_settings import get_user_timezone, load_zone
    from openexecutive.people.notifications import get_notification_settings, zone_for

    if tz and (zone := load_zone(tz.strip())) is not None:
        return zone
    if caller is None:
        return get_user_timezone()
    try:
        return zone_for(get_notification_settings(caller))
    except Exception:
        logger.warning("zones: reading the caller's time zone failed", exc_info=True)
        return get_user_timezone()

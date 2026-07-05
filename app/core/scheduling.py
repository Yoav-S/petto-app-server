"""
scheduling.py — Timezone + repeat helpers for reminder notifications.

A reminder is stored as a local `date` ("YYYY-MM-DD") + `time` ("HH:MM").
To decide *when* to fire it, we combine those with the owner's IANA timezone
and convert to an absolute UTC instant.

Kept deliberately small — no reminder schema migration required.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta

from app.core.config import settings

logger = logging.getLogger("petto")

# Map the client repeat options to a relativedelta step.
_REPEAT_STEPS = {
    "every_day": relativedelta(days=1),
    "every_2_days": relativedelta(days=2),
    "every_week": relativedelta(weeks=1),
    "every_2_weeks": relativedelta(weeks=2),
    "every_month": relativedelta(months=1),
    "every_year": relativedelta(years=1),
}


def resolve_timezone(tz_name: str | None) -> ZoneInfo:
    """Return a ZoneInfo for tz_name, falling back to DEFAULT_TIMEZONE then UTC."""
    for candidate in (tz_name, settings.DEFAULT_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Unknown timezone %r, falling back", candidate)
    return ZoneInfo("UTC")


def compute_scheduled_at(date_str: str, time_str: str, tz_name: str | None) -> datetime | None:
    """
    Combine a local date + time in the given timezone into a UTC datetime.

    Returns None if the strings are malformed (so a bad row never blocks
    the whole dispatcher).
    """
    try:
        hour, minute = (int(p) for p in time_str.split(":"))
        year, month, day = (int(p) for p in date_str.split("-"))
    except (ValueError, AttributeError):
        return None
    tz = resolve_timezone(tz_name)
    local_dt = datetime(year, month, day, hour, minute, tzinfo=tz)
    return local_dt.astimezone(timezone.utc)


def next_occurrence(date_str: str, repeat: str) -> str | None:
    """
    Given a reminder's current date and its repeat rule, return the next
    date string ("YYYY-MM-DD"), or None for one-off reminders ("off").
    """
    step = _REPEAT_STEPS.get(repeat)
    if step is None:
        return None
    try:
        year, month, day = (int(p) for p in date_str.split("-"))
    except (ValueError, AttributeError):
        return None
    return (datetime(year, month, day) + step).strftime("%Y-%m-%d")

"""
scheduling.py — Timezone + repeat helpers for reminder notifications.

A reminder is stored as a local `date` ("YYYY-MM-DD") + `time` ("HH:MM").
To decide *when* to fire it, we combine those with the owner's IANA timezone
and convert to an absolute UTC instant.

Kept deliberately small — no reminder schema migration required.
"""
import logging
from datetime import datetime, timedelta, timezone
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

# Minutes before the reminder time. "off" sends nothing extra.
ALERT_OFFSET_MINUTES = {
    "5m": 5,
    "10m": 10,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "1d": 1440,
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


def compute_alert_at(
    date_str: str,
    time_str: str,
    tz_name: str | None,
    alert: str | None,
) -> datetime | None:
    """UTC instant for the pre-reminder alert, or None when alert is off."""
    minutes = ALERT_OFFSET_MINUTES.get(alert or "")
    if minutes is None:
        return None
    scheduled = compute_scheduled_at(date_str, time_str, tz_name)
    if scheduled is None:
        return None
    return scheduled - timedelta(minutes=minutes)


def occurrence_within_end(date_str: str, end_date: str | None) -> bool:
    """True when this occurrence is allowed (no end, or date <= end)."""
    if not end_date:
        return True
    return date_str <= end_date


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


def catch_up_recurring_date(
    date_str: str,
    time_str: str,
    repeat: str,
    tz_name: str | None,
    *,
    after: datetime,
    end_date: str | None = None,
) -> str | None:
    """
    Advance a recurring series past every occurrence that is already due/overdue.

    Returns the first date whose scheduled_at is still in the future (after
    `after`) and on or before end_date, or None when the series is over.
    """
    if repeat not in _REPEAT_STEPS:
        return None
    candidate = date_str
    for _ in range(800):
        if end_date and candidate > end_date:
            return None
        scheduled = compute_scheduled_at(candidate, time_str, tz_name)
        if scheduled is None:
            return None
        if scheduled > after:
            return candidate
        nxt = next_occurrence(candidate, repeat)
        if not nxt or (end_date and nxt > end_date):
            return None
        candidate = nxt
    return candidate

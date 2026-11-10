"""Server-enforced idle-hold limits.

Tarna's rule is one 30-minute break per driver per Pacific day, and a Square
offline mark that cannot run longer than 60 minutes. These constants are the
authority. ``config/thresholds.json`` mirrors them so a shared client file does
not advertise a longer hold; the server does not read the cap from that file.
"""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

BREAK_DURATION_S = 30 * 60
BREAKS_PER_DRIVER_DAY = 1
SQUARE_OFFLINE_MAX_S = 60 * 60
PACIFIC = ZoneInfo("America/Los_Angeles")


def aware(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def day_bounds(now: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
    """Pacific calendar day containing ``now``, as UTC-aware bounds [start, end)."""
    local = aware(now).astimezone(PACIFIC)
    start = datetime.datetime.combine(local.date(), datetime.time.min, tzinfo=PACIFIC)
    return start, start + datetime.timedelta(days=1)


def pacific_day(now: datetime.datetime) -> str:
    return aware(now).astimezone(PACIFIC).date().isoformat()


def on_day(stamp: datetime.datetime, now: datetime.datetime) -> bool:
    start, end = day_bounds(now)
    value = aware(stamp)
    return bool(value is not None and start <= value < end)


def parse_day(value: str) -> str:
    """Return a YYYY-MM-DD Pacific day, or raise ValueError."""
    try:
        parsed = datetime.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("day must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("day must be YYYY-MM-DD")
    return parsed.isoformat()


def day_bounds_for(day: str) -> tuple[datetime.datetime, datetime.datetime]:
    parsed = datetime.date.fromisoformat(parse_day(day))
    start = datetime.datetime.combine(parsed, datetime.time.min, tzinfo=PACIFIC)
    return start, start + datetime.timedelta(days=1)

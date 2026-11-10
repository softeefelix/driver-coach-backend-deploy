"""Hermes idle totals, stored per Pacific day and driver.

The route poll reads one primary-key row. Ingest upserts the batch Hermes
posts. Neither path scores idle minutes; Hermes already did that.
"""
from __future__ import annotations

import datetime
from typing import Optional

from .holds import aware, parse_day
from .no_idle import iso_z

MAX_DRIVERS = 200
MAX_DRIVER_ID = 128
MAX_MINUTES = 100_000
MAX_STREAK = 10_000
MAX_FLEET = 500


class ScorecardError(Exception):
    def __init__(self, detail: dict):
        super().__init__(detail.get("message") or "scorecard rejected")
        self.detail = detail


def _bad(error: str, message: str) -> None:
    raise ScorecardError({"error": error, "message": message})


def _whole(value, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad("bad_field", f"{name} must be an integer")
    if value < minimum or value > maximum:
        _bad("bad_field", f"{name} is out of range")
    return value


def _parse_as_of(value) -> datetime.datetime:
    if not isinstance(value, str) or not value or len(value) > 40:
        _bad("bad_as_of", "asOf must be a timezone-aware timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _bad("bad_as_of", "asOf must be a timezone-aware timestamp")
    if parsed.tzinfo is None:
        _bad("bad_as_of", "asOf must be a timezone-aware timestamp")
    return aware(parsed)


def parse_ingest(body) -> tuple[str, datetime.datetime, list[dict]]:
    """Validate a Hermes batch. Raises ScorecardError. Does not touch the database."""
    if not isinstance(body, dict):
        _bad("bad_body", "JSON object required")
    try:
        day = parse_day(body.get("day"))
    except (TypeError, ValueError):
        _bad("bad_day", "day must be YYYY-MM-DD")
    as_of = _parse_as_of(body.get("asOf"))
    drivers = body.get("drivers")
    if not isinstance(drivers, list):
        _bad("bad_drivers", "drivers must be a list")
    if len(drivers) > MAX_DRIVERS:
        _bad("too_many_drivers", f"drivers is limited to {MAX_DRIVERS}")
    rows = []
    seen = set()
    for item in drivers:
        if not isinstance(item, dict):
            _bad("bad_driver", "each driver must be an object")
        driver_id = item.get("driverId")
        if not isinstance(driver_id, str) or not driver_id.strip() or len(driver_id) > MAX_DRIVER_ID:
            _bad("bad_driver", "driverId must be a non-empty string")
        driver_id = driver_id.strip()
        if driver_id in seen:
            _bad("duplicate_driver", "driverId is repeated")
        seen.add(driver_id)
        truck = item.get("truckNo", None)
        if truck is not None:
            truck = _whole(truck, "truckNo", minimum=1, maximum=9999)
        rows.append(
            {
                "driver_id": driver_id,
                "truck_no": truck,
                "idle_minutes_today": _whole(
                    item.get("idleMinutesToday"), "idleMinutesToday", minimum=0, maximum=MAX_MINUTES
                ),
                "idle_minutes_week": _whole(
                    item.get("idleMinutesWeek"), "idleMinutesWeek", minimum=0, maximum=MAX_MINUTES
                ),
                "fleet_rank": _whole(item.get("fleetRank"), "fleetRank", minimum=1, maximum=MAX_FLEET),
                "fleet_size": _whole(item.get("fleetSize"), "fleetSize", minimum=1, maximum=MAX_FLEET),
                "clean_day_streak": _whole(
                    item.get("cleanDayStreak"), "cleanDayStreak", minimum=0, maximum=MAX_STREAK
                ),
            }
        )
        if rows[-1]["fleet_rank"] > rows[-1]["fleet_size"]:
            _bad("bad_field", "fleetRank cannot exceed fleetSize")
    return day, as_of, rows


def upsert(cur, day: str, as_of: datetime.datetime, rows: list[dict]) -> int:
    for row in rows:
        cur.execute(
            """
            INSERT INTO idle_scorecard (
                day, driver_id, truck_no, idle_minutes_today, idle_minutes_week,
                fleet_rank, fleet_size, clean_day_streak, as_of, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (day, driver_id) DO UPDATE SET
                truck_no = EXCLUDED.truck_no,
                idle_minutes_today = EXCLUDED.idle_minutes_today,
                idle_minutes_week = EXCLUDED.idle_minutes_week,
                fleet_rank = EXCLUDED.fleet_rank,
                fleet_size = EXCLUDED.fleet_size,
                clean_day_streak = EXCLUDED.clean_day_streak,
                as_of = EXCLUDED.as_of,
                updated_at = now()
            """,
            (
                day,
                row["driver_id"],
                row["truck_no"],
                row["idle_minutes_today"],
                row["idle_minutes_week"],
                row["fleet_rank"],
                row["fleet_size"],
                row["clean_day_streak"],
                as_of,
            ),
        )
    return len(rows)


def load_today(cur, driver_id: str, day: str) -> Optional[dict]:
    """One primary-key read. None when Hermes has not posted this driver today."""
    cur.execute(
        """
        SELECT idle_minutes_today, idle_minutes_week, fleet_rank, fleet_size,
               clean_day_streak, as_of
        FROM idle_scorecard
        WHERE day = %s AND driver_id = %s
        """,
        (day, driver_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "idleMinutesToday": int(row[0]),
        "idleMinutesWeek": int(row[1]),
        "fleetRank": int(row[2]),
        "fleetSize": int(row[3]),
        "cleanDayStreak": int(row[4]),
        "asOf": iso_z(aware(row[5])),
    }

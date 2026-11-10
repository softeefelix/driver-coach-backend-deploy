"""Office read of today's breaks and Square offline periods.

Hermes polls this without a driver session. The shape is the monitor's
exclusion list plus the day's history for the nightly report. Expiry is
applied by the caller before this read so an open row past its cap is already
closed with endReason ``expired``.
"""
from __future__ import annotations

import datetime
from typing import Optional

from .holds import BREAK_DURATION_S, SQUARE_OFFLINE_MAX_S, aware, day_bounds_for
from .no_idle import iso_z

# A later Square-offline period joins the previous chain when it starts within this gap.
SQUARE_OFFLINE_CHAIN_GAP = datetime.timedelta(minutes=5)


def empty_payload(day: str, now: datetime.datetime) -> dict:
    return {
        "day": day,
        "timezone": "America/Los_Angeles",
        "asOf": iso_z(aware(now)),
        "breakDurationS": BREAK_DURATION_S,
        "squareOfflineMaxS": SQUARE_OFFLINE_MAX_S,
        "trucks": [],
        "history": [],
        "squareOfflineTotals": [],
    }


def _release(cur, name: str, *, failed: bool) -> None:
    if failed:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    else:
        cur.execute(f"RELEASE SAVEPOINT {name}")


def _project(row: dict, now: datetime.datetime) -> dict:
    """Show a hold past its cap as expired without writing the row.

    The stored ``ended_at`` stays null until a cab poll, start, or end closes it.
    """
    started = aware(row.get("started_at"))
    allowed = int(row.get("allowed_s") or 0)
    if started is None or row.get("ended_at") is not None or allowed <= 0:
        return row
    ends = started + datetime.timedelta(seconds=allowed)
    if aware(now) < ends:
        return row
    shown = dict(row)
    shown["ended_at"] = ends
    shown["end_reason"] = "expired"
    shown["duration_s"] = allowed
    return shown


def _effective_end(row: dict, now: datetime.datetime) -> datetime.datetime:
    ended = aware(row.get("ended_at"))
    if ended is not None:
        return ended
    started = aware(row.get("started_at"))
    current = aware(now)
    allowed = int(row.get("allowed_s") or 0)
    if started is None or allowed <= 0:
        return current
    return min(current, started + datetime.timedelta(seconds=allowed))


def _effective_duration_s(row: dict, now: datetime.datetime) -> int:
    """Clock-capped seconds. Projected rows already store the cap in duration_s."""
    if row.get("duration_s") is not None and row.get("ended_at") is not None:
        return max(0, int(row["duration_s"]))
    started = aware(row.get("started_at"))
    if started is None:
        return 0
    return max(0, int((_effective_end(row, now) - started).total_seconds()))


def _minutes(seconds: int) -> int:
    return int(round(seconds / 60.0))


def square_offline_totals(rows: list[dict], now: datetime.datetime) -> list[dict]:
    """Per truck and driver: count, total minutes, and the longest chained run.

    The next period stays in the chain when it starts within five minutes of
    the previous derived end. Durations are the clock-capped seconds.
    """
    groups: dict[tuple, list] = {}
    for row in rows:
        truck = row.get("truck_no")
        if isinstance(truck, bool) or not isinstance(truck, int):
            continue
        groups.setdefault((truck, row.get("driver_id")), []).append(row)
    totals = []
    for (truck, driver), periods in groups.items():
        periods.sort(key=lambda item: (aware(item.get("started_at")) or aware(now), str(item.get("id") or "")))
        total_s = 0
        chain_s = 0
        longest_s = 0
        previous_end = None
        for period in periods:
            seconds = _effective_duration_s(period, now)
            total_s += seconds
            started = aware(period.get("started_at"))
            if (
                previous_end is not None
                and started is not None
                and started - previous_end <= SQUARE_OFFLINE_CHAIN_GAP
            ):
                chain_s += seconds
            else:
                chain_s = seconds
            longest_s = max(longest_s, chain_s)
            previous_end = _effective_end(period, now)
        totals.append(
            {
                "truckNo": truck,
                "driverId": driver,
                "count": len(periods),
                "minutes": _minutes(total_s),
                "longestChainMinutes": _minutes(longest_s),
            }
        )
    totals.sort(key=lambda item: (item["truckNo"], item["driverId"] is None, item["driverId"] or ""))
    return totals


def _history_row(kind: str, row: dict) -> dict:
    reason = row.get("end_reason")
    started = aware(row.get("started_at"))
    allowed = int(row.get("allowed_s") or (BREAK_DURATION_S if kind == "break" else SQUARE_OFFLINE_MAX_S))
    ends = started + datetime.timedelta(seconds=allowed) if started else None
    return {
        "kind": kind,
        "id": row.get("id"),
        "truckNo": row.get("truck_no"),
        "driverId": row.get("driver_id"),
        "startedAt": iso_z(started),
        "endedAt": iso_z(row.get("ended_at")),
        "endsAt": iso_z(ends),
        "endReason": reason,
        "expired": reason == "expired",
        "durationS": row.get("duration_s"),
        "allowedS": allowed,
    }


def _active_window(row: Optional[dict], now: datetime.datetime) -> dict:
    if not row or row.get("ended_at") is not None:
        return {
            "active": False,
            "id": None,
            "driverId": None,
            "truckNo": None,
            "startedAt": None,
            "endsAt": None,
            "remainingS": 0,
        }
    started = aware(row["started_at"])
    allowed = int(row["allowed_s"])
    ends = started + datetime.timedelta(seconds=allowed)
    current = aware(now)
    return {
        "active": True,
        "id": row["id"],
        "driverId": row.get("driver_id"),
        "truckNo": row.get("truck_no"),
        "startedAt": iso_z(started),
        "endsAt": iso_z(ends),
        "remainingS": max(0, int((ends - current).total_seconds())),
    }


def _fetch(cur, sql: str, params: tuple, save: str) -> list:
    cur.execute(f"SAVEPOINT {save}")
    failed = False
    rows: list = []
    try:
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    except Exception:
        failed = True
        rows = []
    _release(cur, save, failed=failed)
    return rows


def collect(cur, day: str, now: datetime.datetime) -> dict:
    start, end = day_bounds_for(day)
    breaks = _fetch(
        cur,
        """
        SELECT id::text, truck_no, driver_id, break_type,
               started_at, ended_at, duration_s, allowed_s, end_reason
        FROM shift_break
        WHERE (started_at >= %s AND started_at < %s)
           OR (ended_at IS NOT NULL AND ended_at >= %s AND ended_at < %s)
           OR ended_at IS NULL
        ORDER BY started_at ASC
        """,
        (start, end, start, end),
        "idle_break_rows",
    )
    squares = _fetch(
        cur,
        """
        SELECT id::text, truck_no, driver_id, started_at,
               ended_at, duration_s, allowed_s, end_reason
        FROM square_offline_period
        WHERE (started_at >= %s AND started_at < %s)
           OR (ended_at IS NOT NULL AND ended_at >= %s AND ended_at < %s)
           OR ended_at IS NULL
        ORDER BY started_at ASC
        """,
        (start, end, start, end),
        "idle_square_rows",
    )
    sessions = _fetch(
        cur,
        """
        SELECT truck_no, driver_id, started_at
        FROM driver_coach_session
        WHERE started_at >= %s AND started_at < %s
        ORDER BY started_at ASC
        """,
        (start, end),
        "idle_session_rows",
    )

    def break_dict(row) -> dict:
        return {
            "id": row[0],
            "truck_no": row[1],
            "driver_id": row[2],
            "started_at": row[4],
            "ended_at": row[5],
            "duration_s": row[6],
            "allowed_s": row[7],
            "end_reason": row[8],
        }

    def square_dict(row) -> dict:
        return {
            "id": row[0],
            "truck_no": row[1],
            "driver_id": row[2],
            "started_at": row[3],
            "ended_at": row[4],
            "duration_s": row[5],
            "allowed_s": row[6],
            "end_reason": row[7],
        }

    break_rows = [
        _project(break_dict(row), now) for row in breaks if _overlaps(row[4], row[5], start, end)
    ]
    square_rows = [
        _project(square_dict(row), now) for row in squares if _overlaps(row[3], row[4], start, end)
    ]
    history = [_history_row("break", row) for row in break_rows]
    history.extend(_history_row("square_offline", row) for row in square_rows)
    history.sort(key=lambda row: (row["startedAt"] or "", row["kind"], row["id"] or ""))

    by_truck: dict[int, dict] = {}

    def truck(number) -> dict:
        if number not in by_truck:
            by_truck[number] = {
                "truckNo": number,
                "driverId": None,
                "break": _active_window(None, now),
                "squareOffline": _active_window(None, now),
                "_started": None,
            }
        return by_truck[number]

    for row in sessions:
        truck_no, driver_id, started_at = row
        if truck_no is None:
            continue
        slot = truck(int(truck_no))
        started = aware(started_at)
        if slot["_started"] is None or (started and started >= slot["_started"]):
            slot["_started"] = started
            slot["driverId"] = driver_id

    for row in break_rows:
        if row.get("truck_no") is None or row.get("ended_at") is not None:
            continue
        slot = truck(int(row["truck_no"]))
        slot["break"] = _active_window(row, now)
    for row in square_rows:
        if row.get("truck_no") is None or row.get("ended_at") is not None:
            continue
        slot = truck(int(row["truck_no"]))
        slot["squareOffline"] = _active_window(row, now)

    trucks = []
    for slot in sorted(by_truck.values(), key=lambda item: item["truckNo"]):
        slot.pop("_started", None)
        trucks.append(slot)
    body = empty_payload(day, now)
    body["trucks"] = trucks
    body["history"] = history
    body["squareOfflineTotals"] = square_offline_totals(square_rows, now)
    return body


def _overlaps(started, ended, start, end) -> bool:
    """A period overlaps the Pacific day. An open row counts on its start day and later."""
    started_at = aware(started)
    if started_at is None or started_at >= end:
        return False
    ended_at = aware(ended)
    if ended_at is None:
        return True
    return ended_at > start

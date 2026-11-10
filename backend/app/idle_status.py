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


def empty_payload(day: str, now: datetime.datetime) -> dict:
    return {
        "day": day,
        "timezone": "America/Los_Angeles",
        "asOf": iso_z(aware(now)),
        "breakDurationS": BREAK_DURATION_S,
        "squareOfflineMaxS": SQUARE_OFFLINE_MAX_S,
        "trucks": [],
        "history": [],
    }


def _release(cur, name: str, *, failed: bool) -> None:
    if failed:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    else:
        cur.execute(f"RELEASE SAVEPOINT {name}")


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
        "sessionId": row.get("session_id"),
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
            "sessionId": None,
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
        "sessionId": row.get("session_id"),
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
        SELECT id::text, session_id::text, truck_no, driver_id, break_type,
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
        SELECT id::text, session_id::text, truck_no, driver_id, started_at,
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
        SELECT session_id::text, truck_no, driver_id, started_at
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
            "session_id": row[1],
            "truck_no": row[2],
            "driver_id": row[3],
            "started_at": row[5],
            "ended_at": row[6],
            "duration_s": row[7],
            "allowed_s": row[8],
            "end_reason": row[9],
        }

    def square_dict(row) -> dict:
        return {
            "id": row[0],
            "session_id": row[1],
            "truck_no": row[2],
            "driver_id": row[3],
            "started_at": row[4],
            "ended_at": row[5],
            "duration_s": row[6],
            "allowed_s": row[7],
            "end_reason": row[8],
        }

    break_rows = [break_dict(row) for row in breaks if _overlaps(row[5], row[6], start, end)]
    square_rows = [square_dict(row) for row in squares if _overlaps(row[4], row[5], start, end)]
    history = [_history_row("break", row) for row in break_rows]
    history.extend(_history_row("square_offline", row) for row in square_rows)
    history.sort(key=lambda row: (row["startedAt"] or "", row["kind"], row["id"] or ""))

    by_truck: dict[int, dict] = {}

    def truck(number) -> dict:
        if number not in by_truck:
            by_truck[number] = {
                "truckNo": number,
                "driverId": None,
                "sessionId": None,
                "break": _active_window(None, now),
                "squareOffline": _active_window(None, now),
                "_started": None,
            }
        return by_truck[number]

    for row in sessions:
        session_id, truck_no, driver_id, started_at = row
        if truck_no is None:
            continue
        slot = truck(int(truck_no))
        started = aware(started_at)
        if slot["_started"] is None or (started and started >= slot["_started"]):
            slot["_started"] = started
            slot["driverId"] = driver_id
            slot["sessionId"] = session_id

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

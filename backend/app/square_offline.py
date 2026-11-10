"""A driver marking their Square device offline while they are still selling.

Multiple periods per Pacific day are allowed. Each one is a row: who, which
truck and session, when it started, and whether the driver ended it or the
server expired it at ``SQUARE_OFFLINE_MAX_S``. An open period suppresses idle
nudges the same way a break does. Nothing is written to public.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Optional

from .holds import (
    SQUARE_OFFLINE_MAX_S,
    aware,
    day_bounds,
    on_day,
    pacific_day,
)
from .no_idle import iso_z


class SquareOfflineError(Exception):
    def __init__(self, status: int, detail: dict):
        super().__init__(detail.get("message") or "square offline rejected")
        self.status = status
        self.detail = detail


def _row_from_sql(row) -> dict:
    return {
        "id": str(row[0]),
        "session_id": str(row[1]),
        "truck_no": row[2],
        "driver_id": row[3],
        "started_at": aware(row[4]),
        "ended_at": aware(row[5]),
        "duration_s": row[6],
        "allowed_s": int(row[7]),
        "end_reason": row[8],
    }


def _lock_driver(cur, driver_id: Optional[str], session_id: str) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
        (f"square-offline:{driver_id or session_id}",),
    )


def _lock_session(cur, session_id: str) -> tuple:
    cur.execute(
        "SELECT truck_no, driver_id FROM driver_coach_session WHERE session_id = %s FOR UPDATE",
        (session_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown session_id: {session_id}")
    return row[0], row[1]


def expire_overdue(
    cur,
    now: datetime.datetime,
    *,
    driver_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Close open periods at their stored cap. The end is the cap, not 'now'."""
    now = aware(now)
    sql = """
        UPDATE square_offline_period
           SET ended_at = started_at + (allowed_s * INTERVAL '1 second'),
               duration_s = allowed_s,
               end_reason = 'expired'
         WHERE ended_at IS NULL
           AND started_at + (allowed_s * INTERVAL '1 second') <= %s
    """
    params: list = [now]
    if driver_id is not None or session_id is not None:
        sql += " AND (session_id = %s OR (%s IS NOT NULL AND driver_id = %s))"
        params.extend([session_id, driver_id, driver_id])
    cur.execute(sql, params)
    return cur.rowcount > 0


def load_rows(cur, *, session_id: str, driver_id: Optional[str], now: datetime.datetime) -> list[dict]:
    start, end = day_bounds(now)
    cur.execute(
        """
        SELECT id, session_id, truck_no, driver_id, started_at, ended_at,
               duration_s, allowed_s, end_reason
        FROM square_offline_period
        WHERE session_id = %s
           OR (
                %s IS NOT NULL AND driver_id = %s
                AND (
                    (started_at >= %s AND started_at < %s)
                    OR ended_at IS NULL
                )
           )
        ORDER BY started_at ASC
        FOR UPDATE
        """,
        (session_id, driver_id, driver_id, start, end),
    )
    return [_row_from_sql(row) for row in cur.fetchall()]


def _open_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("ended_at") is None]


def wire_state(rows: list[dict], *, active: Optional[dict], now: datetime.datetime) -> dict:
    now = aware(now)
    today = [row for row in rows if row.get("started_at") and on_day(row["started_at"], now)]
    body = {
        "active": False,
        "id": None,
        "startedAt": None,
        "endsAt": None,
        "remainingS": 0,
        "allowedS": SQUARE_OFFLINE_MAX_S,
        "todayCount": len(today),
        "day": pacific_day(now),
    }
    if not active or active.get("ended_at") is not None:
        return body
    started = aware(active["started_at"])
    allowed = int(active["allowed_s"])
    ends = started + datetime.timedelta(seconds=allowed)
    body.update(
        {
            "active": True,
            "id": active["id"],
            "startedAt": iso_z(started),
            "endsAt": iso_z(ends),
            "remainingS": max(0, int((ends - now).total_seconds())),
            "allowedS": allowed,
        }
    )
    return body


def inactive_state(now: Optional[datetime.datetime] = None) -> dict:
    current = now or datetime.datetime.now(datetime.timezone.utc)
    return wire_state([], active=None, now=current)


def _ended_payload(row: dict) -> dict:
    reason = row.get("end_reason")
    return {
        "id": row["id"],
        "reason": reason,
        "endReason": reason,
        "expired": reason == "expired",
        "startedAt": iso_z(row.get("started_at")),
        "endedAt": iso_z(row.get("ended_at")),
        "durationS": row.get("duration_s"),
    }


def _finish_driver(cur, row: dict, now: datetime.datetime) -> dict:
    cur.execute(
        """
        UPDATE square_offline_period
           SET ended_at = %s, duration_s = %s, end_reason = 'driver'
         WHERE id = %s AND ended_at IS NULL
        """,
        (aware(now), max(0, int(round((aware(now) - aware(row["started_at"])).total_seconds()))), row["id"]),
    )
    updated = dict(row)
    updated["ended_at"] = aware(now)
    updated["duration_s"] = max(0, int(round((aware(now) - aware(row["started_at"])).total_seconds())))
    updated["end_reason"] = "driver"
    return updated


def reconcile_on_poll(
    cur,
    *,
    session_id: str,
    driver_id: Optional[str],
    now: Optional[datetime.datetime] = None,
) -> tuple[dict, bool]:
    now = aware(now or datetime.datetime.now(datetime.timezone.utc))
    wrote = expire_overdue(cur, now, driver_id=driver_id, session_id=session_id)
    rows = load_rows(cur, session_id=session_id, driver_id=driver_id, now=now)
    active = _open_rows(rows)
    current = active[-1] if active else None
    return wire_state(rows, active=current, now=now), wrote


def start(cur, *, session_id: str, now: Optional[datetime.datetime] = None) -> dict:
    now = aware(now or datetime.datetime.now(datetime.timezone.utc))
    truck_no, driver_id = _lock_session(cur, session_id)
    _lock_driver(cur, driver_id, session_id)
    expire_overdue(cur, now, driver_id=driver_id, session_id=session_id)
    rows = load_rows(cur, session_id=session_id, driver_id=driver_id, now=now)
    active = _open_rows(rows)
    if active:
        state = wire_state(rows, active=active[-1], now=now)
        raise SquareOfflineError(
            409,
            {
                "error": "square_offline_active",
                "message": "End the current Square offline period before starting another.",
                "squareOffline": state,
            },
        )
    period_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO square_offline_period (
            id, session_id, truck_no, driver_id, started_at, allowed_s, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (period_id, session_id, int(truck_no), driver_id, now, SQUARE_OFFLINE_MAX_S, now),
    )
    created = {
        "id": period_id,
        "session_id": session_id,
        "truck_no": int(truck_no),
        "driver_id": driver_id,
        "started_at": now,
        "ended_at": None,
        "duration_s": None,
        "allowed_s": SQUARE_OFFLINE_MAX_S,
        "end_reason": None,
    }
    rows.append(created)
    return {"session_id": session_id, "squareOffline": wire_state(rows, active=created, now=now)}


def end(cur, *, session_id: str, now: Optional[datetime.datetime] = None) -> dict:
    now = aware(now or datetime.datetime.now(datetime.timezone.utc))
    _truck, driver_id = _lock_session(cur, session_id)
    _lock_driver(cur, driver_id, session_id)
    expire_overdue(cur, now, driver_id=driver_id, session_id=session_id)
    rows = load_rows(cur, session_id=session_id, driver_id=driver_id, now=now)
    active = _open_rows(rows)
    if not active:
        state = wire_state(rows, active=None, now=now)
        raise SquareOfflineError(
            409,
            {"error": "no_square_offline", "message": "Square is not marked offline.", "squareOffline": state},
        )
    finished = _finish_driver(cur, active[-1], now)
    for index, row in enumerate(rows):
        if row["id"] == finished["id"]:
            rows[index] = finished
            break
    return {
        "session_id": session_id,
        "squareOffline": wire_state(rows, active=None, now=now),
        "ended": _ended_payload(finished),
    }

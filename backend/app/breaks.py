"""One 30-minute break per driver per Pacific day.

``lunch`` and ``break`` are the same hold: the cab may send either type, and
either one uses the single daily break. The length is ``BREAK_DURATION_S``.
The server closes it at that cap even if the cab never calls end; the stored
end is the cap, not the time the poll happened to notice. A fresh Geotab fix
showing the truck moving still ends it early. A second start the same Pacific
day is refused and flagged. Nothing is written to public.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Optional

from .config import break_settings
from .holds import BREAK_DURATION_S, BREAKS_PER_DRIVER_DAY, day_bounds, on_day, pacific_day
from .no_idle import aware, iso_z, utcnow

BREAK_TYPES = ("lunch", "break")


class BreakError(Exception):
    def __init__(self, status: int, detail: dict, *, commit: bool = False):
        super().__init__(detail.get("message") or "break rejected")
        self.status = status
        self.detail = detail
        self.commit = commit


def load_config() -> dict:
    return break_settings()


def duration_s(cfg: dict, break_type: str) -> int:
    del cfg, break_type
    return BREAK_DURATION_S


def allowance(cfg: dict, break_type: str) -> int:
    del cfg, break_type
    return BREAKS_PER_DRIVER_DAY


def end_stats(started_at: datetime.datetime, ended_at: datetime.datetime, allowed_s: int, grace_s: int) -> dict:
    elapsed = (aware(ended_at) - aware(started_at)).total_seconds()
    duration = max(0, int(round(elapsed)))
    overrun = max(0, int(round(elapsed - allowed_s)))
    return {
        "duration_s": duration,
        "overrun_s": overrun,
        "flagged": overrun > int(grace_s),
    }


def auto_end_reason(active: Optional[dict], now: datetime.datetime, *, fresh: bool, moving: bool) -> Optional[str]:
    """Why an active break should end on this poll, or None to leave it running.

    The cap wins over movement. A poll that arrives after 30 minutes records
    ``expired`` at the cap even if the truck is also moving.
    """
    if not active:
        return None
    ends = aware(active["started_at"]) + datetime.timedelta(seconds=int(active["allowed_s"]))
    if aware(now) >= ends:
        return "expired"
    if fresh and moving:
        return "moving"
    return None


def wire_state(
    rows: list[dict],
    *,
    active: Optional[dict],
    latest_flag: Optional[dict],
    now: datetime.datetime,
    cfg: Optional[dict] = None,
) -> dict:
    del cfg
    now = aware(now)
    used = sum(1 for row in rows if row.get("started_at") and on_day(row["started_at"], now))
    # Both legacy counters move together. An older cab that tracks lunch and
    # short breaks separately still sees the one daily break as used up.
    taken = 1 if used else 0
    body = {
        "active": False,
        "type": None,
        "startedAt": None,
        "endsAt": None,
        "remainingS": 0,
        "allowedS": None,
        "used": used,
        "allowed": BREAKS_PER_DRIVER_DAY,
        "day": pacific_day(now),
        "lunchUsed": taken,
        "lunchAllowed": BREAKS_PER_DRIVER_DAY,
        "breakUsed": taken,
        "breakAllowed": BREAKS_PER_DRIVER_DAY,
        "latestFlag": _flag_wire(latest_flag),
    }
    if not active or active.get("ended_at") is not None:
        return body
    started = aware(active["started_at"])
    allowed = int(active["allowed_s"])
    ends = started + datetime.timedelta(seconds=allowed)
    remaining = max(0, int((ends - now).total_seconds()))
    body.update(
        {
            "active": True,
            "type": active["break_type"],
            "startedAt": iso_z(started),
            "endsAt": iso_z(ends),
            "remainingS": remaining,
            "allowedS": allowed,
        }
    )
    return body


def inactive_state(now: Optional[datetime.datetime] = None) -> dict:
    return wire_state([], active=None, latest_flag=None, now=now or utcnow())


def _flag_wire(flag: Optional[dict]) -> Optional[dict]:
    if not flag:
        return None
    return {
        "kind": flag.get("kind"),
        "type": flag.get("break_type"),
        "overrunS": flag.get("overrun_s"),
        "at": iso_z(flag.get("created_at")),
        "detail": flag.get("detail"),
    }


def _row_from_sql(row) -> dict:
    return {
        "id": str(row[0]),
        "break_type": row[1],
        "started_at": aware(row[2]),
        "ended_at": aware(row[3]),
        "allowed_s": int(row[4]),
        "duration_s": row[5],
        "overrun_s": int(row[6] or 0),
        "flagged": bool(row[7]),
        "end_reason": row[8],
        "truck_no": row[9],
        "driver_id": row[10],
    }


def _flag_from_sql(row) -> dict:
    return {
        "id": str(row[0]),
        "break_type": row[1],
        "break_id": str(row[2]) if row[2] else None,
        "kind": row[3],
        "overrun_s": row[4],
        "detail": row[5],
        "created_at": aware(row[6]),
    }


def _lock_session(cur, session_id: str) -> tuple:
    cur.execute(
        "SELECT truck_no, driver_id FROM driver_coach_session WHERE session_id = %s FOR UPDATE",
        (session_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown session_id: {session_id}")
    return row[0], row[1]


def _lock_driver(cur, driver_id: Optional[str], session_id: str) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
        (f"break:{driver_id or session_id}",),
    )


def expire_overdue(
    cur,
    now: datetime.datetime,
    *,
    driver_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Close open breaks at their stored cap. The recorded end is the cap."""
    now = aware(now)
    sql = """
        UPDATE shift_break
           SET ended_at = started_at + (allowed_s * INTERVAL '1 second'),
               duration_s = allowed_s,
               end_reason = 'expired',
               overrun_s = 0,
               flagged = false
         WHERE ended_at IS NULL
           AND started_at + (allowed_s * INTERVAL '1 second') <= %s
    """
    params: list = [now]
    if driver_id is not None or session_id is not None:
        sql += " AND (session_id = %s OR (%s IS NOT NULL AND driver_id = %s))"
        params.extend([session_id, driver_id, driver_id])
    cur.execute(sql, params)
    return cur.rowcount > 0


def load_breaks(cur, session_id: str, driver_id: Optional[str] = None, now: Optional[datetime.datetime] = None) -> list[dict]:
    """This session's breaks plus this driver's breaks on the Pacific day."""
    current = aware(now or utcnow())
    start, end = day_bounds(current)
    cur.execute(
        """
        SELECT id, break_type, started_at, ended_at, allowed_s, duration_s,
               overrun_s, flagged, end_reason, truck_no, driver_id
        FROM shift_break
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


def load_latest_flag(cur, session_id: str) -> Optional[dict]:
    cur.execute(
        """
        SELECT id, break_type, break_id, kind, overrun_s, detail, created_at
        FROM shift_break_flag
        WHERE session_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (session_id,),
    )
    row = cur.fetchone()
    return _flag_from_sql(row) if row else None


def _open_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("ended_at") is None]


def _insert_flag(
    cur,
    *,
    session_id: str,
    truck_no: int,
    driver_id: Optional[str],
    break_type: Optional[str],
    break_id: Optional[str],
    kind: str,
    overrun_s: Optional[int],
    detail: str,
    now: datetime.datetime,
) -> dict:
    flag_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO shift_break_flag (
            id, session_id, truck_no, driver_id, break_type, break_id, kind,
            overrun_s, detail, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            flag_id,
            session_id,
            truck_no,
            driver_id,
            break_type,
            break_id,
            kind,
            overrun_s,
            detail,
            aware(now),
        ),
    )
    return {
        "id": flag_id,
        "break_type": break_type,
        "break_id": break_id,
        "kind": kind,
        "overrun_s": overrun_s,
        "detail": detail,
        "created_at": aware(now),
    }


def _finish(
    cur,
    row: dict,
    *,
    session_id: str,
    now: datetime.datetime,
    reason: str,
    cfg: dict,
) -> tuple[dict, Optional[dict]]:
    if reason == "expired":
        ended_at = aware(row["started_at"]) + datetime.timedelta(seconds=int(row["allowed_s"]))
        stats = {"duration_s": int(row["allowed_s"]), "overrun_s": 0, "flagged": False}
    else:
        ended_at = aware(now)
        stats = end_stats(row["started_at"], ended_at, int(row["allowed_s"]), int(cfg["overrun_grace_s"]))
    cur.execute(
        """
        UPDATE shift_break
           SET ended_at = %s, duration_s = %s, end_reason = %s, overrun_s = %s, flagged = %s
         WHERE id = %s AND ended_at IS NULL
        """,
        (ended_at, stats["duration_s"], reason, stats["overrun_s"], stats["flagged"], row["id"]),
    )
    row = dict(row)
    row.update(
        {
            "ended_at": ended_at,
            "duration_s": stats["duration_s"],
            "end_reason": reason,
            "overrun_s": stats["overrun_s"],
            "flagged": stats["flagged"],
        }
    )
    flag = None
    if stats["flagged"]:
        detail = (
            f"{row['break_type']} ran {stats['overrun_s']}s past the {row['allowed_s']}s allotment"
        )
        flag = _insert_flag(
            cur,
            session_id=session_id,
            truck_no=int(row["truck_no"]),
            driver_id=row.get("driver_id"),
            break_type=row["break_type"],
            break_id=row["id"],
            kind="overrun",
            overrun_s=stats["overrun_s"],
            detail=detail,
            now=now,
        )
    return row, flag


def _ended_payload(row: dict) -> dict:
    reason = row.get("end_reason")
    return {
        "id": row["id"],
        "type": row["break_type"],
        "reason": reason,
        "endReason": reason,
        "expired": reason == "expired",
        "startedAt": iso_z(row.get("started_at")),
        "endedAt": iso_z(row.get("ended_at")),
        "durationS": row.get("duration_s"),
        "overrunS": row.get("overrun_s") or 0,
        "flagged": bool(row.get("flagged")),
    }


def reconcile_on_poll(
    cur,
    *,
    session_id: str,
    truck_no: int,
    driver_id: Optional[str],
    fresh: bool,
    moving: bool,
    now: Optional[datetime.datetime] = None,
    cfg: Optional[dict] = None,
) -> tuple[dict, bool]:
    """Auto-end a break that has expired or whose truck is moving. Returns wire state."""
    now = aware(now or utcnow())
    cfg = cfg or load_config()
    wrote = expire_overdue(cur, now, driver_id=driver_id, session_id=session_id)
    rows = load_breaks(cur, session_id, driver_id, now)
    latest = load_latest_flag(cur, session_id)
    opens = _open_rows(rows)
    # Keep a single active break. Older duplicates are closed without a flag.
    for extra in opens[:-1]:
        finished, flag = _finish(cur, extra, session_id=session_id, now=now, reason="superseded", cfg=cfg)
        _replace(rows, finished)
        if flag:
            latest = flag
        wrote = True
    active = _open_rows(rows)
    current = active[-1] if active else None
    reason = auto_end_reason(current, now, fresh=fresh, moving=moving)
    if current is not None and reason is not None:
        finished, flag = _finish(cur, current, session_id=session_id, now=now, reason=reason, cfg=cfg)
        _replace(rows, finished)
        if flag:
            latest = flag
        current = None
        wrote = True
    return wire_state(rows, active=current, latest_flag=latest, now=now, cfg=cfg), wrote


def _replace(rows: list[dict], updated: dict) -> None:
    for index, row in enumerate(rows):
        if row["id"] == updated["id"]:
            rows[index] = updated
            return


def start(cur, *, session_id: str, break_type: str, now: Optional[datetime.datetime] = None, cfg: Optional[dict] = None) -> dict:
    """Start a lunch or break. Raises KeyError or BreakError. May write an over-limit flag."""
    now = aware(now or utcnow())
    cfg = cfg or load_config()
    if break_type not in BREAK_TYPES:
        raise BreakError(
            400,
            {"error": "bad_type", "message": "type must be lunch or break"},
            commit=False,
        )
    truck_no, driver_id = _lock_session(cur, session_id)
    _lock_driver(cur, driver_id, session_id)
    expire_overdue(cur, now, driver_id=driver_id, session_id=session_id)
    rows = load_breaks(cur, session_id, driver_id, now)
    latest = load_latest_flag(cur, session_id)
    active = _open_rows(rows)
    if active:
        state = wire_state(rows, active=active[-1], latest_flag=latest, now=now, cfg=cfg)
        raise BreakError(
            409,
            {
                "error": "break_active",
                "message": "End the current break before starting another.",
                "break": state,
            },
            commit=False,
        )
    used = sum(1 for row in rows if row.get("started_at") and on_day(row["started_at"], now))
    allowed = allowance(cfg, break_type)
    if used >= allowed:
        detail = "This driver already used today's break."
        flag = _insert_flag(
            cur,
            session_id=session_id,
            truck_no=int(truck_no),
            driver_id=driver_id,
            break_type=break_type,
            break_id=None,
            kind="over_limit",
            overrun_s=None,
            detail=detail,
            now=now,
        )
        state = wire_state(rows, active=None, latest_flag=flag, now=now, cfg=cfg)
        raise BreakError(
            409,
            {"error": "over_limit", "message": detail, "type": break_type, "break": state},
            commit=True,
        )
    allowed_s = duration_s(cfg, break_type)
    break_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO shift_break (
            id, session_id, truck_no, driver_id, break_type, started_at, allowed_s, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (break_id, session_id, int(truck_no), driver_id, break_type, now, allowed_s, now),
    )
    created = {
        "id": break_id,
        "break_type": break_type,
        "started_at": now,
        "ended_at": None,
        "allowed_s": allowed_s,
        "duration_s": None,
        "overrun_s": 0,
        "flagged": False,
        "end_reason": None,
        "truck_no": int(truck_no),
        "driver_id": driver_id,
    }
    rows.append(created)
    state = wire_state(rows, active=created, latest_flag=latest, now=now, cfg=cfg)
    return {"session_id": session_id, "break": state}


def end(cur, *, session_id: str, now: Optional[datetime.datetime] = None, reason: str = "driver", cfg: Optional[dict] = None) -> dict:
    """End the active break. Raises KeyError or BreakError when none is running."""
    now = aware(now or utcnow())
    cfg = cfg or load_config()
    _truck, driver_id = _lock_session(cur, session_id)
    _lock_driver(cur, driver_id, session_id)
    expire_overdue(cur, now, driver_id=driver_id, session_id=session_id)
    rows = load_breaks(cur, session_id, driver_id, now)
    latest = load_latest_flag(cur, session_id)
    active = _open_rows(rows)
    if not active:
        state = wire_state(rows, active=None, latest_flag=latest, now=now, cfg=cfg)
        raise BreakError(
            409,
            {"error": "no_break", "message": "No break is running.", "break": state},
            commit=False,
        )
    finished, flag = _finish(cur, active[-1], session_id=session_id, now=now, reason=reason, cfg=cfg)
    _replace(rows, finished)
    if flag:
        latest = flag
    state = wire_state(rows, active=None, latest_flag=latest, now=now, cfg=cfg)
    return {"session_id": session_id, "break": state, "ended": _ended_payload(finished)}

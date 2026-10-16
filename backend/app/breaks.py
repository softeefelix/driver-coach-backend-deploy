"""Per-shift lunch and break for a signed-in Driver Coach session.

The session is the shift: one 30-minute lunch and two 15-minute breaks, both
configurable in thresholds.json. An active break suppresses idle nudges. It
ends when the driver ends it, when the allotment runs out, or when a fresh
Geotab fix shows the truck moving (``moving_speed_mph``).

Running past the allotment by more than ``overrun_grace_s`` is flagged. The
grace covers the ~20s route poll, so ending on the next poll is not an overrun.
A start past the allowance is refused and flagged. Nothing is written to public.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Optional

from .config import break_settings
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
    if break_type == "lunch":
        return int(cfg["lunch_duration_s"])
    return int(cfg["break_duration_s"])


def allowance(cfg: dict, break_type: str) -> int:
    if break_type == "lunch":
        return int(cfg["lunch_allowance"])
    return int(cfg["break_allowance"])


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
    """Why an active break should end on this poll, or None to leave it running."""
    if not active:
        return None
    if fresh and moving:
        return "moving"
    ends = aware(active["started_at"]) + datetime.timedelta(seconds=int(active["allowed_s"]))
    if aware(now) >= ends:
        return "expired"
    return None


def _counts(rows: list[dict]) -> tuple[int, int]:
    lunch = sum(1 for row in rows if row.get("break_type") == "lunch")
    breaks = sum(1 for row in rows if row.get("break_type") == "break")
    return lunch, breaks


def wire_state(
    rows: list[dict],
    *,
    active: Optional[dict],
    latest_flag: Optional[dict],
    now: datetime.datetime,
    cfg: Optional[dict] = None,
) -> dict:
    cfg = cfg or load_config()
    now = aware(now)
    lunch_used, break_used = _counts(rows)
    body = {
        "active": False,
        "type": None,
        "startedAt": None,
        "endsAt": None,
        "remainingS": 0,
        "allowedS": None,
        "lunchUsed": lunch_used,
        "lunchAllowed": int(cfg["lunch_allowance"]),
        "breakUsed": break_used,
        "breakAllowed": int(cfg["break_allowance"]),
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


def load_breaks(cur, session_id: str) -> list[dict]:
    cur.execute(
        """
        SELECT id, break_type, started_at, ended_at, allowed_s, duration_s,
               overrun_s, flagged, end_reason, truck_no, driver_id
        FROM shift_break
        WHERE session_id = %s
        ORDER BY started_at ASC
        FOR UPDATE
        """,
        (session_id,),
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
    stats = end_stats(row["started_at"], now, int(row["allowed_s"]), int(cfg["overrun_grace_s"]))
    cur.execute(
        """
        UPDATE shift_break
           SET ended_at = %s, duration_s = %s, end_reason = %s, overrun_s = %s, flagged = %s
         WHERE id = %s
        """,
        (aware(now), stats["duration_s"], reason, stats["overrun_s"], stats["flagged"], row["id"]),
    )
    row = dict(row)
    row.update(
        {
            "ended_at": aware(now),
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
    return {
        "id": row["id"],
        "type": row["break_type"],
        "reason": row.get("end_reason"),
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
    rows = load_breaks(cur, session_id)
    latest = load_latest_flag(cur, session_id)
    wrote = False
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
    rows = load_breaks(cur, session_id)
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
    used = _counts(rows)[0 if break_type == "lunch" else 1]
    allowed = allowance(cfg, break_type)
    if used >= allowed:
        noun = "lunch" if break_type == "lunch" else "break"
        detail = (
            f"This shift already has its {noun}."
            if break_type == "lunch"
            else f"This shift already used {allowed} breaks."
        )
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
    _lock_session(cur, session_id)
    rows = load_breaks(cur, session_id)
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

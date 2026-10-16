"""Zero-idle nudges for a parked truck with no Square sale.

A truck is either selling or driving. The route poll (~20s) asks this module
whether the current Geotab fix is a fresh stationary sample and whether Square
has rung a sale for this truck. At a planned pin the driver gets
``no_sale_move_on_s`` (90s) to make a sale. Parked away from every pin, the
wait is only ``off_stop_grace_s`` (15s, the same settle-to-park debounce the
motion classifier already uses) and still requires no recent sale.

Ignoring the nudge escalates: banner, then a voice-level nudge, then a durable
manager flag for Felix. An active lunch or break suppresses the nudge. A fix
older than ``DRIVER_COACH_MAX_FIX_AGE_S`` (else motion ``max_fix_age_s``) never
opens, escalates, or clears an episode.

Episodes land in ``idle_episode`` inside the driver_coach schema.
"""
from __future__ import annotations

import datetime
import json
import math
import uuid
from dataclasses import dataclass
from typing import Optional

from .config import max_fix_age_s, motion_config, no_idle_settings
from .geotab import haversine_m

MANAGER_NAME = "Felix"
_LEVELS = ("banner", "voice", "manager")


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def aware(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def iso_z(value: Optional[datetime.datetime]) -> Optional[str]:
    stamp = aware(value)
    if stamp is None:
        return None
    return stamp.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class NoIdleConfig:
    no_sale_move_on_s: float = 90.0
    off_stop_grace_s: float = 15.0
    voice_after_s: float = 60.0
    manager_alert_after_s: float = 180.0


def load_config() -> NoIdleConfig:
    raw = no_idle_settings()
    return NoIdleConfig(
        no_sale_move_on_s=raw["no_sale_move_on_s"],
        off_stop_grace_s=raw["off_stop_grace_s"],
        voice_after_s=raw["voice_after_s"],
        manager_alert_after_s=raw["manager_alert_after_s"],
    )


@dataclass(frozen=True)
class MotionView:
    """One poll's motion, already gated on fix freshness and thresholds.json."""

    fresh: bool
    stationary: bool
    moving: bool
    at_stop: bool
    stop_order: Optional[int] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    speed_mph: Optional[float] = None
    fix_age_s: Optional[float] = None


@dataclass
class IdleDecision:
    nudge: Optional[dict] = None
    open_episode: Optional[dict] = None
    closed: Optional[dict] = None
    opened_new: bool = False
    dirty: bool = False


def _speed(position: Optional[dict]) -> Optional[float]:
    if not isinstance(position, dict):
        return None
    speed = position.get("speed")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        return None
    if not math.isfinite(float(speed)):
        return None
    return float(speed)


def _coord(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _nearest_pin(lat: float, lng: float, plan, radius_m: float):
    best = None
    best_d = None
    for stop in plan or []:
        if not isinstance(stop, dict):
            continue
        slat, slng = _coord(stop.get("lat")), _coord(stop.get("lng"))
        if slat is None or slng is None:
            continue
        dist = haversine_m(lat, lng, slat, slng)
        if best_d is None or dist < best_d:
            best, best_d = stop, dist
    if best is not None and best_d is not None and best_d <= radius_m:
        return best
    return None


def motion_view(
    position: Optional[dict],
    *,
    phase: Optional[str],
    parked_stop: Optional[dict],
    plan,
    max_age_s: Optional[float] = None,
    settled_mph: Optional[float] = None,
    moving_mph: Optional[float] = None,
    radius_m: Optional[float] = None,
) -> MotionView:
    """Classify a Geotab position for no-idle.

    Speed is mph, the unit ``geotab_live.parse_device_status`` already converts
    to. ``settled_speed_mph`` / ``moving_speed_mph`` / ``parked_radius_m`` come
    from thresholds.json. A fix older than the max age is not fresh, so it
    cannot count as stationary or moving.
    """
    if settled_mph is None or moving_mph is None or radius_m is None:
        motion = motion_config()
        if settled_mph is None:
            settled_mph = motion.settled_speed_mph
        if moving_mph is None:
            moving_mph = motion.moving_speed_mph
        if radius_m is None:
            radius_m = motion.parked_radius_m
    if max_age_s is None:
        max_age_s = max_fix_age_s()

    lat = _coord(position.get("lat")) if isinstance(position, dict) else None
    lng = _coord(position.get("lng")) if isinstance(position, dict) else None
    age = _coord(position.get("fix_age_s")) if isinstance(position, dict) else None
    speed = _speed(position)
    fresh = lat is not None and lng is not None and age is not None and age <= max_age_s
    moving = bool(fresh and speed is not None and speed >= moving_mph)
    settled = bool(fresh and speed is not None and speed <= settled_mph)
    gear_parked = bool(fresh and isinstance(parked_stop, dict) and not moving)
    phase_parked = bool(fresh and phase == "parked" and not moving)
    stationary = bool(fresh and not moving and (settled or gear_parked or phase_parked))

    at_stop = False
    stop_order = None
    if fresh:
        pin = _nearest_pin(lat, lng, plan, radius_m)
        if pin is not None:
            at_stop = True
            order = pin.get("stop_order")
            stop_order = order if isinstance(order, int) and not isinstance(order, bool) else None
        elif isinstance(parked_stop, dict):
            at_stop = True
            order = parked_stop.get("stop_order")
            stop_order = order if isinstance(order, int) and not isinstance(order, bool) else None
        elif phase == "parked":
            at_stop = True
    return MotionView(
        fresh=fresh,
        stationary=stationary,
        moving=moving,
        at_stop=at_stop,
        stop_order=stop_order,
        lat=lat if fresh else None,
        lng=lng if fresh else None,
        speed_mph=speed,
        fix_age_s=age,
    )


def _copy_episode(episode: dict) -> dict:
    copied = dict(episode)
    copied["started_at"] = aware(episode["started_at"])
    copied["nudge_since"] = aware(episode.get("nudge_since"))
    copied["manager_alerted_at"] = aware(episode.get("manager_alerted_at"))
    copied["nudges"] = [dict(item) for item in (episode.get("nudges") or [])]
    return copied


def _blank_episode(now: datetime.datetime, view: MotionView) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "started_at": aware(now),
        "at_stop": bool(view.at_stop),
        "stop_order": view.stop_order,
        "lat": view.lat,
        "lng": view.lng,
        "nudges": [],
        "nudge_since": None,
        "manager_alerted": False,
        "manager_alerted_at": None,
        "manager_alert_for": None,
        "duration_s": 0,
    }


def nudge_message(level: str, at_stop: bool, no_sale_s: float, next_stop_name: Optional[str]) -> str:
    place = f"the next stop ({next_stop_name})" if next_stop_name else "the next stop"
    seconds = int(no_sale_s)
    if level == "banner" and at_stop:
        return f"No sale for {seconds} seconds. Move to {place}."
    if level == "banner":
        return f"Parked off the route with no sale. Move to {place}."
    if level == "voice" and at_stop:
        return f"Still no sale. Leave now and drive to {place}."
    if level == "voice":
        return f"You're still parked off the route. Drive to {place} now."
    if at_stop:
        return f"Still no sale. Felix has been alerted. Move to {place}."
    return f"Parked off the route with no sale. Felix has been alerted. Move to {place}."


def _close(episode: dict, ended_at: datetime.datetime, reason: str) -> dict:
    ended = aware(ended_at)
    started = aware(episode["started_at"])
    duration = max(0, int(round((ended - started).total_seconds())))
    closed = dict(episode)
    closed["ended_at"] = ended
    closed["duration_s"] = duration
    closed["end_reason"] = reason
    return closed


def _wire(episode: dict, level: str, now: datetime.datetime, last_sale_at, next_stop_name, message: str) -> dict:
    started = aware(episode["started_at"])
    current = aware(now)
    idle_for = max(0, int((current - started).total_seconds()))
    if last_sale_at is None:
        no_sale_for = None
    else:
        no_sale_for = max(0, int((current - aware(last_sale_at)).total_seconds()))
    alert_at = episode.get("manager_alerted_at") if level == "manager" else None
    return {
        "level": level,
        "message": message,
        "since": iso_z(started),
        "idleForS": idle_for,
        "noSaleForS": no_sale_for,
        "atStop": bool(episode.get("at_stop")),
        "episodeId": episode["id"],
        "nextStopName": next_stop_name,
        "managerAlert": level == "manager",
        "managerAlertAt": iso_z(alert_at),
        "managerAlertFor": MANAGER_NAME if level == "manager" else None,
    }


def _apply_nudge(
    episode: dict,
    now: datetime.datetime,
    cfg: NoIdleConfig,
    last_sale_at,
    next_stop_name: Optional[str],
) -> dict:
    """Escalate in place. The first due sample is always a banner."""
    current = aware(now)
    if episode.get("nudge_since") is None:
        episode["nudge_since"] = current
    elapsed = (current - aware(episode["nudge_since"])).total_seconds()
    reached = ["banner"]
    if elapsed > 0 and elapsed >= cfg.voice_after_s:
        reached.append("voice")
    if elapsed > 0 and elapsed >= cfg.manager_alert_after_s:
        reached.append("manager")
    have = {item.get("level") for item in episode.get("nudges") or []}
    for level in reached:
        if level in have:
            continue
        message = nudge_message(level, bool(episode.get("at_stop")), cfg.no_sale_move_on_s, next_stop_name)
        episode["nudges"].append(
            {
                "level": level,
                "at": iso_z(current),
                "message": message,
                "managerAlert": level == "manager",
            }
        )
        have.add(level)
    level = reached[-1]
    if level == "manager" and not episode.get("manager_alerted"):
        episode["manager_alerted"] = True
        episode["manager_alerted_at"] = current
        episode["manager_alert_for"] = MANAGER_NAME
    message = nudge_message(level, bool(episode.get("at_stop")), cfg.no_sale_move_on_s, next_stop_name)
    return _wire(episode, level, current, last_sale_at, next_stop_name, message)


def _due(episode: dict, now: datetime.datetime, last_sale_at, cfg: NoIdleConfig) -> bool:
    stationary_for = (aware(now) - aware(episode["started_at"])).total_seconds()
    if episode.get("at_stop"):
        dwell_ok = stationary_for >= cfg.no_sale_move_on_s
    else:
        dwell_ok = stationary_for >= cfg.off_stop_grace_s
    if last_sale_at is None:
        no_sale_ok = True
    else:
        no_sale_ok = (aware(now) - aware(last_sale_at)).total_seconds() >= cfg.no_sale_move_on_s
    return bool(dwell_ok and no_sale_ok)


def decide_idle(
    *,
    now: datetime.datetime,
    view: MotionView,
    sales_known: bool,
    last_sale_at: Optional[datetime.datetime],
    break_active: bool,
    episode: Optional[dict],
    cfg: NoIdleConfig,
    next_stop_name: Optional[str] = None,
) -> IdleDecision:
    """Pure poll step. ``episode`` is the open row, or None.

    The returned open episode keeps the at-stop classification it opened with,
    so a GPS wobble across the 40m pin does not reset the 90s sale window.
    """
    now = aware(now)
    last_sale_at = aware(last_sale_at)
    if last_sale_at is not None and last_sale_at > now:
        last_sale_at = now
    episode = _copy_episode(episode) if episode else None

    if break_active:
        if episode is None:
            return IdleDecision()
        return IdleDecision(closed=_close(episode, now, "break"))

    if not view.fresh:
        if episode is None:
            return IdleDecision()
        return IdleDecision(open_episode=episode, dirty=False)

    if view.moving:
        if episode is None:
            return IdleDecision()
        return IdleDecision(closed=_close(episode, now, "moved"))

    if not sales_known:
        if episode is None:
            return IdleDecision()
        return IdleDecision(open_episode=episode, dirty=False, nudge=None)

    closed = None
    opened_new = False
    if episode is not None and last_sale_at is not None and last_sale_at > aware(episode["started_at"]):
        closed = _close(episode, last_sale_at, "sale")
        episode = _blank_episode(last_sale_at, view)
        # The restarted episode keeps the pin class of where they are now.
        opened_new = True

    if episode is None:
        if not view.stationary:
            return IdleDecision(closed=closed)
        episode = _blank_episode(now, view)
        opened_new = True

    episode["duration_s"] = max(0, int(round((now - aware(episode["started_at"])).total_seconds())))
    nudge = None
    if _due(episode, now, last_sale_at, cfg):
        nudge = _apply_nudge(episode, now, cfg, last_sale_at, next_stop_name)
    return IdleDecision(
        nudge=nudge,
        open_episode=episode,
        closed=closed,
        opened_new=opened_new,
        dirty=True,
    )


def _nudges(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, list) else []


def _episode_from_row(row) -> dict:
    return {
        "id": str(row[0]),
        "started_at": aware(row[1]),
        "lat": row[2],
        "lng": row[3],
        "at_stop": bool(row[4]),
        "stop_order": row[5],
        "nudges": _nudges(row[6]),
        "nudge_since": aware(row[7]),
        "manager_alerted": bool(row[8]),
        "manager_alerted_at": aware(row[9]),
        "manager_alert_for": row[10],
        "duration_s": row[11],
    }


def load_open(cur, session_id: str) -> list[dict]:
    cur.execute(
        """
        SELECT id, started_at, lat, lng, at_stop, stop_order, nudges, nudge_since,
               manager_alerted, manager_alerted_at, manager_alert_for, duration_s
        FROM idle_episode
        WHERE session_id = %s AND ended_at IS NULL
        ORDER BY started_at DESC
        FOR UPDATE
        """,
        (session_id,),
    )
    return [_episode_from_row(row) for row in cur.fetchall()]


def _write_close(cur, closed: dict) -> None:
    cur.execute(
        """
        UPDATE idle_episode
           SET ended_at = %s, duration_s = %s, end_reason = %s
         WHERE id = %s
        """,
        (closed["ended_at"], closed["duration_s"], closed["end_reason"], closed["id"]),
    )


def _write_insert(cur, episode: dict, *, session_id: str, truck_no: int, driver_id: Optional[str]) -> None:
    cur.execute(
        """
        INSERT INTO idle_episode (
            id, session_id, truck_no, driver_id, started_at, duration_s, lat, lng,
            at_stop, stop_order, nudges, nudge_since, manager_alerted,
            manager_alerted_at, manager_alert_for
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
        """,
        (
            episode["id"],
            session_id,
            truck_no,
            driver_id,
            episode["started_at"],
            episode.get("duration_s"),
            episode.get("lat"),
            episode.get("lng"),
            bool(episode.get("at_stop")),
            episode.get("stop_order"),
            json.dumps(episode.get("nudges") or []),
            episode.get("nudge_since"),
            bool(episode.get("manager_alerted")),
            episode.get("manager_alerted_at"),
            episode.get("manager_alert_for"),
        ),
    )


def _write_update(cur, episode: dict) -> None:
    cur.execute(
        """
        UPDATE idle_episode
           SET duration_s = %s, nudges = %s::jsonb, nudge_since = %s,
               manager_alerted = %s, manager_alerted_at = %s, manager_alert_for = %s
         WHERE id = %s AND ended_at IS NULL
        """,
        (
            episode.get("duration_s"),
            json.dumps(episode.get("nudges") or []),
            episode.get("nudge_since"),
            bool(episode.get("manager_alerted")),
            episode.get("manager_alerted_at"),
            episode.get("manager_alert_for"),
            episode["id"],
        ),
    )


def apply_decision(
    cur,
    decision: IdleDecision,
    *,
    session_id: str,
    truck_no: int,
    driver_id: Optional[str],
) -> bool:
    wrote = False
    if decision.closed is not None:
        _write_close(cur, decision.closed)
        wrote = True
    if decision.open_episode is not None and decision.opened_new:
        _write_insert(
            cur, decision.open_episode, session_id=session_id, truck_no=truck_no, driver_id=driver_id
        )
        wrote = True
    elif decision.open_episode is not None and decision.dirty:
        _write_update(cur, decision.open_episode)
        wrote = True
    return wrote


def close_open(cur, session_id: str, now: Optional[datetime.datetime] = None, reason: str = "break") -> bool:
    """End every open idle episode for this session. Used when a break starts."""
    now = aware(now or utcnow())
    open_rows = load_open(cur, session_id)
    if not open_rows:
        return False
    for episode in open_rows:
        _write_close(cur, _close(episode, now, reason))
    return True


def reconcile_on_poll(
    cur,
    *,
    session_id: str,
    truck_no: int,
    driver_id: Optional[str],
    driver_name: str = "",
    position: Optional[dict] = None,
    phase: Optional[str] = None,
    parked_stop: Optional[dict] = None,
    plan=None,
    view: Optional[MotionView] = None,
    break_active: bool = False,
    next_stop_name: Optional[str] = None,
    sale: Optional[dict] = None,
    now: Optional[datetime.datetime] = None,
    cfg: Optional[NoIdleConfig] = None,
) -> tuple[Optional[dict], bool]:
    """Advance the open episode for this poll. Returns ``(nudge or None, wrote)``."""
    now = aware(now or utcnow())
    cfg = cfg or load_config()
    if view is None:
        view = motion_view(position, phase=phase, parked_stop=parked_stop, plan=plan)
    if sale is None:
        sale = _lookup_sale(truck_no, driver_name, now)
    open_rows = load_open(cur, session_id)
    episode = open_rows[0] if open_rows else None
    wrote = False
    for extra in open_rows[1:]:
        _write_close(cur, _close(extra, now, "superseded"))
        wrote = True
    decision = decide_idle(
        now=now,
        view=view,
        sales_known=bool(sale.get("known")),
        last_sale_at=sale.get("last_sale_at"),
        break_active=break_active,
        episode=episode,
        cfg=cfg,
        next_stop_name=next_stop_name,
    )
    wrote = apply_decision(
        cur, decision, session_id=session_id, truck_no=int(truck_no), driver_id=driver_id
    ) or wrote
    return decision.nudge, wrote


def _lookup_sale(truck_no: int, driver_name: str, now: datetime.datetime) -> dict:
    try:
        from . import scorecard

        found = scorecard.last_truck_sale(truck_no=int(truck_no), driver_name=driver_name or "", now=now)
    except Exception:
        return {"known": False, "last_sale_at": None}
    if not isinstance(found, dict):
        return {"known": False, "last_sale_at": None}
    return {
        "known": bool(found.get("known")),
        "last_sale_at": found.get("last_sale_at"),
    }

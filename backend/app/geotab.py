"""Geotab position/speed reader -> a reference GeotabFix.

Speed truth is Geotab (server, ~2-min cadence), NOT iPad GPS (SERVER_FLAGS spec
§4.1; same independence argument as the turn-off spec). The stack ingests Geotab
into public.log_records(device_id, latitude, longitude, date_time). log_records
does not carry an explicit speed column, so we derive instantaneous speed from the
two most recent pings (haversine distance / dt) — the same "breadcrumb" approach
geotab_trace.py uses. This is the wiring layer the reference core is designed to
sit behind (motion_state.py takes speed + distance pre-computed).

distance-to-next-stop = haversine(latest position, next-stop centroid) in meters.
fix_age_s = now - latest ping time (so a stale Geotab fix holds the last phase per
the core's safety rule).

READ-ONLY against public. Maker: Forge. Reviewer: Warden. No deploy without Felix.
"""
from __future__ import annotations

import datetime
import math
from typing import Optional

from .refcore import GeotabFix

_EARTH_M = 6371000.0
_MPS_TO_MPH = 2.2369362920544


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_M * math.asin(min(1.0, math.sqrt(a)))


def _recent_pings(cur, truck_id: str, limit: int = 2) -> list:
    cur.execute(
        """
        SELECT date_time, latitude, longitude
        FROM public.log_records
        WHERE device_id = %s
        ORDER BY date_time DESC
        LIMIT %s
        """,
        (truck_id, limit),
    )
    return cur.fetchall()


def latest_fix(
    cur,
    truck_id: str,
    next_stop_lat: Optional[float],
    next_stop_lng: Optional[float],
    now: Optional[datetime.datetime] = None,
) -> Optional[GeotabFix]:
    """Build a GeotabFix from the two most-recent Geotab pings for `truck_id`.

    speed_mph derived from the last two pings; dist_to_next_stop_m from the latest
    position to the next-stop centroid (None if we have no next stop). Returns None
    if the truck has no pings at all (caller then holds last phase / defaults)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    pings = _recent_pings(cur, truck_id, limit=2)
    if not pings:
        return None
    t0, lat0, lng0 = pings[0]
    lat0, lng0 = float(lat0), float(lng0)

    speed_mph = 0.0
    if len(pings) >= 2:
        t1, lat1, lng1 = pings[1]
        dt_s = (t0 - t1).total_seconds()
        if dt_s > 0:
            dist_m = haversine_m(float(lat1), float(lng1), lat0, lng0)
            speed_mph = (dist_m / dt_s) * _MPS_TO_MPH

    dist_to_stop = None
    if next_stop_lat is not None and next_stop_lng is not None:
        dist_to_stop = haversine_m(lat0, lng0, next_stop_lat, next_stop_lng)

    fix_age_s = max((now - t0).total_seconds(), 0.0)
    return GeotabFix(
        speed_mph=round(speed_mph, 2),
        dist_to_next_stop_m=round(dist_to_stop, 1) if dist_to_stop is not None else None,
        fix_age_s=round(fix_age_s, 1),
        ignition_on=True,  # log_records carries no ignition column; conservative default
    )

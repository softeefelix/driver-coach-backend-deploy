"""The path a truck actually drove, and where it actually stopped.

The blue line is the prescribed route. This module is the other half of the
review: yellow pellets for the driven path, and a pellet about four times that
size wherever the transmission went into Park.

Park is Geotab gear value 126 on diagnostic DiagnosticGearPositionId — the same
signal Geotab uses. A speed of zero is not a stop. Gear rows have no coordinates,
so each Park transition is joined to the nearest GPS log at that timestamp.

READ-ONLY. A Geotab miss returns empty lists. Nothing here raises to the caller.
"""
from __future__ import annotations

import datetime
from typing import Optional

PARK = 126
GEAR_DIAGNOSTIC = "DiagnosticGearPositionId"
# Keep the cab payload small. A full day of 1 Hz logs is thousands of points.
_MAX_CRUMBS = 400
_MAX_STOPS = 80
_PARK_MATCH_S = 120
# A Park counts as making that planned stop only if the shifter went into Park
# near the pin. 120 m is about a short block: a curb stop whose pin sits across
# the street still counts, a park on the next street does not. 80 m missed a
# real curb stop by one meter (Alameda de las Pulgas, truck 7, 81 m).
SERVED_PARK_M = 120


def _utc(value) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _num(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def thin_crumbs(points: list[dict], limit: int = _MAX_CRUMBS) -> list[dict]:
    """Keep the first, the last, and an even sample in between. Order preserved."""
    if len(points) <= limit:
        return points
    if limit < 2:
        return points[:1]
    step = (len(points) - 1) / (limit - 1)
    picked = []
    seen = set()
    for i in range(limit):
        idx = round(i * step)
        if idx not in seen:
            seen.add(idx)
            picked.append(points[idx])
    return picked


def fold_driven(
    logs,
    gear_rows,
    *,
    now: Optional[datetime.datetime] = None,
) -> dict:
    """Pure fold of Geotab rows into {crumbs, stops}.

    crumbs: [{lng, lat}] in time order, thinned.
    stops:  [{lng, lat, at}] — one per transition INTO Park (126), located by
            the GPS log nearest that timestamp within two minutes.
    """
    crumbs = []
    for row in logs or []:
        if not isinstance(row, dict):
            continue
        lat, lng = _num(row.get("latitude")), _num(row.get("longitude"))
        if lat is None or lng is None:
            continue
        crumbs.append({"lng": lng, "lat": lat, "at": _utc(row.get("dateTime"))})
    crumbs.sort(key=lambda p: p["at"] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))

    parks = []
    for row in gear_rows or []:
        if not isinstance(row, dict) or row.get("data") != PARK:
            continue
        at = _utc(row.get("dateTime"))
        if at is not None:
            parks.append(at)
    parks = sorted(set(parks))

    stops = []
    for at in parks:
        nearest = None
        nearest_gap = None
        for crumb in crumbs:
            if crumb["at"] is None:
                continue
            gap = abs((crumb["at"] - at).total_seconds())
            if nearest_gap is None or gap < nearest_gap:
                nearest, nearest_gap = crumb, gap
        if nearest is None or nearest_gap is None or nearest_gap > _PARK_MATCH_S:
            continue
        stops.append({"lng": nearest["lng"], "lat": nearest["lat"], "at": at.isoformat()})
        if len(stops) >= _MAX_STOPS:
            break

    thin = thin_crumbs(crumbs)
    return {
        "crumbs": [{"lng": p["lng"], "lat": p["lat"]} for p in thin],
        "stops": stops,
    }


def passed_orders(plan, *, now_lat, now_lng, ahead_m: float = 250) -> list[int]:
    """Earlier planned stops the truck has already driven past.

    The frozen list stays the list. This only drops a stop the truck is no longer
    near, once it is clearly closer to a later pin. It does not invent a new route.
    A stop they are still at is not passed.
    """
    from .geotab import haversine_m

    if now_lat is None or now_lng is None:
        return []
    ranked = []
    for stop in plan or []:
        if not isinstance(stop, dict) or stop.get("kind") == "event":
            continue
        order = stop.get("stop_order")
        lat, lng = stop.get("lat"), stop.get("lng")
        if not isinstance(order, int) or lat is None or lng is None:
            continue
        ranked.append((haversine_m(now_lat, now_lng, float(lat), float(lng)), order))
    if not ranked:
        return []
    nearest_m, nearest_order = min(ranked)
    if nearest_m > ahead_m:
        return []
    return [order for dist, order in ranked if order < nearest_order and dist > ahead_m]


def park_served_orders(plan, park_stops, *, now_lat, now_lng, radius_m: float = SERVED_PARK_M) -> list[int]:
    """Planned stop orders the truck has already made.

    A stop is made when the transmission went into Park within radius_m of that
    pin, and the truck is no longer sitting on it. Still at the pin means the
    stop is in progress — do not advance yet. Park is the only signal. A drive-by
    does not count.
    """
    from .geotab import haversine_m

    if now_lat is None or now_lng is None:
        return []
    made = []
    for stop in plan or []:
        if not isinstance(stop, dict) or stop.get("kind") == "event":
            continue
        order = stop.get("stop_order")
        lat, lng = stop.get("lat"), stop.get("lng")
        if not isinstance(order, int) or lat is None or lng is None:
            continue
        if haversine_m(now_lat, now_lng, float(lat), float(lng)) <= radius_m:
            continue  # still there
        for park in park_stops or []:
            if haversine_m(float(park["lat"]), float(park["lng"]), float(lat), float(lng)) <= radius_m:
                made.append(order)
                break
    return made


def driven_path(device_id: Optional[str], api=None, *, hours: float = 8) -> dict:
    """Today's driven crumbs and Park stops for one Geotab device.

    Returns {crumbs, stops}. Empty on any failure — a review overlay must never
    take down the live map.
    """
    empty = {"crumbs": [], "stops": []}
    if not device_id:
        return empty
    try:
        client = api
        if client is None:
            from .geotab_live import _get_client
            client = _get_client()
        if client is None:
            return empty
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        logs = client.get("LogRecord", search={
            "deviceSearch": {"id": device_id},
            "fromDate": start.isoformat(),
        }) or []
        gear = client.get("StatusData", search={
            "deviceSearch": {"id": device_id},
            "diagnosticSearch": {"id": GEAR_DIAGNOSTIC},
            "fromDate": start.isoformat(),
        }) or []
        return fold_driven(logs, gear)
    except Exception:
        return empty

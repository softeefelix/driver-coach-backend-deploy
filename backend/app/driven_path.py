"""Read the road travelled and Park transitions from Geotab.

Park is Geotab gear value 126 on DiagnosticGearPositionId.  A Park transition is
joined to the closest GPS log and can be matched to a frozen planned pin.  This is
READ-ONLY and deliberately returns empty results on an unavailable Geotab source.
"""
from __future__ import annotations

import datetime
from typing import Optional

PARK = 126
GEAR_DIAGNOSTIC = "DiagnosticGearPositionId"
_PARK_MATCH_S = 120
SERVED_PARK_M = 120


def _utc(value) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _number(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def fold_park_stops(logs, gear_rows) -> list[dict]:
    """Return GPS-located transitions into Park (gear 126), ordered by time."""
    crumbs = []
    for row in logs or []:
        if not isinstance(row, dict):
            continue
        lat, lng = _number(row.get("latitude")), _number(row.get("longitude"))
        at = _utc(row.get("dateTime"))
        if lat is not None and lng is not None and at is not None:
            crumbs.append({"lat": lat, "lng": lng, "at": at})
    parks = sorted({
        at for row in (gear_rows or [])
        if isinstance(row, dict) and row.get("data") == PARK
        for at in [_utc(row.get("dateTime"))] if at is not None
    })
    stops = []
    for at in parks:
        nearest = min(crumbs, key=lambda crumb: abs((crumb["at"] - at).total_seconds()), default=None)
        if nearest is None or abs((nearest["at"] - at).total_seconds()) > _PARK_MATCH_S:
            continue
        # Preserve the authoritative transmission state on the wire. The client
        # refuses to render a large Park pellet unless this remains gear 126.
        stops.append({
            "lat": nearest["lat"], "lng": nearest["lng"], "at": at.isoformat(), "gear": PARK,
        })
    return stops


def fold_driven_points(logs) -> list[dict]:
    """Return every valid GPS breadcrumb in feed order, without fabricating gaps."""
    points = []
    for row in logs or []:
        if not isinstance(row, dict):
            continue
        lat, lng = _number(row.get("latitude")), _number(row.get("longitude"))
        if lat is not None and lng is not None:
            points.append({"lat": lat, "lng": lng})
    return points


def _planned_rows(plan):
    for stop in plan or []:
        if not isinstance(stop, dict) or stop.get("kind") == "event":
            continue
        order, lat, lng = stop.get("stop_order"), stop.get("lat"), stop.get("lng")
        if isinstance(order, int) and isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            yield stop, order, float(lat), float(lng)


def parked_plan_stop(plan, park_stops, *, now_lat, now_lng, radius_m: float = SERVED_PARK_M) -> Optional[dict]:
    """The planned pin currently being served under a Geotab Park transition.

    Both the current truck position and a Park-126 location must be within 120m of
    the same pin.  This keeps a historical Park from presenting a stale Here row.
    """
    from .geotab import haversine_m

    if not isinstance(now_lat, (int, float)) or not isinstance(now_lng, (int, float)):
        return None
    for stop, _, lat, lng in _planned_rows(plan):
        if haversine_m(now_lat, now_lng, lat, lng) > radius_m:
            continue
        if any(
            isinstance(park, dict)
            and isinstance(park.get("lat"), (int, float))
            and isinstance(park.get("lng"), (int, float))
            and haversine_m(float(park["lat"]), float(park["lng"]), lat, lng) <= radius_m
            for park in (park_stops or [])
        ):
            return dict(stop)
    return None


def park_served_orders(plan, park_stops, *, now_lat, now_lng, radius_m: float = SERVED_PARK_M) -> list[int]:
    """Pins parked at earlier and then left; a current Park is never auto-served."""
    from .geotab import haversine_m

    if not isinstance(now_lat, (int, float)) or not isinstance(now_lng, (int, float)):
        return []
    made = []
    for _, order, lat, lng in _planned_rows(plan):
        if haversine_m(now_lat, now_lng, lat, lng) <= radius_m:
            continue
        if any(
            isinstance(park, dict)
            and isinstance(park.get("lat"), (int, float))
            and isinstance(park.get("lng"), (int, float))
            and haversine_m(float(park["lat"]), float(park["lng"]), lat, lng) <= radius_m
            for park in (park_stops or [])
        ):
            made.append(order)
    return made


def passed_orders(plan, *, now_lat, now_lng, ahead_m: float = 250) -> list[int]:
    """Earlier pins clearly passed once the truck is within 250m of a later pin."""
    from .geotab import haversine_m

    if not isinstance(now_lat, (int, float)) or not isinstance(now_lng, (int, float)):
        return []
    ranked = [
        (haversine_m(now_lat, now_lng, lat, lng), order)
        for _, order, lat, lng in _planned_rows(plan)
    ]
    if not ranked:
        return []
    nearest_m, nearest_order = min(ranked)
    if nearest_m > ahead_m:
        return []
    return [order for distance, order in ranked if order < nearest_order and distance > ahead_m]


def driven_path(device_id: Optional[str], api=None, *, hours: float = 8) -> dict:
    """Fetch today's GPS crumbs and Park transitions, never raising."""
    if not device_id:
        return {"driven": [], "stops": []}
    try:
        client = api
        if client is None:
            from .geotab_live import _get_client
            client = _get_client()
        if client is None:
            return {"driven": [], "stops": []}
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        logs = client.get("LogRecord", search={
            "deviceSearch": {"id": device_id}, "fromDate": start.isoformat(),
        }) or []
        gear = client.get("StatusData", search={
            "deviceSearch": {"id": device_id},
            "diagnosticSearch": {"id": GEAR_DIAGNOSTIC}, "fromDate": start.isoformat(),
        }) or []
        return {"driven": fold_driven_points(logs), "stops": fold_park_stops(logs, gear)}
    except Exception:
        return {"driven": [], "stops": []}

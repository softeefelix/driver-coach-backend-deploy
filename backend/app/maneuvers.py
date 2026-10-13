"""Maneuvers follow the stored blue line, not a pin-to-pin shortcut.

A confirmed leg already has a sliced Master Route trace. Sampling that trace
every ~25 points and asking Mapbox driving for steps is what makes the spoken
streets match the drawn line. A route with no stored trace has no waypoints,
so it gets no maneuvers — never a straight line between pins.
"""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Callable, Optional

MAPBOX_DRIVING_URL = "https://api.mapbox.com/directions/v5/mapbox/driving/"
# Mapbox driving accepts 25 coordinates. The truck, samples along the line,
# and the advised stop must all fit in one request.
MAPBOX_MAX_WAYPOINTS = 25
# The card's verified spacing: one waypoint about every 25 stored points.
TRACE_SAMPLE_EVERY = 25
NEXT_MANEUVERS = 3
# One stored leg costs one Mapbox call. A cab polls about every 20 seconds.
LEG_CACHE_TTL_S = 15 * 60
LEG_CACHE_MAX = 500
# A bad minute must not be hammered, and must not be remembered as "no turns".
FAILURE_CACHE_TTL_S = 30
# The first stored point of the leg, rounded so GPS jitter does not split a key.
LEG_POINT_DECIMALS = 5
# A maneuver this far behind the truck's projection on the leg has been passed.
PASSED_MANEUVER_M = 30.0
# Mapbox driving-traffic is the ETA profile. Maneuvers use driving, which is
# the profile the card verified against route 1778.
_TIMEOUT_S = 8.0

# Instructions that name a compass heading and no street. A blank street is
# dropped separately; these are dropped even when a name slipped through.
_HEADING_WORDS = (
    "north", "south", "east", "west",
    "northeast", "northwest", "southeast", "southwest",
)
_HEADING_VERBS = ("head", "drive", "continue")


def sample_trace_waypoints(
    line: list,
    *,
    every: int = TRACE_SAMPLE_EVERY,
    limit: int = MAPBOX_MAX_WAYPOINTS,
) -> list[list[float]]:
    """[lat, lng] samples along a stored trace, always keeping both ends.

    An empty or single-point line returns nothing. Callers must not invent a
    chord between pins when this comes back empty.
    """
    if every < 1 or limit < 2:
        return []
    points: list[list[float]] = []
    for point in line or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return []
        try:
            lat, lng = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return []
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return []
        points.append([lat, lng])
    if len(points) < 2:
        return []
    sampled = points[::every]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    if len(sampled) <= limit:
        return sampled
    # Too many samples for one request: keep the start, then evenly thin the
    # rest so the request still tracks the whole sliced line.
    head = sampled[0]
    rest = sampled[1:]
    slots = limit - 1
    thinned = [rest[round(i * (len(rest) - 1) / (slots - 1))] for i in range(slots)]
    return [head, *thinned]


def is_heading_only(instruction: str) -> bool:
    """True for 'Head north', 'Drive southeast', 'Continue', and their kin.

    A sentence that also names a street ('Turn right onto Fremont Boulevard')
    is not heading-only. 'Continue on Mowry Avenue' names a street, so it stays.
    """
    text = " ".join((instruction or "").strip().casefold().rstrip(".").split())
    if not text:
        return True
    words = text.split()
    if words[0] not in _HEADING_VERBS:
        return False
    if words[0] == "continue" and len(words) == 1:
        return True
    if len(words) == 2 and words[1] in _HEADING_WORDS:
        return True
    return False


def named_maneuvers(steps: list, *, limit: int = NEXT_MANEUVERS) -> list[dict]:
    """Keep the next maneuvers that name a street. Drop the rest.

    A blank street is dropped. A heading-only instruction is dropped even if
    it carries a street string. The wire shape is exactly
    {street, distance_m, modifier} — no coordinates, no instruction text.
    """
    kept: list[dict] = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        street = step.get("street")
        if not isinstance(street, str) or not street.strip():
            continue
        instruction = step.get("instruction")
        if isinstance(instruction, str) and is_heading_only(instruction):
            continue
        distance = step.get("distance_m")
        if not isinstance(distance, (int, float)) or distance < 0:
            continue
        modifier = step.get("modifier")
        if not isinstance(modifier, str) or not modifier.strip():
            modifier = ""
        kept.append({
            "street": street.strip(),
            "distance_m": int(round(float(distance))),
            "modifier": modifier.strip(),
        })
        if len(kept) >= limit:
            break
    return kept


def parse_driving_steps(data: Optional[dict]) -> list[dict]:
    """Fold a Mapbox driving response into raw steps. No filtering here."""
    if not isinstance(data, dict) or not data.get("routes"):
        return []
    out = []
    for leg in (data["routes"][0] or {}).get("legs") or []:
        for step in leg.get("steps") or []:
            maneuver = step.get("maneuver") or {}
            instruction = maneuver.get("instruction")
            distance = step.get("distance")
            if not isinstance(instruction, str) or not isinstance(distance, (int, float)):
                continue
            modifier = maneuver.get("modifier")
            location = maneuver.get("location")
            lat = lng = None
            if (
                isinstance(location, (list, tuple))
                and len(location) >= 2
                and isinstance(location[0], (int, float))
                and isinstance(location[1], (int, float))
            ):
                lng, lat = float(location[0]), float(location[1])
            out.append({
                "instruction": instruction,
                "distance_m": round(float(distance)),
                "street": step.get("name") if isinstance(step.get("name"), str) else "",
                "modifier": modifier if isinstance(modifier, str) else "",
                "lat": lat,
                "lng": lng,
            })
    return out


def _default_fetch(coords: str, token: str, timeout: float) -> Optional[dict]:
    url = MAPBOX_DRIVING_URL + coords + "?" + urllib.parse.urlencode({
        "access_token": token,
        "steps": "true",
        "geometries": "geojson",
        "overview": "false",
        "banner_instructions": "false",
        "voice_instructions": "false",
    })
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # nosec - fixed Mapbox host
            if getattr(response, "status", 200) != 200:
                return None
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except Exception:
        return None


def _now() -> float:
    return time.monotonic()


# Process-local. Values hold server-side locations; nothing here is serialized.
_leg_cache: "OrderedDict[tuple, tuple]" = OrderedDict()


def _round_point(point) -> Optional[tuple]:
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return None
    try:
        lat, lng = float(point[0]), float(point[1])
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    places = LEG_POINT_DECIMALS
    return (round(lat, places), round(lng, places))


def leg_cache_key(line, *, route_cluster_id, dow, next_stop_order) -> Optional[tuple]:
    """One entry per stored leg. Two routes never share it.

    The first stored trace point, not the truck, is what stays stable across
    polls. Rounding to 5 decimals keeps a meter of GPS noise from splitting
    the key. A missing identity is not a key: the caller fetches uncached.
    """
    origin = _round_point((line or [None])[0] if line else None)
    if origin is None:
        return None
    if not isinstance(route_cluster_id, int) or isinstance(route_cluster_id, bool):
        return None
    if not isinstance(dow, str) or not dow:
        return None
    if not isinstance(next_stop_order, int) or isinstance(next_stop_order, bool):
        return None
    return (route_cluster_id, dow, next_stop_order, origin)


def _cache_get(key: tuple):
    hit = _leg_cache.get(key)
    if hit is None:
        return None
    _stored_at, _ttl, _ok, _steps = hit
    if _now() - _stored_at > _ttl:
        _leg_cache.pop(key, None)
        return None
    _leg_cache.move_to_end(key)
    return hit


def _cache_put(key: tuple, *, ok: bool, steps: list) -> None:
    ttl = LEG_CACHE_TTL_S if ok else FAILURE_CACHE_TTL_S
    _leg_cache[key] = (_now(), ttl, ok, steps)
    _leg_cache.move_to_end(key)
    while len(_leg_cache) > LEG_CACHE_MAX:
        _leg_cache.popitem(last=False)


def _distance_m(a_lat, a_lng, b_lat, b_lng) -> float:
    # Equirectangular meters. A neighborhood leg does not need a geodesic.
    mean_lat = math.radians((a_lat + b_lat) / 2)
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng) * math.cos(mean_lat)
    return math.hypot(dlat, dlng) * 6_371_000


def _projection_m(line: list, truck) -> Optional[float]:
    """Meters from the start of the stored leg to the truck's projection.

    The projection walks the stored trace, not the Mapbox geometry, so a
    poll can drop passed maneuvers without another call.
    """
    origin = _round_point(truck)
    if origin is None or not line or len(line) < 2:
        return None
    lat, lng = float(truck[0]), float(truck[1])
    best = None
    walked = 0.0
    for a, b in zip(line, line[1:]):
        try:
            alat, alng = float(a[0]), float(a[1])
            blat, blng = float(b[0]), float(b[1])
        except (TypeError, ValueError, IndexError):
            return None
        seg = _distance_m(alat, alng, blat, blng)
        if seg <= 0:
            continue
        t = (
            (lat - alat) * (blat - alat) + (lng - alng) * (blng - alng)
        ) / ((blat - alat) ** 2 + (blng - alng) ** 2)
        t = min(1.0, max(0.0, t))
        plat, plng = alat + t * (blat - alat), alng + t * (blng - alng)
        off = _distance_m(lat, lng, plat, plng)
        along = walked + seg * t
        if best is None or off < best[0]:
            best = (off, along)
        walked += seg
    if best is None:
        return None
    return best[1]


def _steps_with_along(steps: list, line: list) -> list[dict]:
    """Attach each maneuver's distance-along-the-leg. Locations stay here."""
    out = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        copied = dict(step)
        lat, lng = copied.get("lat"), copied.get("lng")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            copied["_along_m"] = _projection_m(line, (lat, lng))
        else:
            copied["_along_m"] = None
        out.append(copied)
    return out


def ahead_of_truck(steps: list, line: list, truck, *, limit: int = NEXT_MANEUVERS) -> list[dict]:
    """Named maneuvers still ahead of the truck. Wire shape only.

    A maneuver behind the truck's projection on the stored leg is dropped.
    The whole cached leg is filtered, then capped at `limit`, so passing the
    first named turn reveals the next one without a new Mapbox call.
    """
    along = _projection_m(line, truck) if truck is not None else None
    eligible = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if along is not None:
            step_along = step.get("_along_m")
            if isinstance(step_along, (int, float)) and step_along + PASSED_MANEUVER_M < along:
                continue
        eligible.append(step)
    return named_maneuvers(eligible, limit=limit)


def maneuvers_along_line(
    line: Optional[list],
    *,
    token: Optional[str],
    fetch: Optional[Callable] = None,
    timeout_s: float = _TIMEOUT_S,
    limit: int = NEXT_MANEUVERS,
    truck: Optional[tuple] = None,
    route_cluster_id: Optional[int] = None,
    dow: Optional[str] = None,
    next_stop_order: Optional[int] = None,
) -> list[dict]:
    """Next named maneuvers that track `line`. No line means no maneuvers.

    A complete leg identity is cached process-locally: one Mapbox call per
    leg, then the next named maneuvers ahead of the truck. A missing identity
    still fetches, but is not cached — two unknown legs must not share a
    result. A Mapbox failure returns [] and is remembered for at most 30 s.
    """
    if not token or not line:
        return []
    waypoints = sample_trace_waypoints(line)
    if len(waypoints) < 2:
        return []
    key = leg_cache_key(
        line,
        route_cluster_id=route_cluster_id,
        dow=dow,
        next_stop_order=next_stop_order,
    )
    cached = _cache_get(key) if key is not None else None
    if cached is not None:
        _stored_at, _ttl, ok, steps = cached
        if not ok:
            return []
        return ahead_of_truck(steps, line, truck, limit=limit)
    coords = ";".join(f"{lng},{lat}" for lat, lng in waypoints)
    try:
        data = (fetch or _default_fetch)(coords, token, timeout_s)
    except Exception:
        data = None
    if not data:
        if key is not None:
            _cache_put(key, ok=False, steps=[])
        return []
    stored = _steps_with_along(parse_driving_steps(data), line)
    if key is not None:
        _cache_put(key, ok=True, steps=stored)
    return ahead_of_truck(stored, line, truck, limit=limit)

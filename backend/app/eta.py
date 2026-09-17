"""Traffic-aware live ETA/distance to the next stop (Mapbox driving-traffic).

The shipped app painted a HARDCODED "6 min" and showed the BOOKED schedule time as
if it were an ETA (Felix road-test, build 202609151108). This module computes a REAL
number: the truck's live Geotab position + the next stop's centroid -> Mapbox
driving-traffic Directions -> {eta_min, dist_mi, arrive_est} (est-arrival in
America/Los_Angeles). It NEVER fabricates: no token / no GPS / no coords / a Mapbox
timeout (<=4s) / any error -> returns None, and the client shows "—", never a stale
or fake number.

Disguise/Simplicity: eta_min/dist_mi/arrive_est are OPERATIONAL nav facts shown to
coached and nominal drivers IDENTICALLY (exactly like the old drive/arrive slots) —
they ride on the BASE route.nextStop, never inside the coach bundle, and carry no
$/%/count. Coordinates NEVER leave the server; only the human-facing ETA text ships.

Caching: results are cached ~20s per (truck, stop) so a truck that polls every ~20s
makes at most one Mapbox call per poll (and repeated route resolves within a poll
window reuse the same answer). READ-ONLY: this module touches no database.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import datetime
import json
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

from .config import mapbox_token

# Mapbox driving-traffic Directions. overview=false + annotations keeps the payload
# tiny (we only want the single route's duration+distance, not geometry).
_MAPBOX_URL = (
    "https://api.mapbox.com/directions/v5/mapbox/driving-traffic/"
    "{lng1},{lat1};{lng2},{lat2}"
)
# Same driving-traffic profile, but asking for the FULL geojson GEOMETRY — the
# road-following "snaking breadcrumb" LineString the live nav map draws (LIVE MAP
# brief §BACKEND). Multi-waypoint: truck through the next few stops.
_MAPBOX_LINE_URL = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic/{coords}"
_TIMEOUT_S = 4.0          # brief: Mapbox call bounded to <=4s, then graceful null
_CACHE_TTL_S = 20.0       # brief: cache ~20s per (truck, stop)
_METERS_PER_MILE = 1609.344
_PT = "America/Los_Angeles"

# (truck_key, stop_key) -> (expires_monotonic, result_or_None)
_cache: dict[tuple, tuple[float, Optional[dict]]] = {}
# (truck_key, stop_set_key) -> (expires_monotonic, line_or_None) — the route-geometry
# cache, ~20s per (truck, stop-set) exactly like the ETA cache (one Mapbox integration).
_line_cache: dict[tuple, tuple[float, Optional[list]]] = {}


def _pt_tz():
    """America/Los_Angeles tzinfo (zoneinfo on 3.9+, graceful None if unavailable)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(_PT)
    except Exception:  # pragma: no cover - zoneinfo is stdlib on the target runtime
        return None


def _fmt_clock_pt(dt_utc: datetime.datetime) -> str:
    """UTC instant -> a 12h 'H:MM AM/PM' Pacific clock string (no zero-pad on the
    hour). Manual format (not %-I) so it is portable across libc. AM/PM is REQUIRED
    (Felix road-test: '20:30' style 24h times are a field bug — cab-facing times are
    always 12-hour AM/PM, America/Los_Angeles)."""
    tz = _pt_tz()
    local = dt_utc.astimezone(tz) if tz is not None else dt_utc
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {ampm}"


def _default_fetch(
    lat1: float, lng1: float, lat2: float, lng2: float, token: str, timeout: float
) -> Optional[dict]:
    """Call Mapbox driving-traffic and return the parsed JSON, or None on ANY error
    (network, timeout, non-200, bad body). Never raises to the caller."""
    url = _MAPBOX_URL.format(lng1=lng1, lat1=lat1, lng2=lng2, lat2=lat2) + "?" + (
        urllib.parse.urlencode(
            {
                "access_token": token,
                "overview": "false",
                "annotations": "duration,distance",
            }
        )
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec - fixed host
            if getattr(resp, "status", 200) != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def parse_directions(data: Optional[dict], now_utc: datetime.datetime) -> Optional[dict]:
    """Fold a Mapbox Directions response into {eta_min, dist_mi, arrive_est}, or None
    when the response carries no usable route. Pure + timezone-deterministic (tests
    pin `now_utc`)."""
    if not isinstance(data, dict):
        return None
    routes = data.get("routes") or []
    if not routes:
        return None
    route = routes[0] or {}
    duration = route.get("duration")   # seconds (traffic-aware)
    distance = route.get("distance")   # meters
    if not isinstance(duration, (int, float)) or not isinstance(distance, (int, float)):
        return None
    if duration < 0 or distance < 0:
        return None
    eta_min = max(0, round(duration / 60.0))
    dist_mi = round(distance / _METERS_PER_MILE, 1)
    arrive_est = _fmt_clock_pt(now_utc + datetime.timedelta(seconds=duration))
    return {"eta_min": eta_min, "dist_mi": dist_mi, "arrive_est": arrive_est}


def live_eta(
    truck_key,
    stop_key,
    truck_lat: Optional[float],
    truck_lng: Optional[float],
    stop_lat: Optional[float],
    stop_lng: Optional[float],
    *,
    token: Optional[str] = None,
    now_utc: Optional[datetime.datetime] = None,
    fetch: Optional[Callable] = None,
    use_cache: bool = True,
    timeout_s: float = _TIMEOUT_S,
) -> Optional[dict]:
    """The traffic-aware {eta_min, dist_mi, arrive_est} for a truck heading to a stop,
    or None (graceful) on any missing input / Mapbox failure.

    Graceful-null conditions (each -> the client shows "—", never a fake number):
      - no MAPBOX_TOKEN configured
      - the truck has no GPS fix (truck_lat/lng None) or the stop has no coords
      - Mapbox errors, times out (<= timeout_s), or returns no route

    Cached ~20s per (truck_key, stop_key) so one truck's ~20s poll makes at most one
    Mapbox call. `fetch` is injectable for tests (default hits Mapbox over HTTPS).
    """
    token = token if token is not None else mapbox_token()
    if not token:
        return None
    if None in (truck_lat, truck_lng, stop_lat, stop_lng):
        return None

    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc)
    key = (truck_key, stop_key)
    if use_cache:
        hit = _cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]

    fetch = fetch or _default_fetch
    try:
        data = fetch(truck_lat, truck_lng, stop_lat, stop_lng, token, timeout_s)
    except TypeError:
        # a test fetch may omit the timeout param; call it without it.
        data = fetch(truck_lat, truck_lng, stop_lat, stop_lng, token)
    except Exception:
        data = None

    result = parse_directions(data, now_utc)
    if use_cache:
        _cache[key] = (time.monotonic() + _CACHE_TTL_S, result)
    return result


def clear_cache() -> None:
    """Drop the whole ETA cache (used by tests; harmless in prod)."""
    _cache.clear()
    _line_cache.clear()


# --------------------------------------------------------------------------- #
# Route-line geometry (the live nav map's road-following "snaking breadcrumbs").
# Same Mapbox driving-traffic client + ~20s cache as the ETA — ONE integration.
# --------------------------------------------------------------------------- #
# Mapbox Directions accepts at most 25 waypoints; the truck + a few stops is well
# under that. We only ever pass the truck + the next handful of stops.
_MAX_LINE_WAYPOINTS = 12


def _default_line_fetch(coords: str, token: str, timeout: float) -> Optional[dict]:
    """Call Mapbox driving-traffic for the FULL geojson geometry through `coords`
    ('lng,lat;lng,lat;...'), or None on ANY error. Never raises. `overview=full`
    returns the road-following LineString (the snaking breadcrumbs)."""
    url = _MAPBOX_LINE_URL.format(coords=coords) + "?" + urllib.parse.urlencode(
        {
            "access_token": token,
            "geometries": "geojson",
            "overview": "full",
        }
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec - fixed host
            if getattr(resp, "status", 200) != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def parse_route_line(data: Optional[dict]) -> Optional[list]:
    """Fold a Mapbox Directions (geojson) response into the [[lng,lat], ...] road
    line, or None when the response carries no usable geometry. Pure. The client
    draws this LineString exactly as returned (a GeoJSON coordinate array)."""
    if not isinstance(data, dict):
        return None
    routes = data.get("routes") or []
    if not routes:
        return None
    geom = (routes[0] or {}).get("geometry") or {}
    if geom.get("type") != "LineString":
        return None
    coords = geom.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        return None
    out = []
    for pt in coords:
        if (isinstance(pt, (list, tuple)) and len(pt) >= 2
                and isinstance(pt[0], (int, float)) and isinstance(pt[1], (int, float))):
            out.append([float(pt[0]), float(pt[1])])
    return out if len(out) >= 2 else None


def route_line(
    truck_key,
    waypoints: list,
    *,
    token: Optional[str] = None,
    fetch: Optional[Callable] = None,
    use_cache: bool = True,
    timeout_s: float = _TIMEOUT_S,
) -> Optional[list]:
    """The road-following [[lng,lat], ...] line through `waypoints` (each (lat,lng),
    truck FIRST then the upcoming stops), or None (graceful) on any missing input /
    Mapbox failure.

    Graceful-null conditions (each -> the client omits the line and keeps a simple
    view, never a fake straight line):
      - no MAPBOX_TOKEN configured
      - fewer than two usable waypoints
      - Mapbox errors, times out (<= timeout_s), or returns no geometry

    Cached ~20s per (truck_key, stop-set) so one truck's ~20s poll makes at most one
    Mapbox geometry call. `fetch` is injectable for tests.
    """
    token = token if token is not None else mapbox_token()
    if not token:
        return None
    # Clean the waypoints: keep only real (lat,lng) pairs, cap to Mapbox's limit.
    pts = []
    for wp in waypoints or []:
        if (isinstance(wp, (list, tuple)) and len(wp) >= 2
                and isinstance(wp[0], (int, float)) and isinstance(wp[1], (int, float))):
            pts.append((float(wp[0]), float(wp[1])))
    pts = pts[:_MAX_LINE_WAYPOINTS]
    if len(pts) < 2:
        return None

    key = (truck_key, tuple(pts))
    if use_cache:
        hit = _line_cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]

    # Mapbox path is 'lng,lat;lng,lat;...' (note the axis order flip vs (lat,lng)).
    coords = ";".join(f"{lng},{lat}" for lat, lng in pts)
    fetch = fetch or _default_line_fetch
    try:
        data = fetch(coords, token, timeout_s)
    except TypeError:
        data = fetch(coords, token)
    except Exception:
        data = None

    result = parse_route_line(data)
    if use_cache:
        _line_cache[key] = (time.monotonic() + _CACHE_TTL_S, result)
    return result

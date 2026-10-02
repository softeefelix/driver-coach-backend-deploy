"""Road-following geometry for Driver Coach advised legs.

A confirmed Driver Coach session already has a Master Route cluster and day. Its map
must follow the stored Geotab master trace for that cluster, not recalculate the
shortest road path between planned pins.  The trace is a real neighbourhood sequence:
when the driver is on a leg from block 1 to block 4, blocks 2 and 3 remain on the
blue line even when none of them is a pin.

``advised_leg`` remains a compatibility adapter for callers with no confirmed cluster.
Confirmed live sessions use ``confirmed_cluster_leg`` and deliberately omit the blue
line if Master Route has no usable stored trace; they never substitute a shortcut.
"""
from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from typing import Optional


OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/"
OSRM_MAX_WAYPOINTS = 25
# Master Route is the public, read-only source for a confirmed cluster's stored
# trace. The environment override exists for local/staging tests, not as a gate that
# silently turns production back into pin-to-pin routing.
MASTER_ROUTE_DEFAULT_BASE_URL = "https://master-route-web.onrender.com"
# Conservative display guards, not routing distances. Historical parked-only
# records can jump kilometers; more vertices alone does not make a street snake.
# Dense Geotab history may also have gaps. Omit only the affected leg, never join
# across missing streets or draw navigation far away from the truck/advised stop.
MAX_TRACE_ANCHOR_M = 200.0
MAX_TRACE_STEP_M = 250.0


def _distance_m(a, b) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    h = (math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2)
         * math.sin(math.radians(b[1] - a[1]) / 2) ** 2)
    return 6371000 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _base_url() -> Optional[str]:
    value = os.getenv("MASTER_ROUTE_BASE_URL", MASTER_ROUTE_DEFAULT_BASE_URL).strip().rstrip("/")
    return value or None


def _request(url: str, *, body: Optional[dict] = None, timeout: float = 4.0) -> Optional[dict]:
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        with urllib.request.urlopen(req, timeout=timeout) as response:  # nosec - configured internal base or fixed OSRM host
            if getattr(response, "status", 200) != 200:
                return None
            value = json.loads(response.read().decode())
            return value if isinstance(value, dict) else None
    except Exception:
        return None


def _master_drive_path(base: str, coordinates: list[list[float]]) -> Optional[dict]:
    """Read Master Route's [lat,lng] drive-path polyline and flip it for Mapbox."""
    result = _request(f"{base}/api/drive-path", body={"coordinates": coordinates})
    polyline = result.get("polyline") if isinstance(result, dict) else None
    if not isinstance(polyline, list) or len(polyline) < 2:
        return None
    try:
        line = [[float(point[1]), float(point[0])] for point in polyline]
    except (TypeError, ValueError, IndexError):
        return None
    source = result.get("source") if isinstance(result, dict) else None
    return {"line": line, "source": source or "drive-path"}


def _osrm_segment(coordinates: list[list[float]]) -> Optional[list[list[float]]]:
    """Request one OSRM segment; OSRM geometry already uses [lng,lat]."""
    encoded = ";".join(f"{lng},{lat}" for lat, lng in coordinates)
    url = OSRM_ROUTE_URL + encoded + "?" + urllib.parse.urlencode({
        "overview": "full", "geometries": "geojson", "steps": "false",
    })
    result = _request(url)
    routes = result.get("routes") if isinstance(result, dict) else None
    geometry = (routes[0] or {}).get("geometry") if isinstance(routes, list) and routes else None
    line = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(line, list) or len(line) < 2:
        return None
    try:
        return [[float(point[0]), float(point[1])] for point in line]
    except (TypeError, ValueError, IndexError):
        return None


def _osrm_drive_path(coordinates: list[list[float]]) -> Optional[dict]:
    """Join chunked OSRM legs, sharing each chunk boundary without a straight chord."""
    line: list[list[float]] = []
    start = 0
    while start < len(coordinates) - 1:
        end = min(start + OSRM_MAX_WAYPOINTS, len(coordinates))
        segment = _osrm_segment(coordinates[start:end])
        if not segment:
            return None
        line.extend(segment if not line else segment[1:])
        start = end - 1
    return {"line": line, "source": "osrm"} if len(line) >= 2 else None


def _street_connector(origin: list[float], onto: list[float]) -> Optional[list[list[float]]]:
    """Street path from the truck to the forward point on the stored snake.

    Returns [lat, lng] points, matching the stored trace. None when no street
    path exists — the caller omits the line rather than drawing a chord.
    The public router is slower than the 4-second trace fetch, so this call
    gets its own budget.
    """
    encoded = ";".join(f"{lng},{lat}" for lat, lng in (origin, onto))
    url = OSRM_ROUTE_URL + encoded + "?" + urllib.parse.urlencode({
        "overview": "full", "geometries": "geojson", "steps": "false",
    })
    result = _request(url, timeout=12.0)
    routes = result.get("routes") if isinstance(result, dict) else None
    geometry = (routes[0] or {}).get("geometry") if isinstance(routes, list) and routes else None
    line = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(line, list) or len(line) < 2:
        return None
    try:
        return [[float(point[1]), float(point[0])] for point in line]
    except (TypeError, ValueError, IndexError):
        return None


def _as_lat_lng(point) -> Optional[list[float]]:
    """Validate one Master Route trace coordinate without accepting malformed JSON."""
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return None
    try:
        lat, lng = float(point[0]), float(point[1])
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return [lat, lng]


def _route_trace(base: str, route_cluster_id: int, dow: str) -> Optional[list[list[float]]]:
    """Fetch the stored Geotab master trace for one confirmed cluster and day."""
    query = urllib.parse.urlencode({"routeClusterId": route_cluster_id, "day": dow})
    result = _request(f"{base}/api/route-trace?{query}")
    raw_trace = result.get("trace") if isinstance(result, dict) else None
    if not isinstance(raw_trace, list):
        return None
    trace: list[list[float]] = []
    for point in raw_trace:
        parsed = _as_lat_lng(point)
        if parsed is None:
            return None
        trace.append(parsed)
    return trace if len(trace) >= 2 else None


def _nearest_trace_index(trace: list[list[float]], point: tuple[float, float] | list[float], start: int = 0) -> Optional[int]:
    """Nearest breadcrumb index, in trace order. Squared degrees is sufficient for
    selecting a local point from one route trace and avoids another routing lookup."""
    try:
        lat, lng = float(point[0]), float(point[1])
    except (TypeError, ValueError, IndexError):
        return None
    if start < 0 or start >= len(trace):
        return None
    return min(
        range(start, len(trace)),
        key=lambda index: (trace[index][0] - lat) ** 2 + (trace[index][1] - lng) ** 2,
    )


def confirmed_cluster_leg(
    route_cluster_id: int,
    dow: str,
    truck: tuple[float, float],
    next_stop: tuple[float, float],
) -> Optional[dict]:
    """Return the confirmed Master Route trace section for the leg in progress.

    The driver's current position and advised stop are projected onto the *ordered*
    Geotab master trace.  Keeping every breadcrumb between those anchors is what makes
    a neighbourhood snake visit the unpinned intermediate streets.  If that ordered
    segment cannot be proven, return ``None`` rather than drawing a shortest-path or
    compass-style substitute.
    """
    base = _base_url()
    if not base or not isinstance(route_cluster_id, int) or route_cluster_id < 1 or not isinstance(dow, str) or not dow:
        return None
    truck_point, stop_point = _as_lat_lng(truck), _as_lat_lng(next_stop)
    if truck_point is None or stop_point is None:
        return None
    trace = _route_trace(base, route_cluster_id, dow)
    if trace is None:
        return None
    start = _nearest_trace_index(trace, truck_point)
    # A truck that has not reached the route yet is nearest to some middle
    # point only because that point happens to be closest in a straight line.
    # Starting there puts the first stops behind the truck and the line is
    # dropped. The route ahead begins at its first point.
    if _distance_m(trace[start], truck_point) > MAX_TRACE_ANCHOR_M:
        start = 0
    # The destination must be later in the confirmed direction of travel.  A trace
    # match behind the truck is not this leg and must not be reversed into fake nav.
    end = _nearest_trace_index(trace, next_stop, (start or 0) + 1)
    if start is None or end is None or end <= start:
        return None
    if _distance_m(trace[end], stop_point) > MAX_TRACE_ANCHOR_M:
        return None
    # The line the driver follows starts at the truck and runs forward only.
    # Projecting the truck onto the nearest stored point draws the path behind
    # them, or starts it kilometers away when the last fix is off the route.
    # A truck on the route keeps the stored snake. A truck off it gets the
    # street connector from where they are to the forward point, then the snake.
    leg = trace[start:end + 1]
    if any(_distance_m(a, b) > MAX_TRACE_STEP_M for a, b in zip(leg, leg[1:])):
        return None
    if _distance_m(trace[start], truck_point) > 40:
        connector = _street_connector(truck_point, trace[start])
        if not connector:
            return None
        leg = connector[:-1] + leg
    if len(leg) < 2 or len({tuple(p) for p in leg}) < 2:
        return None
    return {
        "line": [[lng, lat] for lat, lng in leg],
        "source": "master-route-trace",
        # Internal only: routes.py passes these geometry-derived anchors to the
        # directions step resolver; payload.py projects this key off the wire.
        "turn_waypoints": leg,
    }


def advised_leg(waypoints: list[tuple[float, float]]) -> Optional[dict]:
    """Compatibility fallback for callers without a confirmed Master Route cluster."""
    if len(waypoints) < 2:
        return None
    try:
        coordinates = [[float(lat), float(lng)] for lat, lng in waypoints]
    except (TypeError, ValueError):
        return None
    base = _base_url()
    return _master_drive_path(base, coordinates) if base else _osrm_drive_path(coordinates)

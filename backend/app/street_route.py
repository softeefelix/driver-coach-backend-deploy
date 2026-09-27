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


def _nearest_trace_index(trace: list[list[float]], point: tuple[float, float], start: int = 0) -> Optional[int]:
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
    trace = _route_trace(base, route_cluster_id, dow)
    if trace is None:
        return None
    start = _nearest_trace_index(trace, truck)
    # The destination must be later in the confirmed direction of travel.  A trace
    # match behind the truck is not this leg and must not be reversed into fake nav.
    end = _nearest_trace_index(trace, next_stop, (start or 0) + 1)
    if start is None or end is None or end <= start:
        return None
    leg = trace[start:end + 1]
    if len(leg) < 2:
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

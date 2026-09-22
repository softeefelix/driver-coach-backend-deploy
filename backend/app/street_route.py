"""Road-following geometry for Driver Coach advised legs.

When Master Route is configured, its ``/api/drive-path`` owns the street path. When
it is not, this adapter calls OSRM directly in the same chunked form. A stored Master
Route trace is deliberately not queried here: it needs a route-cluster id and day and
is a whole-route breadcrumb, not a pin-to-pin advised leg.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Optional


OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/"
OSRM_MAX_WAYPOINTS = 25


def _base_url() -> Optional[str]:
    value = os.getenv("MASTER_ROUTE_BASE_URL", "").strip().rstrip("/")
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


def advised_leg(waypoints: list[tuple[float, float]]) -> Optional[dict]:
    """Return a road-following ``{line, source}``, never a fabricated straight chord."""
    if len(waypoints) < 2:
        return None
    try:
        coordinates = [[float(lat), float(lng)] for lat, lng in waypoints]
    except (TypeError, ValueError):
        return None
    base = _base_url()
    return _master_drive_path(base, coordinates) if base else _osrm_drive_path(coordinates)

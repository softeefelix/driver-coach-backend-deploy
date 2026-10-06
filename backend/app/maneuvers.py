"""Maneuvers follow the stored blue line, not a pin-to-pin shortcut.

A confirmed leg already has a sliced Master Route trace. Sampling that trace
every ~25 points and asking Mapbox driving for steps is what makes the spoken
streets match the drawn line. A route with no stored trace has no waypoints,
so it gets no maneuvers — never a straight line between pins.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Callable, Optional

MAPBOX_DRIVING_URL = "https://api.mapbox.com/directions/v5/mapbox/driving/"
# Mapbox driving accepts 25 coordinates. The truck, samples along the line,
# and the advised stop must all fit in one request.
MAPBOX_MAX_WAYPOINTS = 25
# The card's verified spacing: one waypoint about every 25 stored points.
TRACE_SAMPLE_EVERY = 25
NEXT_MANEUVERS = 3
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
            out.append({
                "instruction": instruction,
                "distance_m": round(float(distance)),
                "street": step.get("name") if isinstance(step.get("name"), str) else "",
                "modifier": modifier if isinstance(modifier, str) else "",
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


def maneuvers_along_line(
    line: Optional[list],
    *,
    token: Optional[str],
    fetch: Optional[Callable] = None,
    timeout_s: float = _TIMEOUT_S,
    limit: int = NEXT_MANEUVERS,
) -> list[dict]:
    """Next named maneuvers that track `line`. No line means no maneuvers."""
    if not token or not line:
        return []
    waypoints = sample_trace_waypoints(line)
    if len(waypoints) < 2:
        return []
    coords = ";".join(f"{lng},{lat}" for lat, lng in waypoints)
    try:
        data = (fetch or _default_fetch)(coords, token, timeout_s)
    except Exception:
        return []
    return named_maneuvers(parse_driving_steps(data), limit=limit)

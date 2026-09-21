"""Jobber driver-of-record, name mapping, and daily area resolution.

The Driver Coach never infers a driver's route from a truck's GPS location.  Jobber's
assigned daily visit property city is the operational assignment.  The events module
already owns the read-only, token-single-refresher Jobber query, so this module uses
that exact source rather than creating another OAuth/token path.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from collections import Counter
from functools import lru_cache
from typing import Optional

_DEFAULT_MAP_PATH = os.path.expanduser(
    "~/projects/work/MisterSoftee/driver-performance/jobber_name_map.json"
)
_NAME_MAP_ENV = "DRIVER_COACH_NAME_MAP"


def _map_path() -> str:
    env_path = (os.environ.get(_NAME_MAP_ENV) or "").strip()
    return os.path.expanduser(env_path) if env_path else _DEFAULT_MAP_PATH


_MAP_PATH = _DEFAULT_MAP_PATH


@lru_cache(maxsize=8)
def _load_map(path: str) -> dict:
    with open(path) as fh:
        d = json.load(fh)
    return d.get("map", d)


def resolve_square_name(picked_name: str, path: Optional[str] = None) -> str:
    m = _load_map(path or _map_path())
    if picked_name in m and m[picked_name]:
        return m[picked_name]
    low = picked_name.strip().lower()
    for k, v in m.items():
        if k.strip().lower() == low and v:
            return v
    return picked_name


def driver_id_for(square_name: str) -> str:
    slug = re.sub(r"\s+", "_", square_name.strip().lower())
    return f"jobber:{slug}"


def name_from_driver_id(driver_id: str) -> str:
    if not driver_id or not str(driver_id).startswith("jobber:"):
        return ""
    return str(driver_id)[len("jobber:"):].replace("_", " ").strip()


def resolve_driver_of_record(picked_name: str, path: Optional[str] = None) -> dict:
    square = resolve_square_name(picked_name, path=path or _map_path())
    return {"picked_name": picked_name, "square_name": square, "driver_id": driver_id_for(square)}


def _norm(name: Optional[str]) -> str:
    return " ".join((name or "").split()).lower()


def driver_area_for_day(driver_canonical: str, day: datetime.date) -> Optional[str]:
    """Return the uniquely assigned Jobber city for the driver's Pacific day.

    Jobber Visit exposes both `assignedUsers` and `property.address.city` (verified
    against the live GraphQL schema on 2026-09-18).  Prefer recurring visits because
    those are the route assignment; a one-off city is used only if it is the driver's
    sole city that day.  Multiple cities are ambiguous and deliberately return None
    so the driver is shown a picker instead of being silently routed incorrectly.
    """
    from . import events  # lazy: events imports this module for name normalization

    nodes = events._fetch_visits(day)
    if not isinstance(nodes, list):
        return None
    recurring: Counter[str] = Counter()
    all_cities: Counter[str] = Counter()
    for visit in nodes:
        if not isinstance(visit, dict):
            continue
        assigned = [
            (user.get("name") or {}).get("full")
            for user in ((visit.get("assignedUsers") or {}).get("nodes") or [])
            if isinstance(user, dict)
        ]
        if not events._driver_matches(driver_canonical, [n for n in assigned if n]):
            continue
        city = ((visit.get("property") or {}).get("address") or {}).get("city")
        city = " ".join(str(city or "").split())
        if not city:
            continue
        all_cities[city] += 1
        if (visit.get("job") or {}).get("jobType") == "RECURRING":
            recurring[city] += 1
    pool = recurring or all_cities
    if len(pool) != 1:
        return None
    return next(iter(pool))

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


def _city_from_dispatch_title(title: str) -> Optional[str]:
    """A Jobber task title is the cruise sheet. 'South SF - Sign Hill' is a city.

    Truck labels ('T7', 'Softee 7') are not cities. The first segment before
    ' - ' is the area the dispatcher wrote.
    """
    text = " ".join(str(title or "").split())
    if not text or re.fullmatch(r"(?:t|softee|truck)\s*\d+", text, flags=re.I):
        return None
    head = text.split(" - ", 1)[0].strip()
    aliases = {"ssf": "South San Francisco", "south sf": "South San Francisco"}
    return aliases.get(head.casefold(), head) or None


def dispatch_area_for_day(driver_canonical: str, day: datetime.date) -> Optional[str]:
    """City from today's Jobber task title. None if the sheet was not readable.

    A truck-only task ('T7') is not a city. Do not fall through to sales history.
    """
    from . import events
    titles = events.dispatch_titles_for_driver(driver_canonical, day)
    if titles is None:
        return None
    for title in titles:
        city = _city_from_dispatch_title(title)
        if city:
            return city
    return None


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


# Jobber province is a full state name or a postal abbreviation. Visit addresses on
# the sign-in payload use the abbreviation ("CA"), matching a street mailing line.
_US_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}
_STATE_ABBRS = set(_US_STATE_ABBR.values())

# Dispatch sheets abbreviate the peninsula. Visit cities and route cities share a key.
_CITY_ALIASES = {
    "ssf": "southsanfrancisco",
    "southsf": "southsanfrancisco",
    "southsanfran": "southsanfrancisco",
}
_CITY_ALIAS_DISPLAY = {
    "southsanfrancisco": "South San Francisco",
}

_TRUCK_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:truck|softee|t)\s*#?\s*-?\s*(\d+)(?!\d)",
    re.IGNORECASE,
)


def province_abbrev(value: Optional[str]) -> Optional[str]:
    """Jobber `address.province` -> postal abbreviation when it is a US state."""
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if re.fullmatch(r"[A-Za-z]{2}", text):
        return text.upper()
    return _US_STATE_ABBR.get(text.casefold(), text)


def city_key(city: Optional[str]) -> str:
    """Comparable city key. 'South SF' and 'South San Francisco' share one key."""
    key = re.sub(r"[^a-z0-9]", "", (city or "").casefold())
    return _CITY_ALIASES.get(key, key)


def expand_city_alias(city: Optional[str]) -> Optional[str]:
    """Display name for a dispatch abbreviation, or None when `city` is already plain."""
    return _CITY_ALIAS_DISPLAY.get(city_key(city))


def _truck_from_text(text: Optional[str], *, bare_number: bool = False) -> Optional[int]:
    """A fleet truck number written as T13, Softee 13, or Truck #13.

    A bare number is accepted only for a vehicle name (`"13"`). Titles are not
    scanned that way, so a year or a street number cannot become a truck.
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    if bare_number and re.fullmatch(r"\d{1,3}", raw):
        number = int(raw)
        return number if number > 0 else None
    match = _TRUCK_RE.search(raw)
    if not match:
        return None
    number = int(match.group(1))
    return number if 0 < number < 1000 else None


def _visit_truck(visit: dict) -> Optional[int]:
    """Truck painted on the visit: assigned vehicle, then title, job title, user name."""
    users = ((visit.get("assignedUsers") or {}).get("nodes") or [])
    for user in users:
        if not isinstance(user, dict):
            continue
        vehicle = user.get("assignedVehicle")
        name = vehicle.get("name") if isinstance(vehicle, dict) else None
        truck = _truck_from_text(name, bare_number=True)
        if truck is not None:
            return truck
    for text in (visit.get("title"), (visit.get("job") or {}).get("title")):
        truck = _truck_from_text(text)
        if truck is not None:
            return truck
    for user in users:
        if not isinstance(user, dict):
            continue
        truck = _truck_from_text((user.get("name") or {}).get("full"))
        if truck is not None:
            return truck
    return None


def _visit_assigned_names(visit: dict) -> list[str]:
    names = []
    for user in ((visit.get("assignedUsers") or {}).get("nodes") or []):
        if not isinstance(user, dict):
            continue
        full = (user.get("name") or {}).get("full")
        if full:
            names.append(full)
    return names


def _one_driver_match(want: str, name: str) -> bool:
    """Match one assignee. A broken name map must not hide a later exact match."""
    from . import events

    try:
        return events._same_driver(want, name)
    except Exception:
        return events._norm(name) == events._norm(want)


def _driver_is_assigned(driver_canonical: str, names: list[str]) -> bool:
    """True when this sign-in is one of the visit's Jobber assignees.

    Uses the same Jobber<->Square match as the cruise-area resolver. A missing name
    map or a matcher error is 'not this driver', never a failed sign-in.
    """
    if any(_one_driver_match(driver_canonical, name) for name in names):
        return True
    try:
        square = resolve_square_name(driver_canonical)
    except Exception:
        return False
    if not square or _norm(square) == _norm(driver_canonical):
        return False
    return any(_one_driver_match(square, name) for name in names)


def _visit_location(visit: dict) -> tuple[Optional[str], Optional[str]]:
    """`(city, mailing address)` from property.address. Either may be missing."""
    addr = (visit.get("property") or {}).get("address") or {}
    if not isinstance(addr, dict):
        return None, None
    street = " ".join(str(addr.get("street") or addr.get("street1") or "").split()) or None
    city = " ".join(str(addr.get("city") or "").split()) or None
    province = province_abbrev(addr.get("province"))
    parts = [part for part in (street, city, province) if part]
    return city, (", ".join(parts) or None)


def format_assignment_visits(nodes: list, driver_canonical: str, truck: int) -> list[dict]:
    """Today's Jobber visits in the sign-in shape.

    Each row is `{city, address, truck, assigned}`. `assigned` is true when the
    visit's users include the signing-in driver or its truck number is the truck
    they signed in on. Visits with no city and no address are omitted: the route
    picker cannot match them. Never raises.
    """
    try:
        sign_in_truck = int(truck)
    except (TypeError, ValueError):
        sign_in_truck = None
    rows: list[dict] = []
    for visit in nodes or []:
        if not isinstance(visit, dict):
            continue
        try:
            city, address = _visit_location(visit)
            if not city and not address:
                continue
            visit_truck = _visit_truck(visit)
            assigned = _driver_is_assigned(driver_canonical, _visit_assigned_names(visit))
            if not assigned and visit_truck is not None and visit_truck == sign_in_truck:
                assigned = True
            rows.append({
                "city": city,
                "address": address,
                "truck": visit_truck,
                "assigned": assigned,
            })
        except Exception:
            continue
    return rows


def today_assignment_visits(
    driver_canonical: str, truck: int, day: datetime.date
) -> list[dict]:
    """Today's visits for the sign-in assignment. `[]` on an empty day OR any Jobber error.

    Never raises. Sign-in must stay usable when the token, Postgres, or Jobber is down.
    """
    try:
        from . import events

        nodes = events._fetch_visits(day)
        if not nodes:
            return []
        return format_assignment_visits(nodes, driver_canonical, truck)
    except Exception:
        return []

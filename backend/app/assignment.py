"""Resolve a driver's Jobber area to a Master Route without GPS guessing.

The selection order is locked: an active/live Geotab master for the area + weekday +
season wins; otherwise named route candidates for that area/day are offered.  The
resolver never selects by truck position.  Its only fallback is a driver-visible picker.
"""
from __future__ import annotations

import datetime
import os
import re
from collections import Counter
from typing import Optional

import psycopg2

from . import jobber
from .routes import dow_name

_MASTER_ENV = os.path.expanduser("~/.hermes/secrets/master-route.env")

# Sign-in picker floor. Of 220 named route-days with a timetable, 48 have 1–4
# stops and the rest center in the low twenties (median 21). Five is the same
# real-day floor route selection already uses, so a stub cannot appear as a
# choosable Master Route. MIN_ROUTE_STOPS overrides it; 0 disables the filter.
# The proposed route for this driver is never removed.
DEFAULT_MIN_ROUTE_STOPS = 5


def _master_database_url() -> str:
    url = os.environ.get("MASTER_ROUTE_DATABASE_URL")
    if url:
        return url.strip().strip("'\"")
    try:
        with open(_MASTER_ENV) as source:
            for line in source:
                key, sep, value = line.partition("=")
                if sep and key.strip() == "DATABASE_URL":
                    url = value.strip().strip("'\"")
                    if "sslmode=" not in url:
                        url += "&sslmode=require" if "?" in url else "?sslmode=require"
                    return url
    except (FileNotFoundError, IOError):
        pass
    # Fallback: use the main DATABASE_URL (same shared softeedatabase)
    url = os.environ.get("DATABASE_URL")
    if url:
        return url.strip().strip("'\"")
    raise RuntimeError("MASTER_ROUTE_DATABASE_URL and DATABASE_URL are not configured")


def _master_connection():
    """Open the independent Master Route database in Postgres read-only mode."""
    conn = psycopg2.connect(_master_database_url(), connect_timeout=4)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _season_variant(day: datetime.date) -> str:
    """Match Master Route's June 8–August 14 no-school convention exactly."""
    # This is the established web discriminator. Keep it operational and never expose
    # it as a driver control; approximate month rules select the wrong live master at
    # the June/August boundaries.
    month_day = (day.month, day.day)
    return "no-school" if (6, 8) <= month_day <= (8, 14) else "school"


def _norm(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


_ROUTE_DAY_PREFIXES = {
    "mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday",
    "thu", "thur", "thurs", "thursday", "fri", "friday", "sat", "saturday",
    "sun", "sunday",
}


def _route_name_without_day_prefix(name: str) -> str:
    """Remove only an explicit `Tue/`-style route-name prefix, never a city segment."""
    prefix, slash, rest = str(name or "").partition("/")
    return rest if slash and _norm(prefix) in _ROUTE_DAY_PREFIXES else str(name or "")


def _name_for_city(name: str, city: str) -> bool:
    """Match the leading city segment, not arbitrary text within a route name.

    Route names are `City-Anchor` or `Tue/City-Anchor`.  Anchoring at the city
    segment is essential: `San Francisco` must not select `South San Francisco`.
    Punctuation and spaces within the city are intentionally interchangeable so
    `Redwood City` matches `RedwoodCity-Connecticut`.
    """
    city = str(city or "").strip()
    if not city:
        return False
    city_pattern = "".join(
        re.escape(char) if char.isalnum() else r"[^a-z0-9]*"
        for char in city.lower()
    )
    return bool(re.match(
        rf"^{city_pattern}(?=[^a-z0-9]|$)",
        _route_name_without_day_prefix(name).lower(),
    ))


def _live_rows(conn, dow: str, season_variant: str) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.route_cluster_id,
                   COALESCE(NULLIF(rn.name, ''), NULLIF(m.geotab_route_name, ''),
                            'Route ' || m.route_cluster_id::text)
            FROM geotab_route_masters m
            LEFT JOIN route_names rn ON rn.route_cluster_id = m.route_cluster_id
            WHERE m.live = TRUE AND m.active = TRUE
              AND lower(m.dow) = lower(%s)
              AND m.season_variant = %s
            ORDER BY m.route_cluster_id
            """,
            (dow, season_variant),
        )
        return [(int(route_id), str(name)) for route_id, name in cur.fetchall()]


def _min_route_stops() -> int:
    """Picker floor from MIN_ROUTE_STOPS. Blank, non-numeric, or negative keeps the default.

    Zero is a real setting: it turns the filter off without a deploy.
    """
    raw = os.environ.get("MIN_ROUTE_STOPS")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_MIN_ROUTE_STOPS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MIN_ROUTE_STOPS
    if value < 0:
        return DEFAULT_MIN_ROUTE_STOPS
    return value


def _as_counted_row(row) -> tuple[int, str, Optional[int]]:
    """`(route_cluster_id, name, timed_stop_count)`. A missing count means unknown."""
    route_id = int(row[0])
    name = str(row[1])
    if len(row) < 3 or row[2] is None:
        return (route_id, name, None)
    try:
        return (route_id, name, int(row[2]))
    except (TypeError, ValueError):
        return (route_id, name, None)


def _all_candidate_rows(conn, dow: str) -> list[tuple[int, str, Optional[int]]]:
    with conn.cursor() as cur:
        # First try: routes with timed stops on this DOW, plus how many stops
        # that day actually has. Read-only; nothing is deleted.
        cur.execute(
            """
            SELECT rn.route_cluster_id, rn.name, count(*) AS stop_count
            FROM route_names rn
            JOIN route_timed_stops rts ON rts.route_cluster_id = rn.route_cluster_id
            WHERE COALESCE(lower(rn.status), '') <> 'retired'
              AND rn.name IS NOT NULL AND rn.name <> ''
              AND lower(rts.dow) = lower(%s)
            GROUP BY rn.route_cluster_id, rn.name
            ORDER BY rn.name, rn.route_cluster_id
            """,
            (dow,),
        )
        rows = cur.fetchall()
        if rows:
            return [_as_counted_row(row) for row in rows]

        # Fallback: ALL non-retired routes if no DOW-specific routes exist.
        # Their stop counts are unknown, so the picker floor does not guess them out.
        cur.execute(
            """
            SELECT DISTINCT rn.route_cluster_id, rn.name
            FROM route_names rn
            WHERE COALESCE(lower(rn.status), '') <> 'retired'
              AND rn.name IS NOT NULL AND rn.name <> ''
            ORDER BY rn.name, rn.route_cluster_id
            """
        )
        return [_as_counted_row(row) for row in cur.fetchall()]


def _index_stop_counts(rows) -> dict[int, Optional[int]]:
    """route_cluster_id -> timed-stop count. None means the count is unknown."""
    counts: dict[int, Optional[int]] = {}
    for row in rows:
        counted = _as_counted_row(row)
        route_id, _name, stop_count = counted
        if route_id not in counts:
            counts[route_id] = stop_count
        elif stop_count is not None and (counts[route_id] is None or stop_count > counts[route_id]):
            counts[route_id] = stop_count
    return counts


def _live_stop_counts(conn, dow: str, season: str) -> dict[int, int]:
    """Stops on the live Geotab master for this day, the equivalent of route_timed_stops.

    A live master is a real route even when its timed-stop rows are sparse. Any
    failure leaves the map empty so sign-in still builds the picker from timed stops.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.route_cluster_id, count(*) AS stop_count
                FROM geotab_route_masters m
                JOIN geotab_route_master_stops s
                  ON s.route_cluster_id = m.route_cluster_id
                 AND lower(s.dow) = lower(m.dow)
                 AND s.season_variant = m.season_variant
                WHERE m.live = TRUE AND m.active = TRUE
                  AND lower(m.dow) = lower(%s)
                  AND m.season_variant = %s
                GROUP BY m.route_cluster_id
                """,
                (dow, season),
            )
            fetched = cur.fetchall()
    except Exception:
        return {}
    counts: dict[int, int] = {}
    for row in fetched:
        if len(row) < 2:
            continue
        try:
            counts[int(row[0])] = int(row[1])
        except (TypeError, ValueError):
            continue
    return counts


def _route_clears_stop_floor(
    route_id: int,
    proposed_id: Optional[int],
    stop_counts: dict[int, Optional[int]],
    geotab_counts: dict[int, int],
    minimum: int,
) -> bool:
    """True when this route stays in the picker.

    The proposed route always stays. A live Geotab master with enough stops stays.
    An unknown timed-stop count stays (fallback list, or a route that was not in
    the timed-stop query). A known count under the floor is hidden.
    """
    if proposed_id is not None and route_id == proposed_id:
        return True
    geotab_count = geotab_counts.get(route_id)
    if geotab_count is not None and geotab_count >= minimum:
        return True
    if route_id not in stop_counts:
        return True
    timed = stop_counts[route_id]
    if timed is None:
        return True
    return timed >= minimum


def _drop_sparse_candidates(
    result: dict,
    stop_counts: dict[int, Optional[int]],
    geotab_counts: dict[int, int],
    minimum: Optional[int] = None,
) -> None:
    """Remove stub routes from both picker lists. Does not touch stored rows."""
    if minimum is None:
        minimum = _min_route_stops()
    proposed = result.get("route_cluster_id")
    proposed_id = int(proposed) if proposed is not None else None
    for key in ("candidates", "all_candidates"):
        kept = []
        for row in result.get(key) or []:
            if not isinstance(row, dict) or row.get("route_cluster_id") is None:
                kept.append(row)
                continue
            route_id = int(row["route_cluster_id"])
            if _route_clears_stop_floor(route_id, proposed_id, stop_counts, geotab_counts, minimum):
                kept.append(row)
        result[key] = kept


def _candidate_objects(rows) -> list[dict]:
    # A later friendly label can be inserted here without changing the confirmation UI.
    seen: set[int] = set()
    objects = []
    for row in rows:
        route_id = int(row[0])
        name = str(row[1])
        if route_id in seen:
            continue
        seen.add(route_id)
        objects.append({"route_cluster_id": route_id, "name": name})
    return objects


def _truck_usual_route(conn, truck: int, dow: str, choices: list[dict]) -> Optional[dict]:
    """The route this truck actually ran most recently on this weekday.

    Jobber often has no cruise city (only a one-off event, or nothing). The
    driver's usual Master Route for this truck+day is then the proposal — not a
    28-route dump. Read-only. A miss returns None and the picker stays open.
    """
    allowed = {c["route_cluster_id"]: c for c in choices}
    if not allowed or not truck:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT route_cluster_id
                FROM active_sale_stops
                WHERE truck_number = %s
                  AND lower(dow) = lower(%s)
                  AND route_cluster_id IS NOT NULL
                GROUP BY route_cluster_id
                ORDER BY max(created_at) DESC, count(*) DESC
                LIMIT 12
                """,
                (int(truck), dow),
            )
            for (route_id,) in cur.fetchall():
                hit = allowed.get(int(route_id))
                if hit:
                    return hit
    except Exception:
        return None
    return None


def _route_matches_city(route_name: str, city: str) -> bool:
    """True when the route name leads with this city, including dispatch abbreviations."""
    if _name_for_city(route_name, city):
        return True
    expanded = jobber.expand_city_alias(city)
    if expanded and expanded.casefold() != str(city).casefold():
        return _name_for_city(route_name, expanded)
    return False


def _clean_city_token(token: str) -> Optional[str]:
    """A comma-segment that is a city, with a trailing state abbreviation removed."""
    words = " ".join(str(token or "").split()).split()
    if not words:
        return None
    if len(words[-1]) == 2 and words[-1].isalpha() and words[-1].upper() in jobber._STATE_ABBRS:
        words = words[:-1]
    city = " ".join(words).strip(" ,")
    if not city or city.upper() in jobber._STATE_ABBRS:
        return None
    return city


def _city_from_stop_address(address: Optional[str]) -> Optional[str]:
    """City from a Master Route address: '<house>, <street>, <city>, …'."""
    parts = [part.strip() for part in str(address or "").split(",") if part.strip()]
    if len(parts) >= 3:
        return _clean_city_token(parts[2])
    if len(parts) == 2:
        from .payload import _looks_like_house_numbers
        if _looks_like_house_numbers(parts[0]):
            return None
        return _clean_city_token(parts[1])
    return None


def _stop_from_address(address: Optional[str]) -> Optional[dict]:
    if not address:
        return None
    from .payload import friendly_stop_name
    name = friendly_stop_name(str(address))
    city = _city_from_stop_address(str(address))
    if not name and not city:
        return None
    return {"name": name, "city": city}


def _route_stops(conn, route_ids: list[int], dow: str) -> dict[int, list[dict]]:
    """Ordered `{name, city}` stops for these routes. `{}` if the table cannot be read."""
    ids = sorted({int(route_id) for route_id in route_ids if route_id is not None})
    if not ids:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT route_cluster_id, stop_order, address
                FROM route_timed_stops
                WHERE route_cluster_id IN %s
                  AND lower(dow) = lower(%s)
                ORDER BY route_cluster_id, stop_order
                """,
                (tuple(ids), dow),
            )
            fetched = cur.fetchall()
    except Exception:
        return {}
    out: dict[int, list[dict]] = {}
    for route_id, _order, address in fetched:
        stop = _stop_from_address(address)
        if stop:
            out.setdefault(int(route_id), []).append(stop)
    return out


def _detail_from_stops(route_name: str, stops: list[dict]) -> dict:
    cities: list[str] = []
    seen: set[str] = set()
    clean: list[dict] = []
    for stop in stops or []:
        if not isinstance(stop, dict):
            continue
        name = stop.get("name")
        city = stop.get("city")
        if not name and not city:
            continue
        clean.append({"name": name, "city": city})
        if city and city not in seen:
            seen.add(city)
            cities.append(city)
    area = None
    for city in cities:
        if _route_matches_city(route_name, city):
            area = city
            break
    if area is None and cities:
        counts = Counter(cities)
        area = max(cities, key=lambda city: (counts[city], -cities.index(city)))
    return {"area": area, "cities": cities, "stops": clean}


def _match_score(route_name: str, detail: dict, visits: list) -> Optional[int]:
    """Weighted count of today's visit cities this route covers.

    An assigned visit counts 2, any other visit counts 1. None when no visit has a
    city, so the client can tell 'not scored' from a real zero.
    """
    weighted: list[tuple[str, int]] = []
    for visit in visits or []:
        if not isinstance(visit, dict):
            continue
        city = visit.get("city")
        if not city:
            continue
        weighted.append((str(city), 2 if visit.get("assigned") else 1))
    if not weighted:
        return None
    keys = {jobber.city_key(city) for city in (detail.get("cities") or []) if city}
    if detail.get("area"):
        keys.add(jobber.city_key(detail["area"]))
    score = 0
    for city, weight in weighted:
        if jobber.city_key(city) in keys or _route_matches_city(route_name, city):
            score += weight
    return score


def _route_ids(result: dict) -> list[int]:
    ids: list[int] = []
    for key in ("candidates", "all_candidates"):
        for row in result.get(key) or []:
            if isinstance(row, dict) and row.get("route_cluster_id") is not None:
                ids.append(int(row["route_cluster_id"]))
    return ids


def _annotate_candidates(result: dict, stops_by_route: dict[int, list[dict]]) -> None:
    """Add optional area/cities/stops and match_score/likely. Existing keys stay put.

    `likely` is set only on the best 3–4 routes with a positive score. Order is
    unchanged: the iPad sorts. A missing stop table simply omits area/cities/stops;
    the route name can still score against visit cities.
    """
    grouped: dict[int, list[dict]] = {}
    for key in ("candidates", "all_candidates"):
        for row in result.get(key) or []:
            if isinstance(row, dict) and row.get("route_cluster_id") is not None:
                grouped.setdefault(int(row["route_cluster_id"]), []).append(row)
    visits = result.get("jobber_visits") or []
    ranked: list[tuple[int, int, list[dict]]] = []
    for route_id, rows in grouped.items():
        name = str(rows[0].get("name") or "")
        detail = _detail_from_stops(name, stops_by_route.get(route_id) or [])
        score = _match_score(name, detail, visits)
        for row in rows:
            if detail.get("area"):
                row["area"] = detail["area"]
            if detail.get("cities"):
                row["cities"] = list(detail["cities"])
            if detail.get("stops"):
                row["stops"] = [dict(stop) for stop in detail["stops"]]
            if score is not None:
                row["match_score"] = score
        if score:
            ranked.append((score, route_id, rows))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    for _score, _route_id, rows in ranked[:4]:
        for row in rows:
            row["likely"] = True


def resolve_assignment(driver_canonical: str, truck: int, day: datetime.date) -> dict:
    """Return a proposal plus every picker-safe candidate; never raise to sign-in.

    Jobber's assigned area still chooses the proposal. `truck` is not a route guess:
    it only marks which of today's visits belong to the signing-in truck. The visit
    list is `[]` when Jobber returns nothing or errors.
    """
    dow = dow_name(day)
    season = _season_variant(day)
    area = None
    stops_by_route: dict[int, list[dict]] = {}
    stop_counts: dict[int, Optional[int]] = {}
    geotab_counts: dict[int, int] = {}
    try:
        visits = jobber.today_assignment_visits(driver_canonical, truck, day)
    except Exception:
        visits = []
    try:
        area = jobber.dispatch_area_for_day(driver_canonical, day)
        if not area:
            area = jobber.driver_area_for_day(driver_canonical, day)
        conn = _master_connection()
        try:
            # Always return the whole day as an explicit escape hatch.  `candidates`
            # stays area-scoped for the first picker paint; `all_candidates` is used
            # only after the driver asks to search outside that area.
            all_rows = _all_candidate_rows(conn, dow)
            stop_counts = _index_stop_counts(all_rows)
            geotab_counts = _live_stop_counts(conn, dow, season)
            all_choices = _candidate_objects(all_rows)
            live = [(i, n) for i, n in _live_rows(conn, dow, season) if _name_for_city(n, area)] if area else []
            if len(live) == 1:
                route_id, name = live[0]
                result = {"route_cluster_id": route_id, "route_name": name, "source": "live",
                          "season_variant": season, "confidence": "high", "area": area,
                          "dow": dow, "candidates": _candidate_objects(live),
                          "all_candidates": all_choices}
            else:
                named_for_city = [row for row in all_rows if area and _name_for_city(row[1], area)]
                candidates = named_for_city if area else list(all_rows)
                # A recognized city may still have no named route for this day.  Do not
                # strand its driver in an empty picker: fall back to every active route
                # available today, just as we do when Jobber has no city at all.
                if area and not candidates:
                    candidates = list(all_rows)
                choices = _candidate_objects(candidates)
                city_matched = bool(area) and bool(named_for_city or live)
                # A lone candidate is a proposal only when Jobber supplied an area. With
                # no area, even one visible row is not evidence that it is this driver's
                # route; remain in the explicit picker path rather than guessing.
                if area and len(choices) == 1 and city_matched:
                    only = choices[0]
                    result = {"route_cluster_id": only["route_cluster_id"], "route_name": only["name"],
                              "source": "candidate", "season_variant": season, "confidence": "medium",
                              "area": area, "dow": dow, "candidates": choices,
                              "all_candidates": all_choices}
                else:
                    # No Jobber city (the visit list is events, and the task title did not
                    # name an area) is not evidence of last week's route. Leave the picker
                    # open. Do not propose truck sales history.
                    result = {"route_cluster_id": None, "route_name": None, "source": "candidate",
                              "season_variant": season, "confidence": "picker", "area": area,
                              "dow": dow, "candidates": choices, "all_candidates": all_choices}
            stops_by_route = _route_stops(conn, _route_ids(result), dow)
        finally:
            conn.close()
    except Exception:
        # Sign-in must remain usable during Jobber/MR outages: all-day candidates when
        # possible, otherwise an empty picker state the client can visibly explain.
        try:
            conn = _master_connection()
            try:
                all_rows = _all_candidate_rows(conn, dow)
                stop_counts = _index_stop_counts(all_rows)
                geotab_counts = _live_stop_counts(conn, dow, season)
                choices = _candidate_objects(all_rows)
                stops_by_route = _route_stops(conn, [row["route_cluster_id"] for row in choices], dow)
            finally:
                conn.close()
        except Exception:
            choices = []
            stops_by_route = {}
            stop_counts = {}
            geotab_counts = {}
        result = {"route_cluster_id": None, "route_name": None, "source": "candidate",
                  "season_variant": season, "confidence": "picker", "area": area,
                  "dow": dow, "candidates": choices, "all_candidates": choices}
    if not isinstance(visits, list):
        visits = []
    result["jobber_visits"] = visits
    try:
        _drop_sparse_candidates(result, stop_counts, geotab_counts)
    except Exception:
        pass
    try:
        _annotate_candidates(result, stops_by_route)
    except Exception:
        pass
    return result

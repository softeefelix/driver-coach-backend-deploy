"""Resolve a driver's Jobber area to a Master Route without GPS guessing.

The selection order is locked: an active/live Geotab master for the area + weekday +
season wins; otherwise named route candidates for that area/day are offered.  The
resolver never selects by truck position.  Its only fallback is a driver-visible picker.
"""
from __future__ import annotations

import datetime
import os
import re
from contextlib import contextmanager
from typing import Optional

import psycopg2

from . import jobber
from .routes import dow_name

_MASTER_ENV = os.path.expanduser("~/.hermes/secrets/master-route.env")


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


def _candidate_rows(conn, city: str, dow: str) -> list[tuple[int, str]]:
    rows = _all_candidate_rows(conn, dow)
    return [(route_id, name) for route_id, name in rows if _name_for_city(name, city)]


def _all_candidate_rows(conn, dow: str) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        # First try: routes with timed stops on this DOW
        cur.execute(
            """
            SELECT DISTINCT rn.route_cluster_id, rn.name
            FROM route_names rn
            JOIN route_timed_stops rts ON rts.route_cluster_id = rn.route_cluster_id
            WHERE COALESCE(lower(rn.status), '') <> 'retired'
              AND rn.name IS NOT NULL AND rn.name <> ''
              AND lower(rts.dow) = lower(%s)
            ORDER BY rn.name, rn.route_cluster_id
            """,
            (dow,),
        )
        rows = cur.fetchall()
        if rows:
            return [(int(route_id), str(name)) for route_id, name in rows]
        
        # Fallback: ALL non-retired routes if no DOW-specific routes exist
        cur.execute(
            """
            SELECT DISTINCT rn.route_cluster_id, rn.name
            FROM route_names rn
            WHERE COALESCE(lower(rn.status), '') <> 'retired'
              AND rn.name IS NOT NULL AND rn.name <> ''
            ORDER BY rn.name, rn.route_cluster_id
            """
        )
        return [(int(route_id), str(name)) for route_id, name in cur.fetchall()]


def _candidate_objects(rows: list[tuple[int, str]]) -> list[dict]:
    # A later friendly label can be inserted here without changing the confirmation UI.
    seen: set[int] = set()
    return [
        {"route_cluster_id": route_id, "name": name}
        for route_id, name in rows
        if not (route_id in seen or seen.add(route_id))
    ]


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


def resolve_assignment(driver_canonical: str, truck: int, day: datetime.date) -> dict:
    """Return a proposal plus every picker-safe candidate; never raise to sign-in.

    `truck` is accepted for the public seam but intentionally unused: drivers and
    trucks are fungible; Jobber's assigned area is authoritative.
    """
    dow = dow_name(day)
    season = _season_variant(day)
    area = None
    try:
        area = jobber.driver_area_for_day(driver_canonical, day)
        conn = _master_connection()
        try:
            # Always return the whole day as an explicit escape hatch.  `candidates`
            # stays area-scoped for the first picker paint; `all_candidates` is used
            # only after the driver asks to search outside that area.
            all_choices = _candidate_objects(_all_candidate_rows(conn, dow))
            live = [(i, n) for i, n in _live_rows(conn, dow, season) if _name_for_city(n, area)] if area else []
            if len(live) == 1:
                route_id, name = live[0]
                return {"route_cluster_id": route_id, "route_name": name, "source": "live",
                        "season_variant": season, "confidence": "high", "area": area,
                        "dow": dow, "candidates": _candidate_objects(live),
                        "all_candidates": all_choices}
            candidates = _candidate_rows(conn, area, dow) if area else _all_candidate_rows(conn, dow)
            # A recognized city may still have no named route for this day.  Do not
            # strand its driver in an empty picker: fall back to every active route
            # available today, just as we do when Jobber has no city at all.
            if area and not candidates:
                candidates = _all_candidate_rows(conn, dow)
            choices = _candidate_objects(candidates)
            city_matched = bool(area) and bool(_candidate_rows(conn, area, dow) or (
                [(i, n) for i, n in _live_rows(conn, dow, season) if _name_for_city(n, area)]
            ))
            # A lone candidate is a proposal only when Jobber supplied an area. With
            # no area, even one visible row is not evidence that it is this driver's
            # route; remain in the explicit picker path rather than guessing.
            if area and len(choices) == 1 and city_matched:
                only = choices[0]
                return {"route_cluster_id": only["route_cluster_id"], "route_name": only["name"],
                        "source": "candidate", "season_variant": season, "confidence": "medium",
                        "area": area, "dow": dow, "candidates": choices,
                        "all_candidates": all_choices}
            # No matching cruise route (Jobber has no city, or only an event city like
            # Stanford with no Master Route of that name) must not dump every Monday
            # route as if they were equal. Propose the route this truck last ran on
            # this weekday. The driver still confirms; "Not my route" is the escape.
            if not city_matched:
                usual = _truck_usual_route(conn, truck, dow, all_choices)
                if usual:
                    return {"route_cluster_id": usual["route_cluster_id"],
                            "route_name": usual["name"], "source": "truck_history",
                            "season_variant": season, "confidence": "medium",
                            "area": area, "dow": dow, "candidates": [usual],
                            "all_candidates": all_choices}
            return {"route_cluster_id": None, "route_name": None, "source": "candidate",
                    "season_variant": season, "confidence": "picker", "area": area,
                    "dow": dow, "candidates": choices, "all_candidates": all_choices}
        finally:
            conn.close()
    except Exception:
        # Sign-in must remain usable during Jobber/MR outages: all-day candidates when
        # possible, otherwise an empty picker state the client can visibly explain.
        try:
            conn = _master_connection()
            try:
                choices = _candidate_objects(_all_candidate_rows(conn, dow))
            finally:
                conn.close()
        except Exception:
            choices = []
        return {"route_cluster_id": None, "route_name": None, "source": "candidate",
                "season_variant": season, "confidence": "picker", "area": area,
                "dow": dow, "candidates": choices, "all_candidates": choices}

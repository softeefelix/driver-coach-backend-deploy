"""Truck -> today's route -> next planned stop (with coordinates).

Real data chain (all public schema, READ-ONLY):
  trucks(truck_number, truck_id)                          truck identity
  active_sale_stops(truck_number, dow, route_cluster_id)  which route a truck ran (recent)
  route_timed_stops(route_cluster_id, dow, stop_order,    the ordered planned stops for a
      stop_cluster_id, arrive, leave_by, address)         route-day (the plan the app shows)
  stop_clusters(stop_cluster_id, centroid_lat,            stop coordinates for motion distance
      centroid_long, address)

"Next stop" for a fresh sign-in is the FIRST planned stop of the truck's route for
today's day-of-week (the route the truck most recently ran on that dow). The
motion classifier's distance-to-next-stop is computed against that stop's
centroid. As the shift advances, the heartbeat's stop_ctx.stop_id lets the wiring
pick the next unvisited stop_order; v1 seeds with stop_order = 1.

This is READ-ONLY against public — no writes, no DDL. The Felix gate only forbids
WRITES to public; reading the shared route tables is exactly the intended wiring.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import datetime
from typing import Optional

from . import grade as _grade
from .payload import friendly_stop_name

_DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Under-visited gem definition, mirrored from master-route/research/undervisited_gems.py
# so the cab and MR agree on "great but we don't go often" (INTEGRATION_MASTER_ROUTE §2.2).
#   - UNDER-visited: route_timed_stops.visits <= GEM_MAX_VISITS
#   - strong rate:   exp_per_visit (the shrunk $/visit expectation) >= GEM_RATE_MULT x global mean
# Every stop meeting BOTH is a gem, and every gem grades 2 (cultivate, don't camp).
GEM_MAX_VISITS = 8
GEM_RATE_MULT = 1.5

# How many upcoming stops the live route feed ships for the "Up next" list. The
# client paints these REAL forward stops (or hides the list) — never a fake queue.
UP_NEXT_MAX = 4

# The driving line goes truck -> the NEXT stop only. Extra pins ahead made the
# screen look like a 5-stop list and hid the turn to the stop they are on.
MAP_LINE_STOPS = 1


def _is_school_address(address: object) -> bool:
    """Identify timetable school stops without assuming a nonexistent DB column."""
    return isinstance(address, str) and ("school" in address.lower() or "elementary" in address.lower())

# Position-first route selection floor (Felix road-test, Emery/13 South SF): among a
# truck's DOW candidate clusters we pick the one NEAREST the live truck position, but
# only a cluster carrying at least this many ORDERED TIMED STOPS may win on nearness.
# This guards the earlier "1 / 1" fix — a near-empty (e.g. 1-stop) cluster that happens
# to be close can never beat a real day. If NO candidate clears the floor, or there is
# no live position at all, we fall back to the fullest-day pick (prior behavior).
MIN_TIMED_STOPS_FOR_POSITION = 5


def dow_name(d: Optional[datetime.date] = None) -> str:
    return _DOW[(d or datetime.date.today()).weekday()]


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters — the same core the motion wiring uses. Lazy
    import keeps route selection decoupled from geotab's import order (both READ-ONLY)."""
    from . import geotab
    return geotab.haversine_m(lat1, lng1, lat2, lng2)


def read_truck_position(cur, truck_no: int, truck_id: Optional[str] = None) -> Optional[dict]:
    """The truck's CURRENT position: LIVE Geotab (DeviceStatusInfo) first, then the
    stale log_records fallback. Returns {lat,lng,...} or None.

    PRIMARY = live (moving-or-parked, @ now); FALLBACK = geotab.latest_position
    (log_records, ~16h stale AND parked-only) so the mini's local path still works and
    nothing crashes. Never raises. Reused by route selection, nearest-stop, and ETA so
    all three see the SAME position (Felix road-test, truck 13/Emery South SF).
    """
    from . import geotab, geotab_live

    if truck_id is None:
        truck_id = truck_id_for(cur, truck_no)
    position = None
    if truck_id:
        try:
            position = geotab_live.live_position(truck_no=truck_no, device_id=truck_id, cur=cur)
        except Exception:
            position = None  # belt+braces: live_position swallows errors, but never crash the feed
        if position is None:
            position = geotab.latest_position(cur, truck_id)
    return position


def truck_id_for(cur, truck_no: int) -> Optional[str]:
    cur.execute(
        "SELECT truck_id FROM public.trucks WHERE truck_number = %s", (truck_no,)
    )
    row = cur.fetchone()
    return row[0] if row else None


def _candidate_clusters_for_truck_dow(cur, truck_no: int, dow: str) -> list[dict]:
    """The truck's DOW candidate route-clusters, each with its ordered-timed-stop
    count, recency, active-row volume, AND every timed stop's centroid coordinates.

    One row per candidate cluster:
      {route_cluster_id, timed_stops, last_seen, active_rows, coords:[(lat,lng),...]}
    Only clusters with >=1 ordered timed stop for `dow` are returned (a cluster with
    zero timed stops is never a real day). Ordered fullest-first so the first element
    is the current fullest-day pick. READ-ONLY against public.
    """
    cur.execute(
        """
        SELECT ass.route_cluster_id,
               count(DISTINCT rts.stop_order)  AS timed_stops,
               max(ass.created_at)             AS last_seen,
               count(*)                        AS active_rows
        FROM public.active_sale_stops ass
        LEFT JOIN public.route_timed_stops rts
               ON rts.route_cluster_id = ass.route_cluster_id
              AND lower(rts.dow) = lower(%s)
        WHERE ass.truck_number = %s AND lower(ass.dow) = lower(%s)
              AND ass.route_cluster_id IS NOT NULL
        GROUP BY ass.route_cluster_id
        HAVING count(DISTINCT rts.stop_order) > 0
        ORDER BY timed_stops DESC,
                 last_seen DESC NULLS LAST,
                 active_rows DESC
        """,
        (dow, truck_no, dow),
    )
    candidates = []
    for route_cluster_id, timed_stops, last_seen, active_rows in cur.fetchall():
        candidates.append(
            {
                "route_cluster_id": int(route_cluster_id),
                "timed_stops": int(timed_stops),
                "last_seen": last_seen,
                "active_rows": int(active_rows),
                "coords": [],
            }
        )
    if not candidates:
        return candidates

    # Pull the stop centroids for every candidate cluster in ONE query, then bucket
    # by cluster. Only clusters resolved above are queried (no unrelated routes).
    ids = tuple(c["route_cluster_id"] for c in candidates)
    cur.execute(
        """
        SELECT rts.route_cluster_id, sc.centroid_lat, sc.centroid_long
        FROM public.route_timed_stops rts
        LEFT JOIN public.stop_clusters sc
               ON sc.stop_cluster_id = rts.stop_cluster_id
        WHERE rts.route_cluster_id IN %s AND lower(rts.dow) = lower(%s)
        """,
        (ids, dow),
    )
    by_id = {c["route_cluster_id"]: c for c in candidates}
    for route_cluster_id, lat, lng in cur.fetchall():
        if lat is None or lng is None:
            continue
        by_id[int(route_cluster_id)]["coords"].append((float(lat), float(lng)))
    return candidates


def route_for_truck_dow(
    cur, truck_no: int, dow: str, position: Optional[dict] = None
) -> Optional[int]:
    """The route_cluster_id this truck runs on `dow` that resolves to a REAL day.

    POSITION-FIRST (Felix road-test, Emery/13 South SF): among the truck's candidate
    clusters for this dow, pick the one whose stops are NEAREST the truck's LIVE
    position (min haversine truck -> any stop centroid), so the app follows the route
    the truck is actually on — not just the fullest one 24 mi away. Only a cluster
    with a REAL day (>= MIN_TIMED_STOPS_FOR_POSITION ordered timed stops) may win on
    nearness, so a near-empty cluster that happens to be close can never beat a real
    day (this guards the earlier "1 / 1" fix). Nearness ties break on stop-count then
    recency for determinism.

    FALLBACK (unchanged prior behavior): when there is NO live position, or no
    candidate clears the timed-stop floor, or none has usable centroids, pick the
    FULLEST day — the truck's candidate cluster with the MOST ordered timed stops
    (ties on recency then active-stop volume). A cluster with zero timed stops for the
    dow can never be chosen over one with a real ordered day.

    Returns None only when the truck has NO cluster with any timed stop for `dow`.
    READ-ONLY against public.
    """
    candidates = _candidate_clusters_for_truck_dow(cur, truck_no, dow)
    if not candidates:
        return None

    # candidates are already ordered fullest-first -> element 0 is the fullest-day pick.
    fullest = candidates[0]["route_cluster_id"]

    # No live position -> keep the fullest-day behavior (nothing to be near).
    if not position or position.get("lat") is None or position.get("lng") is None:
        return fullest

    plat, plng = float(position["lat"]), float(position["lng"])
    best_id = None
    best_d = None
    for c in candidates:
        # Only a REAL day may win on nearness (guards the near-empty "1 / 1" cluster).
        if c["timed_stops"] < MIN_TIMED_STOPS_FOR_POSITION or not c["coords"]:
            continue
        d = min(_haversine_m(plat, plng, lat, lng) for lat, lng in c["coords"])
        # candidates iterate fullest-first, so a strict `<` makes the FULLER cluster
        # win an exact distance tie (deterministic tie-break: nearness, then stops).
        if best_d is None or d < best_d:
            best_d, best_id = d, c["route_cluster_id"]

    # No qualifying cluster near the truck -> fall back to the fullest day.
    return best_id if best_id is not None else fullest


def next_stop_for_route(
    cur, route_cluster_id: int, dow: str, after_order: int = 0
) -> Optional[dict]:
    """The next planned stop (stop_order > after_order) for a route-day, joined to
    its coordinates. after_order=0 -> the first stop (fresh sign-in)."""
    cur.execute(
        """
        SELECT rts.stop_order, rts.stop_cluster_id, rts.arrive, rts.leave_by,
               rts.address, sc.centroid_lat, sc.centroid_long,
               rts.exp_per_visit, rts.visits
        FROM public.route_timed_stops rts
        LEFT JOIN public.stop_clusters sc
               ON sc.stop_cluster_id = rts.stop_cluster_id
        WHERE rts.route_cluster_id = %s AND lower(rts.dow) = lower(%s)
              AND rts.stop_order > %s
        ORDER BY rts.stop_order
        LIMIT 1
        """,
        (route_cluster_id, dow, after_order),
    )
    row = cur.fetchone()
    if not row:
        return None
    order, stop_cluster_id, arrive, leave_by, address, lat, lng, exp_per_visit, visits = row
    return {
        "stop_order": int(order),
        "stop_cluster_id": int(stop_cluster_id) if stop_cluster_id is not None else None,
        "arrive": arrive,
        "leave_by": leave_by,
        "address": address,
        "lat": float(lat) if lat is not None else None,
        "lng": float(lng) if lng is not None else None,
        "exp_per_visit": float(exp_per_visit) if exp_per_visit is not None else None,
        "visits": int(visits) if visits is not None else None,
    }


def route_benchmark(cur, route_cluster_id: int, dow: str) -> dict:
    """The route/global mean $/visit the classifier grades a stop against
    (INTEGRATION_MASTER_ROUTE §2.2). READ-ONLY: two SELECT avg() over
    route_timed_stops.exp_per_visit — the route-day mean (primary comparison) and
    the global mean (used for the gem rate test, matching undervisited_gems.py).
    Returns {'route_mean': float|None, 'global_mean': float|None}."""
    cur.execute(
        """
        SELECT avg(exp_per_visit) FROM public.route_timed_stops
        WHERE route_cluster_id = %s AND lower(dow) = lower(%s)
              AND exp_per_visit IS NOT NULL
        """,
        (route_cluster_id, dow),
    )
    row = cur.fetchone()
    route_mean = float(row[0]) if row and row[0] is not None else None
    cur.execute(
        "SELECT avg(exp_per_visit) FROM public.route_timed_stops WHERE exp_per_visit IS NOT NULL"
    )
    row = cur.fetchone()
    global_mean = float(row[0]) if row and row[0] is not None else None
    return {"route_mean": route_mean, "global_mean": global_mean}


def grade_signals(
    exp_per_visit: Optional[float],
    visits: Optional[int],
    route_mean: Optional[float],
    global_mean: Optional[float],
    *,
    in_season: bool = True,
    event_contaminated: bool = False,
    is_develop_target: bool = False,
) -> "_grade.GradeResult":
    """Pure adapter: real MR columns -> a graded classification.

    Derives the under-visited gem flag exactly like undervisited_gems.py — a stop
    is a gem iff it is BOTH under-visited (visits <= GEM_MAX_VISITS) AND strong
    (exp_per_visit >= GEM_RATE_MULT x the GLOBAL mean). The rate-vs-mean judgement
    uses the route-day mean when available (a stop competes with its own route),
    falling back to the global mean. Everything else defers to grade.classify_stop.
    Season / event / develop-target signals are pass-through (MR supplies them; the
    live season table is currently empty so in_season defaults True, read-safe)."""
    is_gem = (
        visits is not None
        and visits <= GEM_MAX_VISITS
        and exp_per_visit is not None
        and global_mean is not None
        and exp_per_visit >= GEM_RATE_MULT * global_mean
    )
    benchmark = route_mean if route_mean is not None else global_mean
    return _grade.classify_stop(
        _grade.StopSignals(
            exp_per_visit=exp_per_visit,
            benchmark=benchmark,
            in_season=in_season,
            event_contaminated=event_contaminated,
            is_gem=is_gem,
            is_develop_target=is_develop_target,
        )
    )


def ordered_stops_for_route(cur, route_cluster_id: int, dow: str) -> list[dict]:
    """Every planned stop for a route-day, in order, joined to coordinates.
    READ-ONLY. Used by the live route feed to follow the truck's progression."""
    cur.execute(
        """
        SELECT rts.stop_order, rts.stop_cluster_id, rts.arrive, rts.leave_by,
               rts.address, sc.centroid_lat, sc.centroid_long,
               rts.exp_per_visit, rts.visits
        FROM public.route_timed_stops rts
        LEFT JOIN public.stop_clusters sc
               ON sc.stop_cluster_id = rts.stop_cluster_id
        WHERE rts.route_cluster_id = %s AND lower(rts.dow) = lower(%s)
        ORDER BY rts.stop_order
        """,
        (route_cluster_id, dow),
    )
    out = []
    for row in cur.fetchall():
        order, stop_cluster_id, arrive, leave_by, address, lat, lng, exp_per_visit, visits = row
        stop = {
            "stop_order": int(order),
            "stop_cluster_id": int(stop_cluster_id) if stop_cluster_id is not None else None,
            "arrive": arrive,
            "leave_by": leave_by,
            "address": address,
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
            "exp_per_visit": float(exp_per_visit) if exp_per_visit is not None else None,
            "visits": int(visits) if visits is not None else None,
        }
        # route_timed_stops has no kind column. Mark schools from the loaded route
        # address now, before the frozen snapshot reaches advise_plan.
        if _is_school_address(address):
            stop["kind"] = "school"
        out.append(stop)
    return out


def _grade_stop(cur, stop: dict, route_cluster_id: int, dow: str) -> dict:
    """Attach the REAL Master Route grade (1|2 + reason) to a resolved stop."""
    bench = route_benchmark(cur, route_cluster_id, dow)
    result = grade_signals(
        exp_per_visit=stop.get("exp_per_visit"),
        visits=stop.get("visits"),
        route_mean=bench["route_mean"],
        global_mean=bench["global_mean"],
    )
    stop["grade"] = result.grade
    stop["grade_reason"] = result.reason
    return stop


def _attach_live_eta(
    cur, stop: dict, truck_no: int, position: Optional[dict] = None
) -> dict:
    """Attach the traffic-aware live ETA (eta_min / dist_mi / arrive_est) to a stop.

    Reads the truck's latest Geotab position (unless `position` is supplied to avoid a
    duplicate query) and the stop's centroid coords, then calls the Mapbox ETA service.
    Any missing input / Mapbox failure leaves the three keys as None — the payload maps
    None straight through and the client renders "—", NEVER a stale or fake number.

    These are OPERATIONAL nav facts (like the old drive/arrive slots): they ride on the
    BASE stop, shown to coached and nominal drivers identically. Coordinates are used
    here server-side only and are NEVER shipped (payload.build_next_stop drops lat/lng).
    """
    from . import eta as _eta
    from . import geotab, geotab_live

    stop["eta_min"] = None
    stop["dist_mi"] = None
    stop["arrive_est"] = None

    stop_lat, stop_lng = stop.get("lat"), stop.get("lng")
    if stop_lat is None or stop_lng is None:
        return stop  # no stop coords -> graceful null (client shows "—")

    if position is None:
        truck_id = truck_id_for(cur, truck_no)

        # PRIMARY position source: the truck's CURRENT position read LIVE from Geotab
        # (DeviceStatusInfo), moving-or-parked. FIX (Felix road-test, truck 13/Emery): the
        # old primary was geotab.latest_position(log_records), which is ~16h stale AND only
        # records STOPPED trucks (its ingester skips speed>0) — so the app resolved HAYWARD
        # while the truck was really in SOUTH SAN FRANCISCO. We try live first; on ANY
        # failure (unconfigured / error / timeout / no fix) live_position returns None and
        # we FALL BACK to log_records so the mini's local path still works and nothing
        # crashes. Nearest-stop / phase / ETA logic below is unchanged — it just gets a
        # fresh position. No coordinates leave the server (payload drops lat/lng).
        position = None
        try:
            position = geotab_live.live_position(truck_no=truck_no, device_id=truck_id, cur=cur)
        except Exception:
            position = None  # belt+braces: live_position swallows errors, but never crash the feed
        if position is None and truck_id:
            position = geotab.latest_position(cur, truck_id)
    if position is None:
        return stop  # no GPS fix -> graceful null

    result = _eta.live_eta(
        truck_no,
        stop.get("stop_cluster_id"),
        position.get("lat"),
        position.get("lng"),
        stop_lat,
        stop_lng,
    )
    if result is not None:
        stop["eta_min"] = result["eta_min"]
        stop["dist_mi"] = result["dist_mi"]
        stop["arrive_est"] = result["arrive_est"]
    return stop


def build_map_nav(
    truck_no: int,
    position: Optional[dict],
    stops: list[dict],
    current_idx: int,
) -> Optional[dict]:
    """The LIVE MAP nav object (brief §BACKEND): the truck position + the road-
    following route line + the upcoming stop pins. PURE navigation, shown IDENTICALLY
    to every driver (disguise-safe: coordinates ARE allowed inside this map object,
    but it carries NO grade/coached/voice — it is byte-identical coached vs nominal).

    Shape:
      { truck: {lat,lng, heading?},
        line:  [[lng,lat], ...],          # Mapbox geojson, truck -> next ~4-6 stops
        stops: [{lng,lat,name,order}, ...] }

    Graceful degrade (brief §4): with NO live position we omit the whole map (the
    client falls back to the simple view). With a position but no usable Mapbox line
    (no token / timeout / error) we omit ONLY `line` and still ship truck + pins.
    Returns None to omit the map entirely. Coordinates here are server data the map
    NEEDS; they never carry coaching state.
    """
    # No live position -> no map at all (client falls back to the simple view).
    if not position or position.get("lat") is None or position.get("lng") is None:
        return None

    tlat, tlng = float(position["lat"]), float(position["lng"])
    truck = {"lat": tlat, "lng": tlng}
    heading = position.get("bearing")
    if isinstance(heading, (int, float)):
        truck["heading"] = float(heading)

    # Pins show the rest of the confirmed route. The blue line is only the leg
    # to the next stop — drawing it through every pin made the screen a 5-stop list.
    remaining = stops[current_idx:]
    pins = []
    for s in remaining:
        lat, lng = s.get("lat"), s.get("lng")
        if lat is None or lng is None:
            continue
        pins.append(
            {
                "lng": float(lng),
                "lat": float(lat),
                "name": friendly_stop_name(s.get("address")) or "Stop",
                "order": s.get("stop_order"),
            }
        )

    next_pin = pins[:1]
    waypoints = [(tlat, tlng)] + [(p["lat"], p["lng"]) for p in next_pin]
    # Master Route's drive-path (or direct OSRM when its base is unavailable) owns
    # the road geometry. Do not ask Mapbox for a shortest path through the pins and
    # never substitute a straight chord when no street route is available.
    from .street_route import advised_leg
    traced = advised_leg(waypoints)
    line = None
    line_source = None
    if isinstance(traced, dict):
        line = traced.get("line")
        line_source = traced.get("source")

    nav: dict = {"truck": truck, "stops": pins}
    if isinstance(line, list) and line:
        nav["line"] = line
        nav["line_source"] = line_source or "drive-path"
    # Yellow review overlay. The blue line stays the prescribed route. Crumbs are
    # the path the truck actually drove; stops are transmission-Park events (gear
    # 126), not a speed-zero dwell. A Geotab miss omits both — never a fake trail.
    try:
        from .driven_path import driven_path
        driven = driven_path(position.get("device_id"))
    except Exception:
        driven = None
    if isinstance(driven, dict):
        if driven.get("crumbs"):
            nav["driven"] = driven["crumbs"]
        if driven.get("stops"):
            nav["drivenStops"] = driven["stops"]
    return nav


def resolve_next_stop(
    cur, truck_no: int, dow: Optional[str] = None, after_order: int = 0
) -> dict:
    """Full truck -> route -> next-stop resolution, with the REAL Master Route
    grade attached. Returns a dict with the route id and the next stop (carrying
    `grade` 1|2 + `grade_reason` audit tag), or nulls when the truck has no ranked
    route today — the app then simply shows no next-stop card, coaching still runs.

    READ-ONLY against public: the grade is computed from SELECTs over
    route_timed_stops (rate, visits, means) at sign-in — no new column, no write."""
    dow = dow or dow_name()
    # Read the truck's position ONCE (live -> log_records fallback) so route selection,
    # nearest-stop, and ETA all agree. Position-first route selection: pick the DOW
    # cluster NEAREST the live truck, not just the fullest one 24 mi away (Emery/13).
    position = read_truck_position(cur, truck_no)
    route_id = route_for_truck_dow(cur, truck_no, dow, position=position)
    if route_id is None:
        return {"dow": dow, "route_cluster_id": None, "next_stop": None}
    stop = next_stop_for_route(cur, route_id, dow, after_order=after_order)
    if stop is not None:
        bench = route_benchmark(cur, route_id, dow)
        result = grade_signals(
            exp_per_visit=stop.get("exp_per_visit"),
            visits=stop.get("visits"),
            route_mean=bench["route_mean"],
            global_mean=bench["global_mean"],
        )
        stop["grade"] = result.grade
        stop["grade_reason"] = result.reason
        # traffic-aware live ETA on the BASE stop (both modes identical, no coords out).
        # Reuse the position we already read — no duplicate Geotab call.
        _attach_live_eta(cur, stop, truck_no, position=position)
    return {"dow": dow, "route_cluster_id": route_id, "next_stop": stop}


def resolve_live_route(
    cur,
    truck_no: int,
    route_cluster_id: Optional[int] = None,
    dow: Optional[str] = None,
    *,
    frozen_plan: Optional[list[dict]] = None,
    served_orders: Optional[set[int]] = None,
    skipped_orders: Optional[set[int]] = None,
    events: Optional[list[dict]] = None,
    now_minutes: Optional[int] = None,
) -> dict:
    """Follow the truck along its ordered route and return its CURRENT state.

    This is the live-poll counterpart to resolve_next_stop: instead of always the
    first stop, it uses the truck's latest Geotab position to pick the nearest
    ordered stop it is heading to (or serving), grades that stop, and computes the
    live motion phase from Geotab speed + distance-to-that-stop. It also derives
    the §11 shift phase and the plan-strip route count from real route progress.

    Nearest-stop is the defensible progression signal here: there is no per-stop
    served flag, so the stop the truck is closest to is the one it is arriving at /
    parked at, and the next stop becomes nearest as it rolls. As the truck moves,
    the returned next-stop + phase advance on their own — no fabricated queue.

    Returns:
      { dow, route_cluster_id, next_stop (graded 1|2), phase, shift_phase,
        route_count ("N / M" | None), total_stops }

    READ-ONLY against public. Never raises for missing data: no route / no position
    degrade to safe defaults (first stop / hold DRIVING) so the caller never crashes.
    """
    # Import here to avoid any import-order coupling at module load; both are
    # READ-ONLY wiring layers over the same reference cores.
    from . import geotab, geotab_live, motion
    from .refcore import DRIVING

    dow = dow or dow_name()

    truck_id = truck_id_for(cur, truck_no)
    # PRIMARY position: the truck's CURRENT position from Geotab DeviceStatusInfo
    # (moving-or-parked, @ now). FALL BACK to log_records (geotab.latest_position)
    # when Geotab is unconfigured/unreachable/timed-out so nothing crashes and the
    # mini's local path still works. log_records is ~16h stale AND parked-only, so
    # the live source is what actually places a moving truck in the right city.
    # Read ONCE, up front, so route selection AND nearest-stop see the same position.
    position = read_truck_position(cur, truck_no, truck_id=truck_id)

    # Follow the caller's cluster when given (the session's stored route, itself picked
    # position-first at sign-in); otherwise select position-first here too, so a
    # cluster-less live poll picks the DOW route NEAREST the truck, not the fullest.
    route_id = route_cluster_id if route_cluster_id is not None else route_for_truck_dow(
        cur, truck_no, dow, position=position
    )
    if route_id is None:
        return {
            "dow": dow, "route_cluster_id": None, "next_stop": None,
            "phase": DRIVING, "shift_phase": "plan", "route_count": None, "total_stops": 0,
        }

    # Confirmation persists this exact source snapshot. For legacy/session-less calls we
    # still resolve the ordered Master Route once, but GPS never selects an index.
    stops = frozen_plan if frozen_plan is not None else ordered_stops_for_route(cur, route_id, dow)
    total = len(stops)
    if total == 0:
        return {"dow": dow, "route_cluster_id": route_id, "next_stop": None,
                "phase": DRIVING, "shift_phase": "plan", "route_count": None, "total_stops": 0}

    from .advisor import advise_plan
    now = datetime.datetime.now().astimezone().hour * 60 + datetime.datetime.now().astimezone().minute if now_minutes is None else now_minutes
    advice = advise_plan(stops, served_orders=served_orders or set(), skipped_orders=skipped_orders or set(),
                         events=events or [], now_minutes=now)
    stop = dict(advice["next_stop"]) if advice["next_stop"] else None
    legacy_index = None
    # Compatibility only for callers that have not confirmed a session yet: preserve
    # the old position display. Confirmed sessions always pass frozen_plan and therefore
    # can advance solely by Done/Skip (or dwell persistence), never proximity.
    if frozen_plan is None and not served_orders and not skipped_orders and not events and position is not None:
        nearest = min(
            (s for s in stops if s.get("lat") is not None and s.get("lng") is not None),
            key=lambda s: geotab.haversine_m(position["lat"], position["lng"], s["lat"], s["lng"]),
            default=None,
        )
        if nearest is not None:
            stop = dict(nearest)
            legacy_index = next((i for i, candidate in enumerate(stops) if candidate is nearest), None)
    if stop is not None and stop.get("kind") != "event":
        _grade_stop(cur, stop, route_id, dow)
        _attach_live_eta(cur, stop, truck_no, position=position)
    if stop is not None:
        stop["advice_reason"] = advice["reason"]

    phase = DRIVING
    if truck_id and stop is not None and stop.get("kind") != "event":
        fix = geotab.latest_fix(cur, truck_id, stop.get("lat"), stop.get("lng"))
        phase = motion.phase_for_fix(fix, prior_phase=DRIVING)

    completed = len(served_orders or set()) + len(skipped_orders or set())
    shift_phase = "wrap" if (stop is None or (completed >= total and phase == "parked")) else "plan"
    route_count = f"{legacy_index + 1 if legacy_index is not None else min(completed + 1, total)} / {total}"

    # Advice may pull a time-critical school/event forward, but the frozen source list
    # remains intact. The driver sees the advised leg first, then the other remaining
    # plan entries — not a GPS-generated reroute.
    completed_orders = (served_orders or set()) | (skipped_orders or set())
    active = [s for s in advice["plan"] if s.get("kind") == "event" or s.get("stop_order") not in completed_orders]
    def is_advised(row: dict) -> bool:
        if not stop:
            return False
        if stop.get("kind") == "event":
            return row.get("kind") == "event" and row.get("event_id") == stop.get("event_id")
        return row.get("stop_order") == stop.get("stop_order")
    nav_stops = ([stop] if stop else []) + [s for s in active if not is_advised(s)]
    up_next = [{"name": friendly_stop_name(s.get("address")) or s.get("title") or "Next stop", "arrive": s.get("arrive")}
               for s in nav_stops[1:1 + UP_NEXT_MAX]]

    turns = []
    if position and stop and stop.get("lat") is not None and stop.get("lng") is not None:
        from . import eta as _eta
        turns = _eta.route_steps(truck_no, [(position["lat"], position["lng"]), (stop["lat"], stop["lng"])])
    nav = build_map_nav(truck_no, position, nav_stops, 0)
    return {"dow": dow, "route_cluster_id": route_id, "next_stop": stop,
            "phase": phase, "shift_phase": shift_phase, "route_count": route_count,
            "total_stops": total, "up_next": up_next, "advice_reason": advice["reason"],
            "turns": turns, "map": nav,
            "driven_stops": (nav or {}).get("drivenStops") or [],
            # The whole frozen plan, not the one stop on screen. A Park can clear
            # a pin the advisor has not reached yet.
            "match_plan": advice["plan"]}

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


def dow_name(d: Optional[datetime.date] = None) -> str:
    return _DOW[(d or datetime.date.today()).weekday()]


def truck_id_for(cur, truck_no: int) -> Optional[str]:
    cur.execute(
        "SELECT truck_id FROM public.trucks WHERE truck_number = %s", (truck_no,)
    )
    row = cur.fetchone()
    return row[0] if row else None


def route_for_truck_dow(cur, truck_no: int, dow: str) -> Optional[int]:
    """The route_cluster_id this truck runs on `dow` that resolves to a REAL day.

    BUG FIX (Felix road-test, Emery/13): the old picker ordered the truck's
    active_sale_stops clusters by `max(created_at)` (most-recently-seen) and took
    the top one. On truck 13 Wednesday that landed on a cluster whose
    `route_timed_stops(dow)` was nearly empty — the app resolved a 1-stop route
    while the truck's real 24-stop Wednesday (cluster 1955) was ignored.

    The fix ranks the truck's candidate clusters by how many ORDERED TIMED STOPS
    each actually has for this dow (the real signal for "which route is a full
    day"), and picks the fullest. Ties break on recency then active-stop volume so
    a genuine multi-cluster day is still deterministic. A cluster with zero timed
    stops for the dow can never be chosen over one with a real ordered day.

    Returns None only when the truck has NO cluster with any timed stop for `dow`.
    READ-ONLY against public.
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
        LIMIT 1
        """,
        (dow, truck_no, dow),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


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
        out.append(
            {
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
        )
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
    from . import geotab

    stop["eta_min"] = None
    stop["dist_mi"] = None
    stop["arrive_est"] = None

    stop_lat, stop_lng = stop.get("lat"), stop.get("lng")
    if stop_lat is None or stop_lng is None:
        return stop  # no stop coords -> graceful null (client shows "—")

    if position is None:
        truck_id = truck_id_for(cur, truck_no)
        position = geotab.latest_position(cur, truck_id) if truck_id else None
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
    route_id = route_for_truck_dow(cur, truck_no, dow)
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
        _attach_live_eta(cur, stop, truck_no)
    return {"dow": dow, "route_cluster_id": route_id, "next_stop": stop}


def resolve_live_route(
    cur,
    truck_no: int,
    route_cluster_id: Optional[int] = None,
    dow: Optional[str] = None,
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
    from . import geotab, motion
    from .refcore import DRIVING

    dow = dow or dow_name()
    route_id = route_cluster_id if route_cluster_id is not None else route_for_truck_dow(cur, truck_no, dow)
    if route_id is None:
        return {
            "dow": dow, "route_cluster_id": None, "next_stop": None,
            "phase": DRIVING, "shift_phase": "plan", "route_count": None, "total_stops": 0,
        }

    stops = ordered_stops_for_route(cur, route_id, dow)
    total = len(stops)
    if total == 0:
        return {
            "dow": dow, "route_cluster_id": route_id, "next_stop": None,
            "phase": DRIVING, "shift_phase": "plan", "route_count": None, "total_stops": 0,
        }

    truck_id = truck_id_for(cur, truck_no)
    position = geotab.latest_position(cur, truck_id) if truck_id else None

    # Pick the current stop: nearest ordered stop to the live position; fall back to
    # the first stop when we have no position (fresh sign-in / no pings).
    idx = 0
    if position is not None:
        best_d = None
        for i, s in enumerate(stops):
            if s.get("lat") is None or s.get("lng") is None:
                continue
            d = geotab.haversine_m(position["lat"], position["lng"], s["lat"], s["lng"])
            if best_d is None or d < best_d:
                best_d, idx = d, i
    stop = dict(stops[idx])
    _grade_stop(cur, stop, route_id, dow)
    # traffic-aware live ETA on the BASE stop — reuse the position we already read
    # (both modes identical; graceful null on no-GPS/timeout/error; no coords on wire).
    _attach_live_eta(cur, stop, truck_no, position=position)

    # Live motion phase from Geotab against THIS stop (server owns the phase).
    phase = DRIVING
    if truck_id:
        fix = geotab.latest_fix(cur, truck_id, stop.get("lat"), stop.get("lng"))
        phase = motion.phase_for_fix(fix, prior_phase=DRIVING)

    # §11 shift phase from real route position: heading in (wrap) once the truck is
    # settled at the final ordered stop; otherwise plan. 'tail' (post-plan extension
    # stops) needs an extension-queue signal we do not have, so we never fake it.
    shift_phase = "wrap" if (idx == total - 1 and phase == "parked") else "plan"

    # Plan-strip count = 1-based position through the ordered stops.
    route_count = f"{idx + 1} / {total}"

    # REAL "Up next" forward queue: the next few ORDERED stops after the current one
    # (name + booked time), so the client paints the driver's actual remaining day
    # instead of a hardcoded demo list (Felix road-test: fake Riverside/Sunset/Harbor).
    # Empty near the end of the day -> the client HIDES the Up-Next list. Never a fake.
    up_next = []
    for s in stops[idx + 1: idx + 1 + UP_NEXT_MAX]:
        addr = s.get("address")
        up_next.append(
            {
                # friendly NAME = STREET (not house numbers), same helper as nextStop
                # so a multi-unit cluster never ships a ';'-joined number string.
                "name": friendly_stop_name(addr) or "Next stop",
                "arrive": s.get("arrive"),   # BOOKED schedule time (client formats AM/PM)
            }
        )

    return {
        "dow": dow,
        "route_cluster_id": route_id,
        "next_stop": stop,
        "phase": phase,
        "shift_phase": shift_phase,
        "route_count": route_count,
        "total_stops": total,
        "up_next": up_next,
    }

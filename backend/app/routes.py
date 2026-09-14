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

_DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Under-visited gem definition, mirrored from master-route/research/undervisited_gems.py
# so the cab and MR agree on "great but we don't go often" (INTEGRATION_MASTER_ROUTE §2.2).
#   - UNDER-visited: route_timed_stops.visits <= GEM_MAX_VISITS
#   - strong rate:   exp_per_visit (the shrunk $/visit expectation) >= GEM_RATE_MULT x global mean
# Every stop meeting BOTH is a gem, and every gem grades 2 (cultivate, don't camp).
GEM_MAX_VISITS = 8
GEM_RATE_MULT = 1.5


def dow_name(d: Optional[datetime.date] = None) -> str:
    return _DOW[(d or datetime.date.today()).weekday()]


def truck_id_for(cur, truck_no: int) -> Optional[str]:
    cur.execute(
        "SELECT truck_id FROM public.trucks WHERE truck_number = %s", (truck_no,)
    )
    row = cur.fetchone()
    return row[0] if row else None


def route_for_truck_dow(cur, truck_no: int, dow: str) -> Optional[int]:
    """The route_cluster_id this truck most recently ran on `dow` (real signal
    from active_sale_stops). None if the truck has no ranked route for that day."""
    cur.execute(
        """
        SELECT route_cluster_id
        FROM public.active_sale_stops
        WHERE truck_number = %s AND lower(dow) = lower(%s)
              AND route_cluster_id IS NOT NULL
        GROUP BY route_cluster_id
        ORDER BY max(created_at) DESC NULLS LAST, count(*) DESC
        LIMIT 1
        """,
        (truck_no, dow),
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
    return {"dow": dow, "route_cluster_id": route_id, "next_stop": stop}

"""Serve Andrew's approved Master Route as the driver plan (rollout-gated).

SERVE_MASTER (env) picks the stop source for a route-day:
  off       (default) today's behaviour: always route_timed_stops (source='algorithm')
  approved  the active live master for the season, ONLY if it is approved/locked
  all       the active live master for the season, approved or not (testing)

Season pick mirrors assignment._live_rows: exact season tag first, then blank ''.
When no qualifying master exists the caller falls back to route_timed_stops and
marks every stop plan_source='algorithm'. READ-ONLY: no writes, no DDL.
"""
from __future__ import annotations

import datetime
import os
from typing import Optional

MODES = ("off", "approved", "all")


def serve_mode() -> str:
    mode = (os.environ.get("SERVE_MASTER") or "off").strip().lower()
    return mode if mode in MODES else "off"


def season_for(day: datetime.date) -> str:
    from .assignment import _season_variant
    return _season_variant(day)


def _has_approval_columns(cur) -> bool:
    cur.execute(
        """SELECT count(*) FROM information_schema.columns
           WHERE table_name = 'geotab_route_masters'
             AND column_name IN ('approved_at', 'locked')"""
    )
    return int(cur.fetchone()[0]) == 2


def resolve_master(cur, route_cluster_id: int, dow: str, day: Optional[datetime.date] = None,
                   mode: Optional[str] = None) -> Optional[dict]:
    """The master that should be served, or None (-> algorithm fallback)."""
    mode = mode or serve_mode()
    if mode == "off":
        return None
    season = season_for(day or datetime.date.today())
    approved_sql = ""
    if mode == "approved":
        if not _has_approval_columns(cur):
            return None  # migration not applied yet: nothing can be approved
        approved_sql = " AND (m.locked OR m.approved_at IS NOT NULL)"
    cur.execute(
        """
        SELECT m.id, m.season_variant, m.source_truck, m.source_date
        FROM public.geotab_route_masters m
        WHERE m.route_cluster_id = %s AND lower(m.dow) = lower(%s)
          AND m.active AND m.live
          AND m.season_variant IN (%s, '')""" + approved_sql + """
        ORDER BY (m.season_variant = ''), m.generated_at DESC
        LIMIT 1
        """,
        (route_cluster_id, dow, season),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"master_id": int(row[0]), "season_variant": row[1], "source_truck": row[2],
            "source_date": row[3], "route_cluster_id": int(route_cluster_id), "dow": dow}


def _add_minutes(hhmm: Optional[str], seconds: Optional[float]) -> Optional[str]:
    if not hhmm or seconds is None:
        return hhmm
    try:
        h, m = (int(x) for x in str(hhmm)[:5].split(":"))
    except ValueError:
        return hhmm
    total = h * 60 + m + int(round(float(seconds) / 60.0))
    total = min(total, 23 * 60 + 59)
    return f"{total // 60:02d}:{total % 60:02d}"


def master_stops(cur, master: dict) -> list[dict]:
    """The master's stops in master order, shaped like routes.ordered_stops_for_route.

    exp_per_visit/visits come from the latest timed build for the same cluster when
    present (grading only); otherwise None (grade is unknown-safe)."""
    from .routes import latest_build_sql
    cur.execute(
        """
        SELECT s.stop_order, s.stop_cluster_id, s.arrive, s.stop_sec, s.daypart,
               s.address, s.lat, s.lon, t.exp_per_visit, t.visits
        FROM public.geotab_route_master_stops s
        LEFT JOIN LATERAL (
            SELECT rts.exp_per_visit, rts.visits FROM public.route_timed_stops rts
            WHERE rts.route_cluster_id = s.route_cluster_id
              AND lower(rts.dow) = lower(s.dow)
              AND rts.stop_cluster_id = s.stop_cluster_id
              AND """ + latest_build_sql("rts") + """
            LIMIT 1) t ON TRUE
        WHERE s.route_cluster_id = %s AND lower(s.dow) = lower(%s)
          AND s.season_variant = %s
        ORDER BY s.stop_order
        """,
        (master["route_cluster_id"], master["dow"], master["season_variant"]),
    )
    out = []
    for (order, sid, arrive, stop_sec, daypart, address, lat, lon, epv, visits) in cur.fetchall():
        stop = {
            "stop_order": int(order),
            "stop_cluster_id": int(sid) if sid is not None else None,
            "arrive": arrive,
            "leave_by": _add_minutes(arrive, stop_sec),
            "dwell_sec": float(stop_sec) if stop_sec is not None else None,
            "address": address,
            "lat": float(lat) if lat is not None else None,
            "lng": float(lon) if lon is not None else None,
            "exp_per_visit": float(epv) if epv is not None else None,
            "visits": int(visits) if visits is not None else None,
            "plan_source": "master",
            "master_id": master["master_id"],
        }
        if daypart == "school":
            stop["kind"] = "school"
        out.append(stop)
    return out

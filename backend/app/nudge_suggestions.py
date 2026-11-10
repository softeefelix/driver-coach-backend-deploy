"""Up to two move-along suggestions from the driver's own master route.

The frozen plan and Master Route tables are read only. Served and skipped
orders are not rewritten. A stop another truck's plan already covers today is
left off the list. Historical rank is ``exp_per_visit`` (shrunk dollars per
visit), the same Master Route column the grade chip already reads.
"""
from __future__ import annotations

import datetime
import json
from typing import Optional

from .holds import day_bounds
from .payload import friendly_stop_name

SUGGESTION_LIMIT = 2


def _cluster(stop: dict) -> Optional[int]:
    value = stop.get("stop_cluster_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _order(stop: dict) -> Optional[int]:
    value = stop.get("stop_order")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _rate(stop: dict) -> Optional[float]:
    value = stop.get("exp_per_visit")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number:
        return None
    return number


def choose_suggestions(
    *,
    master_stops: Optional[list] = None,
    timed_stops: Optional[list] = None,
    served_orders: Optional[set] = None,
    skipped_orders: Optional[set] = None,
    current_order: Optional[int] = None,
    covered_cluster_ids: Optional[set] = None,
    limit: int = SUGGESTION_LIMIT,
) -> list[dict]:
    """Rank the driver's own stops. Pure: no database and no mutation.

    ``master_stops`` is the frozen master. ``timed_stops`` is the plan the cab
    is running. Stops still ahead of the current pin, and master stops the
    timed plan dropped, are eligible. Other trucks' cluster ids are excluded.
    """
    master = [stop for stop in (master_stops or []) if isinstance(stop, dict)]
    timed = [stop for stop in (timed_stops or []) if isinstance(stop, dict)]
    served = served_orders or set()
    skipped = skipped_orders or set()
    covered = covered_cluster_ids or set()
    source = master or timed
    if not source:
        return []

    timed_clusters = {cluster for cluster in (_cluster(stop) for stop in timed) if cluster is not None}
    blocked: set[int] = set()
    current_cluster = None
    for stop in timed or source:
        order = _order(stop)
        cluster = _cluster(stop)
        if order is None:
            continue
        if order == current_order and cluster is not None:
            current_cluster = cluster
        if order in served or order in skipped or order == current_order:
            if cluster is not None:
                blocked.add(cluster)

    current_index = None
    if current_cluster is not None:
        for index, stop in enumerate(source):
            if _cluster(stop) == current_cluster:
                current_index = index
                break
    elif current_order is not None:
        for index, stop in enumerate(source):
            if _order(stop) == current_order:
                current_index = index
                break

    ranked = []
    for index, stop in enumerate(source):
        cluster = _cluster(stop)
        order = _order(stop)
        if cluster is not None and (cluster in covered or cluster in blocked):
            continue
        if cluster is None and order is not None and (
            order in served or order in skipped or order == current_order
        ):
            continue
        ahead = current_index is None or index > current_index
        dropped = bool(master and timed) and cluster is not None and cluster not in timed_clusters
        if not ahead and not dropped:
            continue
        rate = _rate(stop)
        why = "dropped" if dropped else "ahead"
        ranked.append(
            {
                "stopOrder": order,
                "stopClusterId": cluster,
                "name": friendly_stop_name(stop.get("address") or stop.get("title")),
                "why": why,
                "expPerVisit": rate,
                "_index": index,
            }
        )
    ranked.sort(key=lambda row: (row["expPerVisit"] is None, -(row["expPerVisit"] or 0.0), row["_index"]))
    out = []
    for row in ranked[: max(0, int(limit))]:
        item = dict(row)
        item.pop("_index", None)
        out.append(item)
    return out


def _release(cur, name: str, *, failed: bool) -> None:
    if failed:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    else:
        cur.execute(f"RELEASE SAVEPOINT {name}")


def _master_stops(cur, route_cluster_id: Optional[int]) -> list:
    if route_cluster_id is None:
        return []
    cur.execute("SAVEPOINT nudge_master")
    failed = False
    stops: list = []
    try:
        from . import master_plan
        from .routes import dow_name, pacific_date

        master = master_plan.resolve_master(
            cur, int(route_cluster_id), dow_name(), pacific_date(), mode="all",
        )
        if master:
            stops = master_plan.master_stops(cur, master) or []
    except Exception:
        failed = True
        stops = []
    _release(cur, "nudge_master", failed=failed)
    return stops


def _covered(cur, session_id: str, now: datetime.datetime) -> set[int]:
    start, end = day_bounds(now)
    cur.execute("SAVEPOINT nudge_covered")
    failed = False
    covered: set[int] = set()
    try:
        cur.execute(
            """
            SELECT plan_snapshot FROM driver_coach_session
            WHERE session_id <> %s AND started_at >= %s AND started_at < %s
            """,
            (session_id, start, end),
        )
        for (raw,) in cur.fetchall():
            rows = raw if isinstance(raw, list) else json.loads(raw or "[]")
            if not isinstance(rows, list):
                continue
            for stop in rows:
                if isinstance(stop, dict):
                    cluster = _cluster(stop)
                    if cluster is not None:
                        covered.add(cluster)
    except Exception:
        failed = True
        covered = set()
    _release(cur, "nudge_covered", failed=failed)
    return covered


def for_poll(
    cur,
    *,
    session_id: str,
    route_cluster_id: Optional[int],
    frozen_plan,
    served_orders: set,
    skipped_orders: set,
    current_order: Optional[int],
    now: datetime.datetime,
) -> list[dict]:
    """Best-effort suggestions. A missing master table or column yields []."""
    timed = frozen_plan if isinstance(frozen_plan, list) else []
    try:
        master = _master_stops(cur, route_cluster_id)
        covered = _covered(cur, session_id, now)
        return choose_suggestions(
            master_stops=master,
            timed_stops=timed,
            served_orders=served_orders,
            skipped_orders=skipped_orders,
            current_order=current_order,
            covered_cluster_ids=covered,
        )
    except Exception:
        return []

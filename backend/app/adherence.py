"""Read-only Geotab impression of a frozen plan. Never a stop outcome.

Office can show this while the cab iPad is offline. It does not choose the cab
cursor, and nothing here is written to served or skipped stop orders. A failure
or a malformed trail returns an empty estimate so the cab poll still succeeds.
"""
from __future__ import annotations

import logging
import math

from .driven_path import park_served_orders, parked_plan_stop, passed_orders

_log = logging.getLogger(__name__)


def empty_adherence() -> dict:
    """A non-authoritative estimate that changes no cursor and names no stop."""
    return {
        "source": "geotab",
        "authoritative": False,
        "affectsCursor": False,
        "estimatedServedStopOrders": [],
        "estimatedPassedStopOrders": [],
        "parkedStopOrder": None,
    }


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _clean_plan(plan):
    if not isinstance(plan, (list, tuple)):
        return []
    rows = []
    for stop in plan:
        if not isinstance(stop, dict):
            continue
        order = stop.get("stop_order")
        lat, lng = _finite(stop.get("lat")), _finite(stop.get("lng"))
        if isinstance(order, bool) or not isinstance(order, int) or lat is None or lng is None:
            continue
        rows.append({**stop, "stop_order": order, "lat": lat, "lng": lng})
    return rows


def _clean_parks(park_stops):
    if not isinstance(park_stops, (list, tuple)):
        return []
    parks = []
    for park in park_stops:
        if not isinstance(park, dict):
            continue
        lat, lng = _finite(park.get("lat")), _finite(park.get("lng"))
        if lat is None or lng is None:
            continue
        parks.append({**park, "lat": lat, "lng": lng})
    return parks


def adherence_estimate(plan, park_stops, *, now_lat, now_lng) -> dict:
    """Gear-126 parks and pass-by near a later pin, kept separate from Done/Skip.

    A current Park within 120m is `parkedStopOrder` and is not a completion.
    A departed Park within 120m is `estimatedServedStopOrders`. Being within
    250m of a later pin puts earlier pins in `estimatedPassedStopOrders`; that
    is not a skip. `authoritative` and `affectsCursor` stay false.
    Missing or malformed telemetry yields an empty estimate and does not raise.
    """
    estimate = empty_adherence()
    lat, lng = _finite(now_lat), _finite(now_lng)
    if lat is None or lng is None:
        return estimate
    try:
        rows = _clean_plan(plan)
        parks = _clean_parks(park_stops)
        estimate["estimatedServedStopOrders"] = [
            order for order in park_served_orders(rows, parks, now_lat=lat, now_lng=lng)
            if isinstance(order, int) and not isinstance(order, bool)
        ]
        estimate["estimatedPassedStopOrders"] = [
            order for order in passed_orders(rows, now_lat=lat, now_lng=lng)
            if isinstance(order, int) and not isinstance(order, bool)
        ]
        parked = parked_plan_stop(rows, parks, now_lat=lat, now_lng=lng)
        order = parked.get("stop_order") if isinstance(parked, dict) else None
        if isinstance(order, int) and not isinstance(order, bool):
            estimate["parkedStopOrder"] = order
    except Exception as exc:
        _log.warning("adherence estimate unavailable: %s", type(exc).__name__)
        return empty_adherence()
    return estimate

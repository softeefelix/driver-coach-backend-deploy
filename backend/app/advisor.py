"""Pure decision core for a confirmed Driver Coach plan.

The frozen plan is an ordered snapshot. GPS is intentionally absent from the choice:
it can classify motion but can never reselect a route or silently advance the cursor.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Iterable, Optional


_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])\s*$")


def clock_minutes(value: object) -> Optional[int]:
    """Parse the cab-facing 12-hour clock, returning minutes after midnight."""
    if not isinstance(value, str):
        return None
    match = _CLOCK_RE.match(value)
    if not match:
        return None
    hour, minute, suffix = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if not 1 <= hour <= 12 or minute > 59:
        return None
    return (hour % 12 + (12 if suffix == "PM" else 0)) * 60 + minute


def _label(stop: dict) -> str:
    address = str(stop.get("address") or stop.get("title") or "this stop")
    return address.split(",", 1)[0]


def insert_events(plan: Iterable[dict], events: Iterable[dict]) -> list[dict]:
    """Return a display/advice plan with ONE_OFF events inserted by booked start time.

    The original plan's stops keep their exact order and identity. Events are additive
    session advice entries; this does not rewrite Master Route or delete a stop.
    """
    rows = [deepcopy(stop) for stop in plan]
    for event in events or []:
        if not isinstance(event, dict) or not event.get("address") or not event.get("startTime"):
            continue
        rows.append({
            "kind": "event",
            "event_id": str(event.get("id") or event.get("title") or event["address"]),
            "title": event.get("title") or "Booked event",
            "address": event["address"],
            "arrive": event["startTime"],
            "lat": event.get("lat"),
            "lng": event.get("lng"),
            # An un-geocodable booking stays in the advised plan. The explicit state
            # lets the cab-facing advice say why its map pin cannot be drawn.
            "coordinates_missing": bool(event.get("coordinates_missing")) or (
                event.get("lat") is None or event.get("lng") is None
            ),
            "stop_order": None,
        })
    # Stable sort preserves the frozen cruise order among equal/unknown times.
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda pair: (
        clock_minutes(pair[1].get("arrive")) is None,
        clock_minutes(pair[1].get("arrive")) if clock_minutes(pair[1].get("arrive")) is not None else 24 * 60 + pair[0],
        pair[0],
    ))
    return [row for _, row in indexed]


def advise_plan(
    frozen_plan: Iterable[dict],
    *,
    served_orders: set[int],
    skipped_orders: set[int],
    now_minutes: int,
    events: Iterable[dict] = (),
    gps_nearest_order: Optional[int] = None,
    deadline_horizon_minutes: int = 90,
) -> dict:
    """Choose the best next action without mutating the frozen ordered plan.

    A booked school/event inside the horizon is allowed to advise ahead of the cruise
    cursor. Otherwise the first remaining frozen plan stop wins. `gps_nearest_order`
    exists only to make the non-inference invariant explicit and is deliberately unused.
    """
    del gps_nearest_order
    frozen = [deepcopy(stop) for stop in frozen_plan]
    all_rows = insert_events(frozen, events)

    def active(stop: dict) -> bool:
        order = stop.get("stop_order")
        return stop.get("kind") == "event" or (order not in served_orders and order not in skipped_orders)

    remaining = [stop for stop in frozen if active(stop)]
    active_rows = [stop for stop in all_rows if active(stop)]
    cruise_next = next((stop for stop in frozen if active(stop)), None)

    urgent = []
    for stop in active_rows:
        due = clock_minutes(stop.get("arrive"))
        if due is None or due < now_minutes or due - now_minutes > deadline_horizon_minutes:
            continue
        if stop.get("kind") == "event" or stop.get("kind") == "school":
            urgent.append((due, stop))
    if urgent:
        _, next_stop = min(urgent, key=lambda item: item[0])
        if next_stop.get("kind") == "event":
            reason = f"Get to booked event by {next_stop['arrive']}"
            if next_stop.get("coordinates_missing"):
                reason += " (coordinates missing)"
        else:
            reason = f"Get to {_label(next_stop)} by {next_stop['arrive']}"
    else:
        next_stop = cruise_next
        reason = "Next planned stop" if next_stop else "Plan complete"

    return {"plan": all_rows, "remaining": remaining, "next_stop": next_stop, "reason": reason}

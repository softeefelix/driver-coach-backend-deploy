"""Explicit cab cursor chosen by the driver.

Stored inside the append-only chooser state under ``driverProgress``. It does not
rewrite served or skipped history and it does not replace the frozen plan.
A missing key means revision 0 and no explicit target, which is how every
session written before this field behaves.
"""
from __future__ import annotations


class ProgressConflict(Exception):
    """expected_revision does not match the session. No write was applied."""

    def __init__(self, revision: int, target_order):
        self.revision = revision
        self.target_order = target_order
        super().__init__("progress revision conflict")


def empty_progress() -> dict:
    return {"revision": 0, "targetOrder": None}


def from_state(state) -> dict:
    """Read driverProgress from a chooser state. Unknown or absent data is revision 0."""
    if not isinstance(state, dict):
        return empty_progress()
    raw = state.get("driverProgress")
    if not isinstance(raw, dict):
        return empty_progress()
    revision = raw.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        revision = 0
    target = raw.get("targetOrder")
    if isinstance(target, bool) or not isinstance(target, int):
        target = None
    return {"revision": revision, "targetOrder": target}


def stamp(progress) -> dict:
    """JSON-safe copy stored on the next chooser state row."""
    revision = progress.get("revision", 0) if isinstance(progress, dict) else 0
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        revision = 0
    target = progress.get("targetOrder") if isinstance(progress, dict) else None
    if isinstance(target, bool) or not isinstance(target, int):
        target = None
    return {"revision": revision, "targetOrder": target}


def merge_into_advice(advice, progress) -> None:
    """Keep an explicit driver cursor on the chooser row this poll appends.

    School-slack still writes its own choice. This only carries the driver
    cursor forward so a route poll cannot drop it.
    """
    if not isinstance(advice, dict) or not isinstance(advice.get("state"), dict):
        return
    if progress is None:
        return
    advice["state"]["driverProgress"] = stamp(progress)


def first_stop_order(plan):
    """First uploaded stop_order. Snapshot order, not numeric order and not GPS."""
    for stop in plan or []:
        if not isinstance(stop, dict):
            continue
        order = stop.get("stop_order")
        if isinstance(order, int) and not isinstance(order, bool):
            return order
    return None


def plan_stop(plan, stop_order):
    if isinstance(stop_order, bool) or not isinstance(stop_order, int):
        return None
    for stop in plan or []:
        if isinstance(stop, dict) and stop.get("stop_order") == stop_order:
            return stop
    return None


def require_revision(progress, expected_revision) -> None:
    current = progress["revision"] if isinstance(progress, dict) else 0
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise ValueError("expected_revision is required")
    if expected_revision != current:
        raise ProgressConflict(current, progress.get("targetOrder") if isinstance(progress, dict) else None)


def bump(progress, target_order) -> dict:
    current = progress["revision"] if isinstance(progress, dict) else 0
    if isinstance(target_order, bool) or not isinstance(target_order, int):
        target_order = None
    return {"revision": current + 1, "targetOrder": target_order}

"""Disguise-safe session payload builder — THE disguise boundary on the server.

The app never receives a `coached`/`voice` boolean (SERVER_FLAGS spec §2.4). The
server resolves the flags and ships only resolved CONTENT, in the exact shape the
client's session core already consumes (app/src/core/session.js):

  base = { driver:{id,name}, truck, route:{ nextStop } }
  COACHED -> base + { coach: { grade{1,2}, stopDone{1,2}, arrivalCue? } }
     - arrivalCue key PRESENT iff arrival_voice (the 40-stop rule); ABSENT = silent
  NOMINAL -> base (NO `coach` key at all; absence == nominal)

Invariants this module guarantees (asserted by tests + assertDisguiseSafe here,
mirroring the client's session.js assertDisguiseSafe):
  - no `coached`/`nominal` key anywhere on the payload
  - the coach bundle carries no `voice`/`arrival_voice`/`coached` boolean
  - a nominal payload and a coached-but-silent payload differ ONLY by the presence
    of the `coach` object — there is no boolean to sniff

The coach VOCABULARY is the Warden-PASSed client copy (grade.js GRADE + stopDone),
carried verbatim so the server ships exactly what Warden reviewed and the client's
simplicity-law re-check (assertLawful) passes.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

from typing import Optional

from .refcore import ResolvedFlags

# Verbatim from app/src/core/grade.js GRADE (the whole coached vocabulary).
_GRADE_COPY = {
    "1": {
        "short": "Good stop",
        "line": "Good stop. Work it while it\u2019s working, then roll.",
        "speak": "Good stop. Work it while it\u2019s working, then roll.",
    },
    "2": {
        "short": "Needs work",
        "line": "Needs work. Cultivate it \u2014 make the asks, don\u2019t over-stay.",
        "speak": "Needs work. Cultivate it \u2014 make the asks, don\u2019t over-stay.",
    },
}
# Verbatim from grade.js stopDoneLine.
_STOP_DONE = {
    "1": "Good stop.",
    "2": "That one needed work \u2014 next one\u2019s a 1.",
}

# Booleans that must never appear on the wire (mirrors session.js assertDisguiseSafe).
# `grade`/`nextStopGrade` on the BASE payload would be a per-stop value a nominal
# driver could diff against a coached peer — the grade value lives ONLY inside the
# coach bundle (present-iff-coached), so it is forbidden at the top level.
_FORBIDDEN_TOP = ("coached", "nominal", "grade", "nextStopGrade")
# Inside the coach bundle: no mode/voice boolean, and no `grade_reason` — the audit
# tag is server-side only and is NEVER spoken or shown to a driver.
_FORBIDDEN_COACH = ("voice", "arrival_voice", "coached", "grade_reason")

# Safe default grade when a coached stop has no computed grade (no route / missing
# MR data): 2 = "needs work"/cultivate. Never fake a 1 ("camp here") on missing data.
_DEFAULT_GRADE = 2


def _coach_bundle(arrival_voice: bool, next_stop_grade: int) -> dict:
    bundle = {
        "grade": {k: dict(v) for k, v in _GRADE_COPY.items()},
        "stopDone": dict(_STOP_DONE),
        # the per-stop grade VALUE (1|2) — the coach's entire output. Lives INSIDE
        # the coach bundle so a nominal payload has no grade value to leak.
        "nextStopGrade": next_stop_grade,
    }
    # arrivalCue asset PRESENT iff voice on — never a boolean; absence == silent.
    if arrival_voice:
        bundle["arrivalCue"] = True
    return bundle


def build_next_stop(stop: Optional[dict], route_cluster_id: Optional[int]) -> Optional[dict]:
    """Map a resolved route next-stop to the client route.nextStop shape.
    Coordinates are NOT shipped to the client (disguise: the app carries no lat/lng
    for the truck; the motion phase is computed server-side). Only human-facing
    route text goes on the wire."""
    if not stop:
        return None
    name = None
    addr = stop.get("address")
    if addr:
        # first address segment as a friendly stop name; keep sub = full-ish address
        name = addr.split(",")[0].strip() or None
    return {
        "name": name or "Next stop",
        "sub": addr,
        "arrive": stop.get("arrive"),
        "leaveBy": stop.get("leave_by"),
    }


def assert_disguise_safe(payload: dict) -> bool:
    """Server-side mirror of session.js assertDisguiseSafe. Raises on any leak."""
    if not isinstance(payload, dict):
        raise ValueError("disguise: payload must be an object")
    for k in _FORBIDDEN_TOP:
        if k in payload:
            raise ValueError(
                f"disguise violation: payload must not carry a '{k}' boolean"
            )
    coach = payload.get("coach")
    if isinstance(coach, dict):
        for k in _FORBIDDEN_COACH:
            if k in coach:
                raise ValueError(
                    f"disguise violation: coach bundle must not carry a '{k}' boolean"
                )
    return True


def build_session_payload(
    driver_id: str,
    driver_name: str,
    truck_no: int,
    flags: ResolvedFlags,
    next_stop: Optional[dict],
    route_cluster_id: Optional[int],
    phase: Optional[str] = None,
) -> dict:
    """Assemble the disguise-safe session payload the client consumes.

    `flags.coached` / `flags.arrival_voice` are the SERVER decision (never shipped
    as booleans) — they only gate whether the coach bundle / arrivalCue asset is
    PRESENT. `phase` is the server-computed motion phase (info the shell may seed
    the layout with); it is layout state, not a coaching flag.
    """
    payload = {
        "driver": {"id": driver_id, "name": driver_name},
        "truck": truck_no,
        "route": {"nextStop": build_next_stop(next_stop, route_cluster_id)},
    }
    if phase is not None:
        payload["phase"] = phase
    if flags.coached:
        # the per-stop grade (1|2) rides INSIDE the coach bundle. Read it from the
        # resolved stop; a coached stop with no computed grade (no route / missing
        # MR data) falls back to the safe default 2 — never a fake "1".
        grade = _DEFAULT_GRADE
        if next_stop and isinstance(next_stop.get("grade"), int) and next_stop["grade"] in (1, 2):
            grade = next_stop["grade"]
        payload["coach"] = _coach_bundle(flags.arrival_voice, grade)
    # NOMINAL: no `coach` key at all — absence == nominal, nothing to leak (and so
    # no grade value on the wire for a nominal driver to diff against a coached peer).
    assert_disguise_safe(payload)
    return payload


def build_route_payload(
    next_stop: Optional[dict],
    route_cluster_id: Optional[int],
    phase: str,
    coached: bool,
    shift_phase: Optional[str] = None,
    route_count: Optional[str] = None,
) -> dict:
    """The disguise-safe body for GET /driver-coach/v1/route (the live poll).

    Ships the CURRENT route state the client re-renders each tick:
      - route.nextStop  (no coordinates on the wire — disguise, like signin)
      - phase           the server-computed live motion phase (driving/arriving/parked)
      - shiftPhase      §11 shift phase (plan/tail/wrap) — layout state, not a flag
      - routeCount      the plan-strip "N / M" count (or absent)
      - coach.nextStopGrade  the REAL updated grade (1|2) — ONLY when coached, and
                             ONLY inside the coach bundle (present-iff-coached), so a
                             nominal poll carries NO grade value to diff. Mirrors the
                             signin disguise boundary exactly.

    A NOMINAL session's poll has no `coach` key — identical disguise contract as
    build_session_payload. assert_disguise_safe re-checks the top-level forbidden set.
    """
    payload: dict = {
        "route": {"nextStop": build_next_stop(next_stop, route_cluster_id)},
        "phase": phase,
    }
    if shift_phase is not None:
        payload["shiftPhase"] = shift_phase
    if route_count is not None:
        payload["routeCount"] = route_count
    if coached:
        grade = _DEFAULT_GRADE
        if next_stop and isinstance(next_stop.get("grade"), int) and next_stop["grade"] in (1, 2):
            grade = next_stop["grade"]
        # grade rides INSIDE the coach bundle (present-iff-coached), never top level.
        payload["coach"] = {"nextStopGrade": grade}
    assert_disguise_safe(payload)
    return payload

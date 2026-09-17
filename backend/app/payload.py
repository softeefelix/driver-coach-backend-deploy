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

import datetime
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

# DISGUISE RULE CHANGE (LIVE MAP brief §2, deliberate + scoped): the "no coordinates
# on the wire" rule existed to hide grade/coached status. The live nav MAP is shown
# IDENTICALLY to every driver (pure navigation), so its coordinates leak NO coaching.
# Coordinates ARE therefore allowed inside the `map` (and `route` nav) object — the
# `map` object must be byte-identical between a coached and a nominal sign-in for the
# same truck. The grade/coached/voice keys above stay forbidden everywhere, and grade
# still rides ONLY in the coach bundle. `map` carries none of the forbidden keys.
_MAP_ALLOWED_KEYS = ("truck", "line", "stops")

# Safe default grade when a coached stop has no computed grade (no route / missing
# MR data): 2 = "needs work"/cultivate. Never fake a 1 ("camp here") on missing data.
_DEFAULT_GRADE = 2


def fmt_clock_ampm(value) -> Optional[str]:
    """Normalize a BOOKED/leave-by timetable value to a 12-hour 'H:MM AM/PM' string,
    America/Los_Angeles convention (Felix road-test: cab-facing times are never 24h
    '20:30'). Accepts a datetime/time, or a string like '20:30' / '8:30 PM'. A value
    already carrying AM/PM (or that can't be parsed as a clock) is returned unchanged
    (never mangled); None stays None. No zero-pad on the hour.
    """
    if value is None:
        return None
    # datetime / time objects -> format directly.
    if isinstance(value, (datetime.datetime, datetime.time)):
        hour24, minute = value.hour, value.minute
    else:
        s = str(value).strip()
        if not s:
            return None
        up = s.upper()
        if "AM" in up or "PM" in up:
            return s  # already 12-hour; leave it exactly as-is
        # accept "HH:MM[:SS]" (24h). Anything else is returned verbatim.
        parts = s.split(":")
        if len(parts) < 2:
            return s
        try:
            hour24 = int(parts[0])
            minute = int(parts[1])
        except ValueError:
            return s
        if not (0 <= hour24 <= 23 and 0 <= minute <= 59):
            return s
    hour = hour24 % 12 or 12
    ampm = "AM" if hour24 < 12 else "PM"
    return f"{hour}:{minute:02d} {ampm}"


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


# Human-facing stop NAME cap. The friendly name is the STREET, not the house
# number(s) — a multi-unit cluster's first address segment is a grotesque ';'-joined
# list ('2601;2603;...;2651'), which must never reach the cab screen. Names are
# capped (ellipsis included) so nothing over ~this length or carrying ';' ships.
_NAME_MAX = 28


def _cap_name(s: str) -> str:
    """Trim a name to _NAME_MAX chars (ellipsis counted), never mid-run of spaces."""
    if len(s) > _NAME_MAX:
        return s[: _NAME_MAX - 1].rstrip() + "\u2026"
    return s


def _looks_like_house_numbers(seg: str) -> bool:
    """True iff `seg` is ONLY house number(s): a single number, or a ';'-joined list of
    numbers (a multi-unit cluster like '2601;2603;...;2651'), each optionally carrying a
    unit letter/dash suffix ('12A', '12-14'). This is what distinguishes a raw house-
    number first segment (hide it, show the street) from a real landmark first segment
    like 'Lincoln Elementary' (keep it). Empty -> False."""
    seg = seg.strip()
    if not seg:
        return False
    for part in seg.split(";"):
        part = part.strip()
        if not part:
            continue
        if not part[0].isdigit():
            return False
        if not all(c.isdigit() or c.isalpha() or c == "-" for c in part):
            return False
    return True


def friendly_stop_name(address: Optional[str]) -> Optional[str]:
    """The human-facing stop NAME derived from a full address string.

    Master-Route addresses are '<house#(s)>, <street>, <city>, …'. When the 1st segment
    is the house number(s) — which for a multi-unit cluster is a grotesque ';'-joined
    list ('2601;2603;...;2651') that must NEVER render as a stop name — the friendly name
    is the STREET (the 2nd comma-segment): 'Panama Street', 'Barrington Court', 'Corsair
    Boulevard'. When the 1st segment is a real landmark name ('Lincoln Elementary') it is
    kept as-is rather than replaced by the street.

    Falls back to the 1st segment only when there is no real 2nd segment, and even then
    never emits a ';'-joined number string: a ';'-bearing first segment collapses to
    'first-number …'. Every result is capped at _NAME_MAX chars. Returns None only for an
    empty address (callers substitute 'Next stop'). `sub`/full address is kept by the
    caller — this is a pure formatting helper.
    """
    if not address:
        return None
    segments = [seg.strip() for seg in str(address).split(",")]
    first = segments[0] if segments else ""
    street = segments[1] if len(segments) >= 2 else ""
    # The street (2nd segment) wins only when the 1st segment is a raw house number —
    # otherwise a landmark first segment ('Lincoln Elementary') is the real name.
    if _looks_like_house_numbers(first) and street:
        return _cap_name(street) or None
    if first:
        # NEVER emit a ';'-joined house-number string as a name.
        if ";" in first:
            first = first.split(";")[0].strip() + "\u2026"
        return _cap_name(first) or None
    if street:
        return _cap_name(street) or None
    return None


def _live_eta_fields(live_eta: Optional[dict]) -> dict:
    """Normalize a live-ETA dict into the camelCase wire fields, or {} when there is
    no usable ETA. Accepts either the eta.py shape ({eta_min,dist_mi,arrive_est}) or a
    stop-carried shape (same keys). Missing/None -> {} so the client shows "—", never a
    stale/fake number. All three must be present together or none ship."""
    if not isinstance(live_eta, dict):
        return {}
    eta_min = live_eta.get("eta_min")
    dist_mi = live_eta.get("dist_mi")
    arrive_est = live_eta.get("arrive_est")
    if eta_min is None or dist_mi is None or arrive_est is None:
        return {}
    return {"etaMin": eta_min, "distMi": dist_mi, "arriveEst": arrive_est}


def build_next_stop(
    stop: Optional[dict],
    route_cluster_id: Optional[int],
    live_eta: Optional[dict] = None,
) -> Optional[dict]:
    """Map a resolved route next-stop to the client route.nextStop shape.
    Coordinates are NOT shipped to the client (disguise: the app carries no lat/lng
    for the truck; the motion phase is computed server-side). Only human-facing
    route text goes on the wire.

    `arrive`/`leaveBy` are BOOKED timetable values (route_timed_stops.arrive) — the
    client labels them BOOKED so a schedule time never masquerades as an ETA. The
    LIVE traffic-aware fields are separate and OPTIONAL: `etaMin` (minutes) + `distMi`
    (miles) form the "8 min · 2.3 mi" drive line, and `arriveEst` ("H:MM" Pacific) is
    the est-arrival distinct from BOOKED. `live_eta` may be passed explicitly, else it
    is read off the stop (routes._attach_live_eta sets eta_min/dist_mi/arrive_est). When
    there is no live ETA the three keys are OMITTED ENTIRELY (client shows "—"), never a
    stale or fake number. These are operational nav facts on the BASE stop, both modes.
    """
    if not stop:
        return None
    addr = stop.get("address")
    # friendly stop NAME = the STREET (not house numbers); sub keeps the full address.
    name = friendly_stop_name(addr)
    # live_eta param wins; otherwise derive it from the stop's own eta_* keys.
    eta_src = live_eta if live_eta is not None else stop
    ns = {
        "name": name or "Next stop",
        "sub": addr,
        # BOOKED schedule time + leave-by, normalized to 12-hour AM/PM (cab-facing
        # times are never 24h "20:30"). The client labels `arrive` as BOOKED.
        "arrive": fmt_clock_ampm(stop.get("arrive")),      # BOOKED schedule time
        "leaveBy": fmt_clock_ampm(stop.get("leave_by")),
    }
    ns.update(_live_eta_fields(eta_src))   # etaMin/distMi/arriveEst — only when present
    return ns


def assert_disguise_safe(payload: dict) -> bool:
    """Server-side mirror of session.js assertDisguiseSafe. Raises on any leak.

    The `map` nav object is ALLOWED to carry coordinates (LIVE MAP brief §2) — it is
    pure navigation shown identically to every driver — but it must carry NONE of the
    forbidden grade/coached/voice keys, so a coached and a nominal map for the same
    truck are byte-identical.
    """
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
    # The map nav object may carry coords, but never a grade/coached/voice key.
    nav = payload.get("map")
    if isinstance(nav, dict):
        for k in _FORBIDDEN_TOP + _FORBIDDEN_COACH:
            if k in nav:
                raise ValueError(
                    f"disguise violation: map nav object must not carry a '{k}' key"
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
    live_eta: Optional[dict] = None,
) -> dict:
    """Assemble the disguise-safe session payload the client consumes.

    `flags.coached` / `flags.arrival_voice` are the SERVER decision (never shipped
    as booleans) — they only gate whether the coach bundle / arrivalCue asset is
    PRESENT. `phase` is the server-computed motion phase (info the shell may seed
    the layout with); it is layout state, not a coaching flag. `live_eta` is the
    traffic-aware ETA on the BASE nextStop (defaults to whatever the stop carries).
    """
    payload = {
        "driver": {"id": driver_id, "name": driver_name},
        "truck": truck_no,
        "route": {"nextStop": build_next_stop(next_stop, route_cluster_id, live_eta)},
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
    live_eta: Optional[dict] = None,
    up_next: Optional[list] = None,
    map_nav: Optional[dict] = None,
    mapbox_token: Optional[str] = None,
) -> dict:
    """The disguise-safe body for GET /driver-coach/v1/route (the live poll).

    Ships the CURRENT route state the client re-renders each tick:
      - route.nextStop  (no coordinates on the wire — disguise, like signin). Carries
                         the LIVE traffic-aware ETA (etaMin/distMi/arriveEst) on the
                         BASE stop when `live_eta` (or the stop's own eta_* keys) has
                         one — an OPERATIONAL nav fact, identical in both modes; ABSENT
                         when there is no ETA so the client shows "—", never a fake.
      - phase           the server-computed live motion phase (driving/arriving/parked)
      - shiftPhase      §11 shift phase (plan/tail/wrap) — layout state, not a flag
      - routeCount      the plan-strip "N / M" count (or absent)
      - map             LIVE MAP nav object {truck, line?, stops} — pure navigation,
                        byte-IDENTICAL in both modes (the disguise rule change). It MAY
                        carry coordinates (it needs them to draw), but never a grade/
                        coached/voice key. Absent when there is no live position (client
                        falls back to the simple view).
      - mapboxToken     the PUBLIC (pk.) Mapbox token the client's Mapbox GL JS map
                        inits with — fine to expose; absent/omitted when unset.
      - coach.nextStopGrade  the REAL updated grade (1|2) — ONLY when coached, and
                             ONLY inside the coach bundle (present-iff-coached), so a
                             nominal poll carries NO grade value to diff. Mirrors the
                             signin disguise boundary exactly.

    A NOMINAL session's poll has no `coach` key — identical disguise contract as
    build_session_payload. assert_disguise_safe re-checks the top-level forbidden set
    AND that the `map` object carries none of the forbidden keys.
    """
    payload: dict = {
        "route": {"nextStop": build_next_stop(next_stop, route_cluster_id, live_eta)},
        "phase": phase,
    }
    if shift_phase is not None:
        payload["shiftPhase"] = shift_phase
    if route_count is not None:
        payload["routeCount"] = route_count
    # REAL forward queue for the "Up next" list. Each entry is {name, arrive} with the
    # booked time normalized to 12-hour AM/PM. Present ONLY when there are real upcoming
    # stops (empty list omitted) so the client hides the list rather than showing a fake
    # one. Carries NO coordinates and NO grade — plain human-facing route text, both modes.
    if up_next:
        payload["upNext"] = [
            {"name": s.get("name"), "arrive": fmt_clock_ampm(s.get("arrive"))}
            for s in up_next
        ]
    # LIVE MAP nav object — pure navigation, byte-identical both modes. Present ONLY
    # when the server built one (there is a live truck position); absent -> the client
    # falls back to the simple next-stop focal. It may carry coordinates but NEVER a
    # grade/coached/voice key (assert_disguise_safe re-checks this below).
    if map_nav:
        payload["map"] = map_nav
    # The PUBLIC Mapbox token for the client's map (pk. — safe to expose). Omitted
    # when unset so the client just falls back to the simple view.
    if mapbox_token:
        payload["mapboxToken"] = mapbox_token
    if coached:
        grade = _DEFAULT_GRADE
        if next_stop and isinstance(next_stop.get("grade"), int) and next_stop["grade"] in (1, 2):
            grade = next_stop["grade"]
        # grade rides INSIDE the coach bundle (present-iff-coached), never top level.
        payload["coach"] = {"nextStopGrade": grade}
    assert_disguise_safe(payload)
    return payload

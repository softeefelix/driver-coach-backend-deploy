"""Driver Coach — Geotab motion-state classifier (build-input reference core).

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.

Drives the motion-switched layout (DESIGN_DIRECTION.md §1c-1, §7 rows 1-2):
  driving  -> nav-dominant, chip HIDDEN, voice-first
  arriving -> last ~0.2 mi to next stop: nav still owns screen, chip ring appears
  parked   -> nav hides, 190px grade chip + info + actions

Speed truth is GEOTAB (server, ~2 min cadence, brief §9) — NOT iPad GPS — because
Geotab is the position/ignition source of record and can't be spoofed from the
iPad (same independence argument as the turn-off spec). The classifier is a small
hysteresis state machine so a truck idling at a red light mid-block never flips the
whole screen to `parked`, and GPS jitter at a stop never flaps chip visibility.

Pure/stdlib. Distances are supplied pre-computed (meters to next planned stop) so
this core carries no geo dependency; the wiring layer computes distance-to-stop
from Geotab position + the master-route next-stop coordinate.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# ----------------------------------------------------------------------------
# Thresholds (defaults, tunable at live-review against real fleet traces).
# Speeds in mph; distances in meters. Ice-cream trucks creep, so bars are low.
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class MotionConfig:
    # driving <-> stopped speed band (hysteresis: must clearly move to leave a stop,
    # must clearly settle to enter one)
    moving_speed_mph: float = 6.0        # >= this => unambiguously driving
    settled_speed_mph: float = 2.0       # <= this => effectively stopped

    # arriving geofence around the next planned stop
    arriving_radius_m: float = 320.0     # ~0.2 mi (brief/design "last ~0.2 mi")
    parked_radius_m: float = 40.0        # within this + settled => parked at the stop

    # dwell timers (seconds) — debounce transient states
    settle_to_park_s: float = 15.0       # stopped this long inside stop radius => parked
    move_to_drive_s: float = 6.0         # moving this long => driving (leave parked/arriving)
    arriving_min_dwell_s: float = 0.0    # arriving can be instantaneous on geofence entry

    # freshness: Geotab fix older than this can't be trusted to flip layouts
    max_fix_age_s: float = 150.0         # ~one Geotab cadence + margin


PARKED, ARRIVING, DRIVING = "parked", "arriving", "driving"


@dataclass
class GeotabFix:
    speed_mph: float
    dist_to_next_stop_m: Optional[float]   # meters to next planned stop; None if unknown
    fix_age_s: float = 0.0                 # age of this Geotab reading
    ignition_on: bool = True


@dataclass
class MotionState:
    """Carried between fixes (server- or client-side; the machine is deterministic)."""
    phase: str = DRIVING                   # current committed layout phase
    settled_for_s: float = 0.0             # accumulated time at/under settled speed
    moving_for_s: float = 0.0              # accumulated time at/over moving speed


@dataclass
class MotionResult:
    phase: str
    changed: bool
    reason: dict = field(default_factory=dict)
    next_state: MotionState = field(default_factory=MotionState)

    def as_dict(self) -> dict:
        return asdict(self)


def step(
    fix: GeotabFix,
    state: Optional[MotionState] = None,
    dt_s: float = 20.0,
    cfg: Optional[MotionConfig] = None,
) -> MotionResult:
    """Advance the motion state machine by one Geotab fix.

    dt_s = seconds since the previous fix (drives the dwell timers). Defaults to
    the heartbeat cadence; the wiring layer passes the true delta.
    """
    cfg = cfg or MotionConfig()
    state = state or MotionState()
    prev = state.phase
    reason: dict = {}

    # --- 0) stale / ignition-off safety: hold last phase, don't flap ---------
    if fix.fix_age_s > cfg.max_fix_age_s:
        reason["rule"] = f"stale_fix:{fix.fix_age_s}s>{cfg.max_fix_age_s}s_hold"
        return MotionResult(prev, False, reason, MotionState(prev, 0.0, 0.0))

    # --- 1) integrate speed dwell timers ------------------------------------
    is_moving = fix.speed_mph >= cfg.moving_speed_mph
    is_settled = fix.speed_mph <= cfg.settled_speed_mph
    moving_for = (state.moving_for_s + dt_s) if is_moving else 0.0
    settled_for = (state.settled_for_s + dt_s) if is_settled else 0.0

    d = fix.dist_to_next_stop_m
    near_stop = (d is not None) and (d <= cfg.parked_radius_m)
    in_arriving = (d is not None) and (d <= cfg.arriving_radius_m)

    # --- 2) transition logic (priority: driving > parked > arriving) --------
    # A clearly-moving truck is ALWAYS driving — nav must dominate (safety law):
    # chip is hidden while driving regardless of distance.
    if moving_for >= cfg.move_to_drive_s:
        phase = DRIVING
        reason["rule"] = f"moving:{fix.speed_mph}mph_for_{moving_for}s"
    # Settled + within the stop radius long enough => parked (full chip layout).
    elif near_stop and settled_for >= cfg.settle_to_park_s:
        phase = PARKED
        reason["rule"] = (
            f"parked:d={d}m<={cfg.parked_radius_m}_settled_{settled_for}s"
        )
    # Inside the arriving geofence, not yet committed to parked => arriving.
    elif in_arriving:
        phase = ARRIVING
        reason["rule"] = f"arriving:d={d}m<={cfg.arriving_radius_m}"
    else:
        # Not clearly moving, not near a stop, not in geofence: hold prior phase
        # rather than flap (e.g. brief idle mid-block at a light).
        phase = prev if prev in (DRIVING, ARRIVING, PARKED) else DRIVING
        reason["rule"] = f"hold_prior:{phase}_speed={fix.speed_mph}_d={d}"

    reason["inputs"] = {
        "speed_mph": fix.speed_mph,
        "dist_to_next_stop_m": d,
        "moving_for_s": moving_for,
        "settled_for_s": settled_for,
        "ignition_on": fix.ignition_on,
    }

    return MotionResult(
        phase=phase,
        changed=(phase != prev),
        reason=reason,
        next_state=MotionState(
            phase=phase,
            settled_for_s=settled_for,
            moving_for_s=moving_for,
        ),
    )

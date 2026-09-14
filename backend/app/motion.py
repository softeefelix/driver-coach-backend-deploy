"""Server-side motion classify path — shares the pure reference `step()` verbatim.

The session/heartbeat can carry a motion `phase` computed server-side from Geotab
speed + distance-to-next-stop (SERVER_FLAGS spec §4). The pure function is the
reference core (motion_state.step) — this module ONLY wires real inputs into it
and carries the small state between fixes; it never reimplements the logic (the
same code runs client-side offline, so both sides agree — spec §4.5).

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

from typing import Optional

from .config import motion_config
from .refcore import DRIVING, GeotabFix, MotionResult, MotionState, motion_step


def classify(
    fix: GeotabFix,
    state: Optional[MotionState] = None,
    dt_s: float = 20.0,
) -> MotionResult:
    """Advance the shared motion state machine by one real Geotab fix, using the
    thresholds.json-pinned MotionConfig (asserted parity with the client)."""
    return motion_step(fix, state=state, dt_s=dt_s, cfg=motion_config())


def phase_for_fix(fix: Optional[GeotabFix], prior_phase: str = DRIVING) -> str:
    """One-shot phase for a session/heartbeat when we don't carry dwell state.

    No fix -> hold prior (fail-safe). With a fix, seed the state at the prior phase
    and step once; the core's hysteresis + stale-hold rules make this safe.
    """
    if fix is None:
        return prior_phase
    r = classify(fix, state=MotionState(phase=prior_phase))
    return r.phase

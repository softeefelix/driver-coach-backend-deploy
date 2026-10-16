"""Config: load config/thresholds.json (the SINGLE SOURCE OF TRUTH the client,
the reference cores, and this service all share) and expose the flag/motion
configs built from it — so the server can never silently drift from the app.

The 5 open decisions (SERVER_FLAGS spec §7) ship here as documented, tunable
DEFAULTS — never hard-coded at a call site:
  - nominal_tiers = {STAR}
  - new-hire floor = 90 days
  - voice_deadband = 2.0
  - the 7 motion thresholds come straight from thresholds.json (asserted, not forked)
  - offline = run the shared classifier locally and reconcile (client behavior;
    the pure motion function is shared verbatim so both sides agree)

All fail-safe to "coached + voice on" / "hold last phase" (the cores enforce this).

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

from .refcore import REPO_ROOT, FlagConfig, MotionConfig

_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "thresholds.json")


@lru_cache(maxsize=1)
def load_thresholds() -> dict:
    """Load the shared thresholds.json (client + server single source of truth)."""
    with open(_CONFIG_PATH) as fh:
        return json.load(fh)


def motion_config() -> MotionConfig:
    """MotionConfig with the 7 thresholds pinned from thresholds.json (asserted
    equal to the dataclass defaults by test_config_parity.py — no fork)."""
    m = load_thresholds()["motion"]
    return MotionConfig(
        moving_speed_mph=m["moving_speed_mph"],
        settled_speed_mph=m["settled_speed_mph"],
        arriving_radius_m=m["arriving_radius_m"],
        parked_radius_m=m["parked_radius_m"],
        settle_to_park_s=m["settle_to_park_s"],
        move_to_drive_s=m["move_to_drive_s"],
        arriving_min_dwell_s=m["arriving_min_dwell_s"],
        max_fix_age_s=m["max_fix_age_s"],
    )


def flag_config() -> FlagConfig:
    """FlagConfig with the people-facing voice thresholds pinned from
    thresholds.json; the coached-vs-nominal policy uses the spec-§7 defaults
    ({STAR} only, 90d floor) which the FlagConfig dataclass already carries."""
    f = load_thresholds()["flags"]
    return FlagConfig(
        voice_stop_threshold=f["voice_stop_threshold"],
        voice_deadband=f["voice_deadband"],
    )


def liveness_config() -> dict:
    """Liveness/turn-off timing constants (turn-off spec §7), from thresholds.json."""
    return dict(load_thresholds()["liveness"])


def _number(raw: dict, key: str, default: float, *, minimum: float) -> float:
    try:
        value = float(raw.get(key, default))
    except (TypeError, ValueError):
        return float(default)
    if value < minimum:
        return float(default)
    return value


def _whole(raw: dict, key: str, default: int, *, minimum: int) -> int:
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError):
        return int(default)
    if value < minimum:
        return int(default)
    return value


def no_idle_settings() -> dict:
    """No-idle nudge timing from thresholds.json.

    ``off_stop_grace_s`` defaults to the motion settle-to-park debounce (15s): a
    truck parked away from every planned pin is nudged after that grace, not after
    the full sale window. Escalation is measured from the first banner.
    """
    raw = load_thresholds().get("no_idle") or {}
    voice = _number(raw, "voice_after_s", 60, minimum=0)
    manager = _number(raw, "manager_alert_after_s", 180, minimum=0)
    if manager < voice:
        manager = voice
    return {
        "no_sale_move_on_s": _number(raw, "no_sale_move_on_s", 90, minimum=0),
        "off_stop_grace_s": _number(raw, "off_stop_grace_s", 15, minimum=0),
        "voice_after_s": voice,
        "manager_alert_after_s": manager,
    }


def break_settings() -> dict:
    """Per-shift lunch/break allowances from thresholds.json.

    One 30-minute lunch and two 15-minute breaks. ``overrun_grace_s`` is how far
    past the allotment a break may run (poll lag) before it is flagged.
    """
    raw = load_thresholds().get("breaks") or {}
    return {
        "lunch_duration_s": _whole(raw, "lunch_duration_s", 1800, minimum=1),
        "break_duration_s": _whole(raw, "break_duration_s", 900, minimum=1),
        "lunch_allowance": _whole(raw, "lunch_allowance", 1, minimum=0),
        "break_allowance": _whole(raw, "break_allowance", 2, minimum=0),
        "overrun_grace_s": _whole(raw, "overrun_grace_s", 60, minimum=0),
    }


def max_fix_age_s() -> float:
    """Oldest Geotab fix that may drive a no-idle nudge or a movement break-end.

    ``DRIVER_COACH_MAX_FIX_AGE_S`` overrides the shared motion ``max_fix_age_s``
    (150s). A non-numeric or negative override is ignored.
    """
    raw = os.environ.get("DRIVER_COACH_MAX_FIX_AGE_S")
    if raw is not None and raw.strip() != "":
        try:
            value = float(raw)
        except ValueError:
            value = -1.0
        if value >= 0:
            return value
    return float(motion_config().max_fix_age_s)


def mapbox_token() -> str:
    """The Mapbox access token for the traffic-aware ETA service (eta.py).

    Read from the environment (`MAPBOX_TOKEN`) — NEVER hard-coded, never committed.
    Felipe sets it on the Render service env (brief §B); the fleet's working token
    lives in ~/.hermes/secrets/mapbox.env for local build/test. An unset/blank token
    means the ETA service degrades gracefully to null (the client shows "—"), so this
    returns "" rather than raising — a missing token must never crash the route feed.
    """
    return (os.environ.get("MAPBOX_TOKEN") or "").strip()

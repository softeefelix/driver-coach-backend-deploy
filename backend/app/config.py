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

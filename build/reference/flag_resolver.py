"""Driver Coach — server-side flag resolver (build-input reference core).

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.

This is the PURE decision core for the two server-side flags the iPad app
consumes but must never compute or reveal (brief PRODUCT_BRIEF.md v0.4 §3 disguise,
§13 adaptive voice; DESIGN_DIRECTION.md §1d/§7 row 9):

  1. coached        — coached layer on/off (vs nominal info/nav-only)
  2. arrival_voice  — adaptive 40-stop arrival-voice rule (coached only)

It is intentionally dependency-free (stdlib) and side-effect-free so it can be
unit-tested against synthetic driver-performance snapshots and later wired to the
real stack (`~/projects/work/MisterSoftee/driver-performance/driver_profiles.py`
tier/stops_per_day) without change. It is resolved ONCE at sign-in and frozen for
the day (see spec §2.3). The app never receives a raw `coached` boolean on the
wire — the server ships resolved *content*; this core produces the server-side
decision only (spec §2.4 disguise).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# ----------------------------------------------------------------------------
# Tunable thresholds (defaults; all owned by Felix for the people-facing ones).
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class FlagConfig:
    # --- coached vs nominal (brief §3) ---
    # Everyone starts coached; only PROVEN top performers drift to nominal.
    nominal_tiers: tuple = ("STAR",)          # tier(s) eligible for nominal at all
    min_tenure_days_for_nominal: int = 90     # new hires auto-coached >= this first
    nominal_streak_days: int = 30             # must hold an eligible tier this long
    recoach_streak_days: int = 5              # drop below SOLID this long -> re-coach
    demote_below_tiers: tuple = (             # tiers that count as "slipped" for recoach
        "REPLACE-FIRST REVIEW", "UNDERPERFORMING", "MIDDLE",
    )

    # --- adaptive arrival voice (brief §13, Felix 2026-09-05) ---
    voice_stop_threshold: float = 40.0        # rolling avg stops/day; >= -> silent
    voice_deadband: float = 2.0               # hysteresis band around the line

    # --- freshness / safety ---
    max_profile_age_days: int = 3             # stale snapshot -> conservative defaults


@dataclass
class DriverProfile:
    """Latest completed daily snapshot from the driver-performance stack.

    Field names mirror driver_profiles.py output rows.
    """
    driver_id: str
    tier: Optional[str] = None                # STAR/SOLID/MIDDLE/UNDERPERFORMING/...
    days_worked: int = 0
    stops_per_day: Optional[float] = None     # trailing-window street stops/day
    status: str = "ACTIVE"                    # ACTIVE / INACTIVE
    profile_age_days: int = 0                 # age of this snapshot at sign-in


@dataclass
class PriorFlagState:
    """Server-persisted stickiness carried across days (spec §2.2 hysteresis)."""
    coached: bool = True                      # default: everyone coached
    eligible_streak_days: int = 0             # consecutive days in a nominal-eligible tier
    slipped_streak_days: int = 0              # consecutive days slipped below SOLID


@dataclass
class ResolvedFlags:
    coached: bool
    arrival_voice: bool
    # audit trail (mirrors turn-off spec evidence jsonb — every flag is defensible)
    reason: dict = field(default_factory=dict)
    next_state: PriorFlagState = field(default_factory=PriorFlagState)

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


_SOLID_OR_BETTER = {"SOLID", "STAR"}


def resolve_flags(
    profile: DriverProfile,
    prior: Optional[PriorFlagState] = None,
    cfg: Optional[FlagConfig] = None,
) -> ResolvedFlags:
    """Resolve (coached, arrival_voice) for one driver at sign-in.

    Conservative by construction: unknown/new/stale -> coached + voice on.
    Nominal is *earned and sticky*; coached is the safe default.
    """
    cfg = cfg or FlagConfig()
    prior = prior or PriorFlagState()
    reason: dict = {}

    # --- 0) staleness / safety gate -----------------------------------------
    stale = profile.profile_age_days > cfg.max_profile_age_days
    unknown = profile.tier in (None, "LIGHT SAMPLE") or profile.status != "ACTIVE"
    new_hire = profile.days_worked < cfg.min_tenure_days_for_nominal

    # --- 1) update stickiness counters --------------------------------------
    eligible = (
        not stale
        and not unknown
        and not new_hire
        and profile.tier in cfg.nominal_tiers
    )
    slipped = (not stale) and (profile.tier in cfg.demote_below_tiers)

    eligible_streak = (prior.eligible_streak_days + 1) if eligible else 0
    slipped_streak = (prior.slipped_streak_days + 1) if slipped else 0

    # --- 2) coached decision (sticky hysteresis) ----------------------------
    if stale:
        coached = True
        reason["coached_rule"] = "stale_snapshot_conservative_default"
    elif unknown or new_hire:
        coached = True
        reason["coached_rule"] = (
            "new_hire_or_light_sample" if (new_hire or unknown) else "default"
        )
    elif prior.coached:
        # currently coached: earn nominal only after a sustained eligible streak
        if eligible_streak >= cfg.nominal_streak_days:
            coached = False
            reason["coached_rule"] = (
                f"earned_nominal:{profile.tier}_streak>={cfg.nominal_streak_days}"
            )
        else:
            coached = True
            reason["coached_rule"] = (
                f"coached:eligible_streak={eligible_streak}/{cfg.nominal_streak_days}"
            )
    else:
        # currently nominal: stay nominal until a sustained slip
        if slipped_streak >= cfg.recoach_streak_days:
            coached = True
            reason["coached_rule"] = (
                f"recoached:slipped_streak>={cfg.recoach_streak_days}"
            )
        else:
            coached = False
            reason["coached_rule"] = (
                f"nominal:slipped_streak={slipped_streak}/{cfg.recoach_streak_days}"
            )

    # --- 3) adaptive arrival voice (coached only) ---------------------------
    # Nominal has no coach at all -> voice is irrelevant; report False.
    if not coached:
        arrival_voice = False
        reason["voice_rule"] = "nominal_no_coach"
    elif profile.stops_per_day is None:
        arrival_voice = True
        reason["voice_rule"] = "unknown_pace_conservative_voice_on"
    else:
        # deadband hysteresis around the 40-stop line: silence only clears the
        # bar with margin; voice re-engages the moment pace dips below it.
        lo = cfg.voice_stop_threshold
        hi = cfg.voice_stop_threshold + cfg.voice_deadband
        if profile.stops_per_day >= hi:
            arrival_voice = False
            reason["voice_rule"] = f"fast:{profile.stops_per_day}>={hi}_silent"
        elif profile.stops_per_day < lo:
            arrival_voice = True
            reason["voice_rule"] = f"slow:{profile.stops_per_day}<{lo}_voice"
        else:
            # inside the deadband -> hold prior UX bias: voice on (conservative)
            arrival_voice = True
            reason["voice_rule"] = (
                f"deadband:{lo}<= {profile.stops_per_day} <{hi}_voice_on"
            )

    reason["inputs"] = {
        "tier": profile.tier,
        "days_worked": profile.days_worked,
        "stops_per_day": profile.stops_per_day,
        "status": profile.status,
        "profile_age_days": profile.profile_age_days,
        "stale": stale,
        "new_hire": new_hire,
    }

    return ResolvedFlags(
        coached=coached,
        arrival_voice=arrival_voice,
        reason=reason,
        next_state=PriorFlagState(
            coached=coached,
            eligible_streak_days=eligible_streak,
            slipped_streak_days=slipped_streak,
        ),
    )

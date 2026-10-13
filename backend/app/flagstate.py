"""PriorFlagState persistence — the hysteresis carried across days (SERVER_FLAGS
spec §2.2). Reads the driver's last state before resolve_flags(), writes the
resolved next_state after. Lives in the isolated driver_coach_flag_state table.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .refcore import (
    DriverProfile,
    FlagConfig,
    PriorFlagState,
    ResolvedFlags,
    resolve_flags,
)

_PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_day(moment: datetime.datetime) -> datetime.date:
    """Calendar day of a sign-in in Pacific time.

    The Render host clock is UTC. A sign-in at 17:30 Pacific is 00:30 UTC the
    next day, so a UTC date would count that evening twice. Same helper the
    route code uses (routes.pacific_date): America/Los_Angeles.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(_PACIFIC).date()


def load_prior(cur, driver_id: str) -> Optional[PriorFlagState]:
    """Last persisted flag state for a driver, or None (resolver then uses the
    conservative default: coached=True, streaks 0)."""
    cur.execute(
        """
        SELECT coached, eligible_streak_days, slipped_streak_days
        FROM driver_coach_flag_state WHERE driver_id = %s
        """,
        (driver_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return PriorFlagState(
        coached=bool(row[0]),
        eligible_streak_days=int(row[1]),
        slipped_streak_days=int(row[2]),
    )


def load_row(cur, driver_id: str):
    """Persisted state plus updated_at, the no-migration day marker.

    updated_at is rewritten only when a sign-in actually advances the day, so
    its Pacific date is the day the counters last advanced. No new column:
    the coached bit and both counters are already on this row.

    Returns (PriorFlagState, updated_at) or None.
    """
    cur.execute(
        """
        SELECT coached, eligible_streak_days, slipped_streak_days, updated_at
        FROM driver_coach_flag_state WHERE driver_id = %s
        """,
        (driver_id,),
    )
    row = cur.fetchone()
    if not row or row[3] is None:
        return None
    state = PriorFlagState(
        coached=bool(row[0]),
        eligible_streak_days=int(row[1]),
        slipped_streak_days=int(row[2]),
    )
    return state, row[3]


def _held(profile: DriverProfile, prior: PriorFlagState, cfg: FlagConfig) -> ResolvedFlags:
    """The decision already frozen for this Pacific day.

    Counters and the coached bit come from the row, not from a fresh increment.
    Voice is layout, not a streak: it follows the frozen coached bit and the
    current pace, using the resolver's own thresholds, without calling it.
    The audit reason records that this sign-in did not advance the day.
    """
    if not prior.coached:
        arrival_voice = False
        voice_rule = "nominal_no_coach"
    elif profile.stops_per_day is None:
        arrival_voice = True
        voice_rule = "unknown_pace_conservative_voice_on"
    else:
        lo = cfg.voice_stop_threshold
        hi = cfg.voice_stop_threshold + cfg.voice_deadband
        if profile.stops_per_day >= hi:
            arrival_voice = False
            voice_rule = f"fast:{profile.stops_per_day}>={hi}_silent"
        else:
            # Below the bar, and inside the deadband, voice stays on. That is
            # the resolver's conservative hold, and a same-day repeat must not
            # invent a different one.
            arrival_voice = True
            if profile.stops_per_day < lo:
                voice_rule = f"slow:{profile.stops_per_day}<{lo}_voice"
            else:
                voice_rule = f"deadband:{lo}<= {profile.stops_per_day} <{hi}_voice_on"
    return ResolvedFlags(
        coached=prior.coached,
        arrival_voice=arrival_voice,
        reason={
            "coached_rule": "same_pacific_day_held",
            "voice_rule": voice_rule,
            "held": {
                "eligible_streak_days": prior.eligible_streak_days,
                "slipped_streak_days": prior.slipped_streak_days,
            },
        },
        next_state=PriorFlagState(
            coached=prior.coached,
            eligible_streak_days=prior.eligible_streak_days,
            slipped_streak_days=prior.slipped_streak_days,
        ),
    )


def advance_for_signin(
    cur,
    driver_id: str,
    profile: DriverProfile,
    cfg: FlagConfig,
    now: Optional[datetime.datetime] = None,
) -> ResolvedFlags:
    """Resolve flags for one sign-in, advancing streaks at most once per Pacific day.

    A later sign-in on the same Pacific day leaves both counters and the coached
    decision exactly as the first sign-in left them, and does not rewrite
    updated_at. It does not call the resolver (the resolver increments on every
    call). Voice for that repeat follows the frozen coached bit; the table has
    no voice column, and this card adds none. The first sign-in of a later
    Pacific day calls the reference resolver, which still increments and still
    resets a counter on a tier change.
    """
    if now is None:
        now = datetime.datetime.now(_PACIFIC)
    today = pacific_day(now)
    existing = load_row(cur, driver_id)
    if existing is not None:
        prior, updated_at = existing
        if pacific_day(updated_at) == today:
            return _held(profile, prior, cfg)
    else:
        prior = None
    flags = resolve_flags(profile, prior, cfg)
    save_state(cur, driver_id, flags.next_state, updated_at=now)
    return flags


def save_state(cur, driver_id: str, state: PriorFlagState, updated_at=None) -> None:
    """Upsert the resolved next_state for a driver (idempotent per driver).

    updated_at is the Pacific day marker. Callers that already decided this
    sign-in is a new Pacific day pass the sign-in instant; a bare save (no
    instant) stamps now(), which on the Render host is UTC and still converts
    back to the correct Pacific date on the next read.
    """
    if updated_at is None:
        stamp_sql = "now()"
        params = (
            driver_id,
            state.coached,
            state.eligible_streak_days,
            state.slipped_streak_days,
        )
    else:
        stamp_sql = "%s"
        params = (
            driver_id,
            state.coached,
            state.eligible_streak_days,
            state.slipped_streak_days,
            updated_at,
        )
    cur.execute(
        f"""
        INSERT INTO driver_coach_flag_state
            (driver_id, coached, eligible_streak_days, slipped_streak_days, updated_at)
        VALUES (%s, %s, %s, %s, {stamp_sql})
        ON CONFLICT (driver_id) DO UPDATE SET
            coached = EXCLUDED.coached,
            eligible_streak_days = EXCLUDED.eligible_streak_days,
            slipped_streak_days = EXCLUDED.slipped_streak_days,
            updated_at = EXCLUDED.updated_at
        """,
        params,
    )

"""Streak counters advance once per Pacific calendar day, not once per sign-in.

The reference resolver increments on every call and must stay byte-identical.
The day gate lives in flagstate: a later sign-in on the same Pacific day keeps
both counters and the coached decision exactly as the first sign-in left them.
The day marker is updated_at, read in America/Los_Angeles. No schema change.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import flagstate
from app.flagstate import advance_for_signin
from app.refcore import DriverProfile, FlagConfig, PriorFlagState

PT = ZoneInfo("America/Los_Angeles")

# STAR, 90+ days tenure, fresh snapshot: eligible for the nominal streak.
STAR = DriverProfile(
    driver_id="star",
    tier="STAR",
    days_worked=120,
    stops_per_day=30.0,
    status="ACTIVE",
    profile_age_days=0,
)
# MIDDLE is a slipped tier (demote_below_tiers). Tenure does not matter for slip.
SLIPPED = DriverProfile(
    driver_id="slipped",
    tier="MIDDLE",
    days_worked=200,
    stops_per_day=20.0,
    status="ACTIVE",
    profile_age_days=0,
)

CFG = FlagConfig()


def pt(*parts):
    """A timezone-aware Pacific instant. parts = year, month, day, hour, minute."""
    return datetime(*parts, tzinfo=PT)


class MemoryCursor:
    """One driver_coach_flag_state row. updated_at is timestamptz, as Postgres stores it.

    SELECT asks for four columns. A legacy row (no stamp yet) returns the stamp
    as None so the gate treats it as 'not yet counted today'.
    """

    def __init__(self):
        self.state = None
        self.saved_at = None
        self._row = None

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if text.startswith("SELECT"):
            if self.state is None:
                self._row = None
            else:
                self._row = (
                    self.state.coached,
                    self.state.eligible_streak_days,
                    self.state.slipped_streak_days,
                    self.saved_at,
                )
            return
        if text.startswith("INSERT"):
            self.state = PriorFlagState(
                coached=bool(params[1]),
                eligible_streak_days=int(params[2]),
                slipped_streak_days=int(params[3]),
            )
            # A bare save stamps now(); tests always pass an instant, which is
            # the last bind parameter.
            if len(params) == 5:
                self.saved_at = params[4]
            return
        raise AssertionError(f"unexpected SQL: {text}")

    def fetchone(self):
        return self._row


def sign_in(cur, profile, when, prior_coached=True):
    """One sign-in at an explicit Pacific instant. Returns the persisted next state."""
    state = advance_for_signin(cur, profile.driver_id, profile, CFG, now=when)
    return state.next_state


def test_five_signins_one_pacific_day_increment_once():
    """Five sign-ins on one Pacific day: both counters +1 once, coached frozen after the first."""
    cur = MemoryCursor()
    day = pt(2026, 10, 12, 9, 0)
    first = sign_in(cur, STAR, day)
    assert first.eligible_streak_days == 1
    assert first.slipped_streak_days == 0
    assert first.coached is True

    for hour in (10, 12, 15, 20):
        again = sign_in(cur, STAR, day.replace(hour=hour))
        assert again.eligible_streak_days == 1
        assert again.slipped_streak_days == 0
        assert again.coached is first.coached


def test_same_pacific_day_does_not_call_the_resolver():
    """The reference resolver increments on every call. A same-day repeat must
    not call it at all, or the counter would move even if we discarded the result."""
    cur = MemoryCursor()
    when = pt(2026, 10, 12, 9, 0)
    first = sign_in(cur, STAR, when)
    assert first.eligible_streak_days == 1

    calls = {"n": 0}
    real = flagstate.resolve_flags

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkey = counting
    flagstate.resolve_flags = monkey
    try:
        again = sign_in(cur, STAR, when.replace(hour=18))
    finally:
        flagstate.resolve_flags = real
    assert calls["n"] == 0
    assert again.eligible_streak_days == 1
    assert again.coached is True


def test_five_signins_one_pacific_day_hold_a_slipped_streak_too():
    """Same-day repeats hold the slipped counter, not only the eligible one."""
    cur = MemoryCursor()
    when = pt(2026, 10, 12, 9, 0)
    first = sign_in(cur, SLIPPED, when)
    assert first.slipped_streak_days == 1
    assert first.eligible_streak_days == 0
    held = sign_in(cur, SLIPPED, when.replace(hour=18))
    assert held.slipped_streak_days == 1
    assert held.eligible_streak_days == 0
    assert held.coached is first.coached


def test_three_consecutive_pacific_days_increment_three():
    cur = MemoryCursor()
    seen = []
    for day in (12, 13, 14):
        seen.append(sign_in(cur, STAR, pt(2026, 10, day, 8, 15)))
    assert [s.eligible_streak_days for s in seen] == [1, 2, 3]
    assert all(s.coached is True for s in seen)


def test_pacific_midnight_is_two_days_utc_midnight_is_one():
    """23:59 then 00:01 Pacific is 2 days. 17:30 and 18:30 Pacific is 1 day
    even though 17:30 Pacific is 00:30 UTC the next calendar day."""
    cur = MemoryCursor()
    first = sign_in(cur, STAR, pt(2026, 10, 12, 23, 59))
    second = sign_in(cur, STAR, pt(2026, 10, 13, 0, 1))
    assert first.eligible_streak_days == 1
    assert second.eligible_streak_days == 2

    cur = MemoryCursor()
    # 17:30 PT on 2026-10-12 is 00:30 UTC on 2026-10-13. Same Pacific day as 18:30 PT.
    early = sign_in(cur, STAR, pt(2026, 10, 12, 17, 30))
    later = sign_in(cur, STAR, pt(2026, 10, 12, 18, 30))
    assert early.eligible_streak_days == 1
    assert later.eligible_streak_days == 1
    assert later.coached is early.coached


def test_nominal_flip_on_the_30th_pacific_day_exactly_once():
    """A coached STAR earns nominal on the 30th distinct Pacific day, and a
    second sign-in that same day does not flip again or advance the counter."""
    cur = MemoryCursor()
    last = None
    for n in range(1, 30):
        last = sign_in(cur, STAR, pt(2026, 1, n, 9, 0) if n <= 28 else pt(2026, 2, n - 28, 9, 0))
        assert last.coached is True, n
        assert last.eligible_streak_days == n
    # Days 1-28 are January; 29 is Feb 1. The 30th day is 2026-02-02.
    day30 = pt(2026, 2, 2, 9, 0)
    flipped = sign_in(cur, STAR, day30)
    assert flipped.eligible_streak_days == 30
    assert flipped.coached is False
    repeat = sign_in(cur, STAR, day30.replace(hour=16, minute=45))
    assert repeat.eligible_streak_days == 30
    assert repeat.coached is False


def test_tier_change_still_resets_the_counter():
    """A tier change on a new Pacific day resets the counter that no longer applies,
    exactly as the resolver does today. Same-day tier change is still one day."""
    cur = MemoryCursor()
    built = sign_in(cur, STAR, pt(2026, 10, 12, 9, 0))
    assert built.eligible_streak_days == 1
    # Same Pacific day, tier drops: the day already counted, so nothing moves.
    held = sign_in(cur, SLIPPED, pt(2026, 10, 12, 15, 0))
    assert held.eligible_streak_days == 1
    assert held.slipped_streak_days == 0
    assert held.coached is built.coached
    # Next Pacific day in the slipped tier: eligible resets, slipped starts at 1.
    reset = sign_in(cur, SLIPPED, pt(2026, 10, 13, 9, 0))
    assert reset.eligible_streak_days == 0
    assert reset.slipped_streak_days == 1


def test_updated_at_is_compared_in_pacific_not_utc():
    """A row written at 17:30 Pacific (00:30 UTC next day) is still 'today'
    for an 18:30 Pacific sign-in. Comparing the UTC date would double-count."""
    cur = MemoryCursor()
    sign_in(cur, STAR, pt(2026, 6, 15, 17, 30))
    stored = cur.saved_at
    assert stored.astimezone(timezone.utc).date().isoformat() == "2026-06-16"
    again = sign_in(cur, STAR, pt(2026, 6, 15, 18, 30))
    assert again.eligible_streak_days == 1

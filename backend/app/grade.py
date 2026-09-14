"""Driver Coach — stop grade classifier (build-input reference core).

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.

The grade chip (`1` = good stop / `2` = needs work) is the coach's ENTIRE output
(brief PRODUCT_BRIEF.md §0; INTEGRATION_MASTER_ROUTE.md §2). This is the PURE,
dependency-free classifier that turns Master Route signals into that grade. It is
stdlib-only and side-effect-free — a thin classifier over signals MR already
computes, NOT a new metric — so it unit-tests on synthetic rows and later binds to
the real read-only SELECTs (routes.py) without change.

**D1 CONFIRMED rule (Felix 2026-09-13):**
  1 (good — match dwell, keep earning): shrunk $/visit
     (`route_timed_stops.exp_per_visit`) STRICTLY ABOVE the route/global mean AND
     in-season AND not event-contaminated AND not a gem AND not a develop-target.
  2 (needs work — cultivate, don't over-dwell): below-mean shrunk rate, OR a TIE
     at the mean, OR an under-visited gem (EVERY gem -> 2 — the canonical cultivate
     target), OR a route-shift / new-area develop target, OR seasonal-off, OR
     event-contaminated, OR insufficient data.

A TIE (rate == mean) grades 2: the D1 shorthand says ">= mean" for a 1, but the
same rule states "ties/insufficient -> 2" as the SAFE default — a weak stop must
never read as "1, camp here". So a 1 requires the rate to be STRICTLY greater than
the mean; equality falls to the safe 2. Acceptance (brief §68): gem->2, tie->2,
weak->2, good->1.

`grade_reason` is an AUDIT tag only (e.g. `good_rate`, `weak_rate`, `tie`, `gem`,
`develop_target`, `seasonal_off`, `event_contaminated`, `insufficient`). It is
NEVER spoken or shown to a driver — it exists so every grade is defensible, exactly
like `flag_resolver.py`'s `reason` dict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StopSignals:
    """The Master Route signals a single planned stop is graded on.

    All fields are already computed by MR and read READ-ONLY at sign-in
    (INTEGRATION_MASTER_ROUTE.md §2.2). Field names mirror the MR columns:

      exp_per_visit  -- route_timed_stops.exp_per_visit (shrunk $/visit expectation)
      benchmark      -- the route/global mean $/visit to compare against
      in_season      -- current_season() gate (school-stop-in-summer -> False)
      event_contaminated -- the stop's history is event-driven, not street sales
      is_gem         -- under-visited gem (undervisited_gems.py); EVERY gem -> 2
      is_develop_target -- MR is actively developing this stop (route-shift/new-area)

    Unknown-safe: exp_per_visit / benchmark may be None (missing MR data) -> the
    classifier returns the safe 2 ('insufficient'). in_season defaults True (a
    missing/empty seasonality table must not suppress a real stop), the two
    contamination flags default False (absence of a bad signal is not a bad signal).
    """

    exp_per_visit: Optional[float]
    benchmark: Optional[float]
    in_season: bool = True
    event_contaminated: bool = False
    is_gem: bool = False
    is_develop_target: bool = False


@dataclass(frozen=True)
class GradeResult:
    grade: int          # 1 | 2 — the coach's entire output
    reason: str         # audit tag only; NEVER spoken/shown


# Grades, named so callers never sprinkle magic 1/2 literals.
GOOD = 1
NEEDS_WORK = 2


def classify_stop(signals: StopSignals) -> GradeResult:
    """Classify one stop into (grade, reason). Pure and total: always 1 or 2.

    Precedence is DELIBERATE — every "make it a 2" signal is checked before the
    only path that yields a 1, so a good rate can never override a gem, a
    develop-target, an off-season/event-contaminated stop, or missing data. The
    order also gives a stable, most-specific audit reason:

      1) insufficient data (missing rate or benchmark)   -> 2 insufficient
      2) under-visited gem (every gem is a cultivate)     -> 2 gem
      3) develop-target (route-shift / new area)          -> 2 develop_target
      4) seasonal-off                                     -> 2 seasonal_off
      5) event-contaminated history                       -> 2 event_contaminated
      6) rate <= benchmark (below-mean OR tie)            -> 2 weak_rate | tie
      7) rate  > benchmark, all gates clear               -> 1 good_rate
    """
    rate = signals.exp_per_visit
    mean = signals.benchmark

    # 1) insufficient data -> safe 2 (ties/insufficient -> 2).
    if rate is None or mean is None:
        return GradeResult(NEEDS_WORK, "insufficient")

    # 2) every under-visited gem grades 2 (the canonical cultivate target),
    #    regardless of how strong its per-visit rate looks.
    if signals.is_gem:
        return GradeResult(NEEDS_WORK, "gem")

    # 3) a stop MR is actively developing (route-shift / new area) -> cultivate.
    if signals.is_develop_target:
        return GradeResult(NEEDS_WORK, "develop_target")

    # 4) seasonal-off (e.g. a school stop in summer). MR normally suppresses these
    #    from the plan; if one still reaches us, it is never a "camp here" 1.
    if not signals.in_season:
        return GradeResult(NEEDS_WORK, "seasonal_off")

    # 5) event-contaminated: the rate reflects event days, not street demand.
    if signals.event_contaminated:
        return GradeResult(NEEDS_WORK, "event_contaminated")

    # 6) rate strictly above the mean -> 1; equal (tie) or below -> 2.
    if rate > mean:
        return GradeResult(GOOD, "good_rate")
    if rate == mean:
        return GradeResult(NEEDS_WORK, "tie")
    return GradeResult(NEEDS_WORK, "weak_rate")


def grade_for_stop(
    exp_per_visit: Optional[float],
    benchmark: Optional[float],
    *,
    in_season: bool = True,
    event_contaminated: bool = False,
    is_gem: bool = False,
    is_develop_target: bool = False,
) -> int:
    """Convenience wrapper returning just the grade int (1|2). Same rule as
    classify_stop; use classify_stop when you also need the audit reason."""
    return classify_stop(
        StopSignals(
            exp_per_visit=exp_per_visit,
            benchmark=benchmark,
            in_season=in_season,
            event_contaminated=event_contaminated,
            is_gem=is_gem,
            is_develop_target=is_develop_target,
        )
    ).grade

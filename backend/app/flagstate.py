"""PriorFlagState persistence — the hysteresis carried across days (SERVER_FLAGS
spec §2.2). Reads the driver's last state before resolve_flags(), writes the
resolved next_state after. Lives in the isolated driver_coach_flag_state table.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

from typing import Optional

from .refcore import PriorFlagState


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


def save_state(cur, driver_id: str, state: PriorFlagState) -> None:
    """Upsert the resolved next_state for a driver (idempotent per driver)."""
    cur.execute(
        """
        INSERT INTO driver_coach_flag_state
            (driver_id, coached, eligible_streak_days, slipped_streak_days, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (driver_id) DO UPDATE SET
            coached = EXCLUDED.coached,
            eligible_streak_days = EXCLUDED.eligible_streak_days,
            slipped_streak_days = EXCLUDED.slipped_streak_days,
            updated_at = now()
        """,
        (
            driver_id,
            state.coached,
            state.eligible_streak_days,
            state.slipped_streak_days,
        ),
    )

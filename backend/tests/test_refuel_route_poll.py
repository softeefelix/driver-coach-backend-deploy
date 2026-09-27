"""Refuel reminder wiring for the Driver Coach live route-poll payload."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.payload import build_route_payload
from app.service import refuel_reminder_for_route_poll


PT = ZoneInfo("America/Los_Angeles")


def at(hour: int, minute: int = 0) -> datetime:
    # September 14, 2026 is a Monday.
    return datetime(2026, 9, 14, hour, minute, tzinfo=PT)


def route_poll_at(now: datetime) -> dict:
    return build_route_payload(
        next_stop=None,
        route_cluster_id=42,
        phase="driving",
        coached=False,
        refuel_reminder=refuel_reminder_for_route_poll(7, now=now),
    )


def test_route_poll_omits_refuel_reminder_before_6pm_and_includes_it_at_6pm_on_monday():
    assert "banner" not in route_poll_at(at(17, 59))
    assert route_poll_at(at(18, 0))["banner"] == {"kind": "refuel"}
    assert refuel_reminder_for_route_poll(7, now=at(18, 0), refuel_confirmed=True) is None

"""No-idle nudges, break allowances, and the route-poll contract."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.breaks import allowance, auto_end_reason, end_stats, wire_state
from app.config import break_settings, max_fix_age_s, no_idle_settings
from app.no_idle import (
    MANAGER_NAME,
    IdleDecision,
    MotionView,
    NoIdleConfig,
    decide_idle,
    motion_view,
    nudge_message,
)
from app.payload import build_route_payload
from app.refuel import Reminder
from app.scorecard import last_truck_sale
from app import config, service

T0 = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
CFG = NoIdleConfig()


def view(**overrides) -> MotionView:
    base = dict(
        fresh=True, stationary=True, moving=False, at_stop=True, stop_order=4,
        lat=37.8, lng=-122.2, speed_mph=0.0, fix_age_s=1.0,
    )
    base.update(overrides)
    return MotionView(**base)


def step(episode, seconds, **kwargs) -> IdleDecision:
    return decide_idle(
        now=T0 + timedelta(seconds=seconds),
        view=kwargs.pop("view", view()),
        sales_known=kwargs.pop("sales_known", True),
        last_sale_at=kwargs.pop("last_sale_at", None),
        break_active=kwargs.pop("break_active", False),
        episode=episode,
        cfg=kwargs.pop("cfg", CFG),
        next_stop_name=kwargs.pop("next_stop_name", "Panama Street"),
    )


def test_thresholds_pin_the_owners_rule():
    raw = config.load_thresholds()
    assert raw["no_idle"]["no_sale_move_on_s"] == 90
    assert raw["no_idle"]["off_stop_grace_s"] == 15
    assert raw["breaks"]["lunch_duration_s"] == 1800
    assert raw["breaks"]["break_duration_s"] == 900
    assert raw["breaks"]["lunch_allowance"] == 1
    assert raw["breaks"]["break_allowance"] == 2
    assert no_idle_settings()["no_sale_move_on_s"] == 90
    assert break_settings()["lunch_allowance"] == 1
    assert allowance(break_settings(), "break") == 2


def test_at_stop_nudge_starts_at_90s_and_escalates_to_felix():
    first = step(None, 0)
    assert first.nudge is None and first.opened_new
    assert step(first.open_episode, 89).nudge is None

    banner = step(step(first.open_episode, 89).open_episode, 90)
    assert banner.nudge["level"] == "banner"
    assert banner.nudge["message"] == "No sale for 90 seconds. Move to the next stop (Panama Street)."
    assert banner.nudge["atStop"] is True
    assert banner.nudge["managerAlert"] is False
    assert banner.nudge["idleForS"] == 90
    assert [item["level"] for item in banner.open_episode["nudges"]] == ["banner"]

    voice = step(banner.open_episode, 150)
    assert voice.nudge["level"] == "voice"
    assert "Leave now" in voice.nudge["message"]
    assert voice.nudge["managerAlert"] is False

    manager = step(voice.open_episode, 270)
    assert manager.nudge["level"] == "manager"
    assert manager.nudge["managerAlert"] is True
    assert manager.nudge["managerAlertFor"] == MANAGER_NAME
    assert "Felix has been alerted" in manager.nudge["message"]
    assert manager.open_episode["manager_alert_for"] == "Felix"
    assert [item["level"] for item in manager.open_episode["nudges"]] == ["banner", "voice", "manager"]


def test_a_long_idle_still_opens_on_a_banner():
    opened = step(None, 0).open_episode
    opened["started_at"] = T0 - timedelta(seconds=1000)
    decision = step(opened, 0)
    assert decision.nudge["level"] == "banner"
    assert decision.nudge["managerAlert"] is False


def test_off_stop_uses_the_settle_grace_and_still_requires_no_sale():
    parked = view(at_stop=False, stop_order=None)
    opened = step(None, 0, view=parked)
    assert opened.nudge is None
    assert step(opened.open_episode, 14, view=parked).nudge is None
    due = step(opened.open_episode, 15, view=parked)
    assert due.nudge["level"] == "banner"
    assert due.nudge["atStop"] is False
    assert due.nudge["message"].startswith("Parked off the route with no sale.")

    recent = T0 - timedelta(seconds=10)
    again = step(None, 0, view=parked, last_sale_at=recent)
    held = step(again.open_episode, 20, view=parked, last_sale_at=recent)
    assert held.nudge is None

    old = T0 - timedelta(seconds=120)
    quiet = step(None, 0, view=parked, last_sale_at=old)
    nudged = step(quiet.open_episode, 15, view=parked, last_sale_at=old)
    assert nudged.nudge["level"] == "banner"
    assert nudged.nudge["noSaleForS"] == 135


def test_a_sale_closes_the_episode_and_restarts_the_clock():
    opened = step(None, 0).open_episode
    sale_at = T0 + timedelta(seconds=40)
    decision = step(opened, 50, last_sale_at=sale_at)
    assert decision.closed["end_reason"] == "sale"
    assert decision.closed["duration_s"] == 40
    assert decision.opened_new
    assert decision.nudge is None
    assert decision.open_episode["started_at"] == sale_at
    assert decision.open_episode["id"] != opened["id"]


def test_moving_break_unknown_sales_and_stale_fix():
    opened = step(None, 0).open_episode

    moved = step(opened, 30, view=view(stationary=False, moving=True, speed_mph=8))
    assert moved.closed["end_reason"] == "moved" and moved.nudge is None and moved.open_episode is None

    paused = step(opened, 10, break_active=True)
    assert paused.closed["end_reason"] == "break" and paused.nudge is None

    assert step(None, 120, sales_known=False).open_episode is None
    held = step(opened, 200, sales_known=False)
    assert held.nudge is None and held.dirty is False and held.closed is None

    stale = view(fresh=False, stationary=False, moving=False, fix_age_s=400)
    assert step(None, 0, view=stale).open_episode is None
    kept = step(opened, 30, view=stale)
    assert kept.nudge is None and kept.dirty is False and kept.closed is None
    assert kept.open_episode["id"] == opened["id"]


def test_creeping_does_not_open_or_clear_an_episode():
    crawling = view(stationary=False, moving=False, speed_mph=4)
    assert step(None, 30, view=crawling).open_episode is None
    opened = step(None, 0).open_episode
    kept = step(opened, 100, view=crawling)
    assert kept.closed is None and kept.open_episode["id"] == opened["id"]
    assert kept.nudge["level"] == "banner"


def test_motion_view_reuses_speed_radius_and_fix_age(monkeypatch):
    monkeypatch.delenv("DRIVER_COACH_MAX_FIX_AGE_S", raising=False)
    pin = [{"stop_order": 2, "lat": 37.8, "lng": -122.2, "kind": "neighborhood"}]
    here = {"lat": 37.8, "lng": -122.2, "speed": 0, "fix_age_s": 150}
    fresh = motion_view(here, phase="driving", parked_stop=None, plan=pin)
    assert fresh.fresh and fresh.stationary and fresh.at_stop and fresh.stop_order == 2

    stale = motion_view(
        {**here, "fix_age_s": 150.1, "speed": 0}, phase="parked", parked_stop={"stop_order": 2}, plan=pin,
    )
    assert stale.fresh is False and stale.stationary is False and stale.at_stop is False

    moving = motion_view({**here, "speed": 6}, phase="parked", parked_stop=None, plan=pin)
    assert moving.moving and moving.stationary is False

    settled_edge = motion_view({**here, "speed": 2}, phase="driving", parked_stop=None, plan=[])
    assert settled_edge.stationary and settled_edge.at_stop is False

    # ~50m north of the pin is outside the 40m parked radius.
    away = motion_view(
        {"lat": 37.80045, "lng": -122.2, "speed": 0, "fix_age_s": 1},
        phase="driving", parked_stop=None, plan=pin,
    )
    assert away.stationary and away.at_stop is False

    monkeypatch.setenv("DRIVER_COACH_MAX_FIX_AGE_S", "30")
    assert max_fix_age_s() == 30
    assert motion_view(
        {**here, "fix_age_s": 30}, phase="driving", parked_stop=None, plan=pin,
    ).fresh
    assert motion_view(
        {**here, "fix_age_s": 31}, phase="driving", parked_stop=None, plan=pin,
    ).fresh is False
    monkeypatch.setenv("DRIVER_COACH_MAX_FIX_AGE_S", "nope")
    assert max_fix_age_s() == 150


def test_break_end_rules_and_overrun_grace():
    cfg = break_settings()
    started = T0
    active = {"started_at": started, "allowed_s": 1800, "break_type": "lunch"}
    assert auto_end_reason(active, T0 + timedelta(seconds=1799), fresh=True, moving=False) is None
    assert auto_end_reason(active, T0 + timedelta(seconds=1800), fresh=True, moving=False) == "expired"
    assert auto_end_reason(active, T0 + timedelta(seconds=10), fresh=True, moving=True) == "moving"
    assert auto_end_reason(active, T0 + timedelta(seconds=10), fresh=False, moving=True) is None

    on_time = end_stats(started, T0 + timedelta(seconds=1800), 1800, cfg["overrun_grace_s"])
    assert on_time["overrun_s"] == 0 and on_time["flagged"] is False
    poll_lag = end_stats(started, T0 + timedelta(seconds=1820), 1800, cfg["overrun_grace_s"])
    assert poll_lag["overrun_s"] == 20 and poll_lag["flagged"] is False
    late = end_stats(started, T0 + timedelta(seconds=1861), 1800, cfg["overrun_grace_s"])
    assert late["overrun_s"] == 61 and late["flagged"] is True

    state = wire_state(
        [active], active=active, latest_flag=None, now=T0 + timedelta(seconds=60), cfg=cfg,
    )
    assert state["active"] is True and state["remainingS"] == 1740
    assert state["lunchAllowed"] == 1 and state["breakAllowed"] == 2


def test_square_sale_matches_the_driver_or_the_truck_and_not_a_longer_number():
    team = {"tm-1": "Amina Cole"}
    def payment(when, **extra):
        body = {
            "status": "COMPLETED",
            "amount_money": {"amount": 350},
            "team_member_id": "tm-1",
            "created_at": when,
        }
        body.update(extra)
        return body

    sale = last_truck_sale(
        truck_no=7, driver_name="Amina Cole", now=T0, team=team,
        payments=[payment("2026-10-15T17:40:00Z")],
    )
    assert sale["known"] is True
    assert sale["last_sale_at"] == datetime(2026, 10, 15, 17, 40, tzinfo=timezone.utc)

    tagged = last_truck_sale(
        truck_no=7, driver_name="", now=T0, team={},
        payments=[payment("2026-10-15T17:50:00Z", team_member_id="other", note="Truck #7")],
    )
    assert tagged["known"] is True and tagged["last_sale_at"].minute == 50

    other_truck = last_truck_sale(
        truck_no=7, driver_name="", now=T0, team={},
        payments=[
            payment("2026-10-15T17:50:00Z", team_member_id="other", note="Truck 17"),
            payment("2026-10-15T17:51:00Z", team_member_id="other", device_details={"device_name": "T17"}),
        ],
    )
    assert other_truck == {"known": False, "last_sale_at": None}

    none_today = last_truck_sale(
        truck_no=7, driver_name="Amina Cole", now=T0, team=team, payments=[],
    )
    assert none_today == {"known": True, "last_sale_at": None}
    # No token in this environment: the live lookup must not invent a sale.
    from app.scorecard import clear_sale_cache
    clear_sale_cache()
    assert last_truck_sale(truck_no=7, driver_name="Amina Cole", now=T0)["known"] is False


def test_poll_payload_carries_nudge_and_break_without_clobbering_refuel():
    nudge = {
        "level": "voice",
        "message": nudge_message("voice", True, 90, "Panama Street"),
        "managerAlert": False,
    }
    resting = {
        "active": True, "type": "break", "remainingS": 400,
        "lunchAllowed": 1, "breakAllowed": 2,
    }
    payload = build_route_payload(
        next_stop={"address": "100, Panama Street, Oakland", "grade": 1},
        route_cluster_id=42,
        phase="parked",
        coached=True,
        refuel_reminder=Reminder(truck_number=7, service_date=T0.date()),
        nudge=nudge,
        break_state=resting,
    )
    assert payload["banner"] == {"kind": "refuel"}
    assert payload["nudge"] is nudge
    assert payload["break"] is resting
    assert "voice" not in payload
    nominal = build_route_payload(
        next_stop=None, route_cluster_id=42, phase="driving", coached=False,
        nudge=None, break_state=resting,
    )
    assert nominal["nudge"] is None and nominal["break"] == resting and "coach" not in nominal
    untouched = build_route_payload(
        next_stop=None, route_cluster_id=42, phase="driving", coached=False,
    )
    assert "nudge" not in untouched and "break" not in untouched and "banner" not in untouched


def test_break_routes_map_client_errors(monkeypatch):
    def missing(req, schema="driver_coach"):
        raise KeyError("unknown session_id: x")

    monkeypatch.setattr(service, "do_start_break", missing)
    monkeypatch.setattr(service, "do_end_break", missing)
    client = TestClient(service.app)
    start = client.post("/driver-coach/v1/break/start", json={"session_id": "x", "type": "lunch"})
    assert start.status_code == 404
    end = client.post("/driver-coach/v1/break/end", json={"session_id": "x"})
    assert end.status_code == 404

    def limited(req, schema="driver_coach"):
        from app.breaks import BreakError
        raise BreakError(409, {"error": "over_limit", "message": "This shift already has its lunch."}, commit=True)

    monkeypatch.setattr(service, "do_start_break", limited)
    again = client.post("/driver-coach/v1/break/start", json={"session_id": "x", "type": "lunch"})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "over_limit"

    bad = client.post("/driver-coach/v1/break/start", json={"session_id": "x"})
    assert bad.status_code == 422

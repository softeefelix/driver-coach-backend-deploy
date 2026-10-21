"""ONE_OFF events take precedence over remaining stops (Felix 2026-10-20).

Case: Sony Playstation event 5:30-7:00 PM PT, reassigned Emery N -> Leila.
"""
from app.advisor import advise_plan
from app import school_slack

PLAN = [
    {"stop_order": 1, "address": "100 A St, San Mateo", "arrive": "13:00", "lat": 37.55, "lng": -122.31},
    {"stop_order": 2, "address": "200 B St, San Mateo", "arrive": "15:00", "lat": 37.56, "lng": -122.30},
    {"stop_order": 3, "address": "300 C St, San Mateo", "arrive": "18:00", "lat": 37.57, "lng": -122.29},
]
SONY = {"id": "sony", "title": "Food Fleet - Sony Playstation Event 10/20",
        "address": "2207 Bridgepointe Parkway, Foster City", "startTime": "5:30 PM",
        "endTime": "7:00 PM", "status": "LATE", "lat": 37.56, "lng": -122.28}
BINGO = {"kind": "visit", "id": "bingo", "title": "Bingo Night", "address": "9 Elm, San Mateo",
         "startTime": "4:00 PM", "endTime": "5:00 PM", "lat": 37.55, "lng": -122.30}


def m(h, mi=0):
    return h * 60 + mi


def test_fallback_pins_started_event_until_it_ends():
    for now in (m(17, 10), m(17, 45), m(18, 59)):
        out = advise_plan(PLAN, served_orders=set(), skipped_orders=set(), now_minutes=now, events=[SONY])
        assert out["next_stop"]["kind"] == "event", now
    out = advise_plan(PLAN, served_orders=set(), skipped_orders=set(), now_minutes=m(19, 5), events=[SONY])
    assert out["next_stop"]["stop_order"] == 1


def test_fallback_has_no_90_minute_window_but_waits_for_leave_by():
    out = advise_plan(PLAN, served_orders=set(), skipped_orders=set(), now_minutes=m(13, 53), events=[SONY])
    assert out["next_stop"]["stop_order"] == 1   # route continues until leave-by


def test_recurring_visit_is_a_normal_stop_never_next():
    out = advise_plan(PLAN, served_orders=set(), skipped_orders=set(), now_minutes=m(15, 55), events=[BINGO])
    assert out["next_stop"]["stop_order"] == 1
    assert any(r.get("kind") == "visit" for r in out["plan"])


def _choose(now, events, travel_min=25, previous=None):
    return school_slack.choose(PLAN, served_orders=set(), skipped_orders=set(), events=events,
                               now_minutes=now, position={"lat": 37.5, "lng": -122.3},
                               travel=lambda o, d: travel_min, previous=previous)


def test_live_chooser_pins_event_at_eta_leave_by():
    early = _choose(m(16, 0), [SONY])
    assert early["next_stop"].get("kind") != "event"
    assert early["anchor_context"]["leaveBy"] == "4:45 PM"   # 5:30 - 15 buffer - 5 margin - 25 drive
    due = _choose(m(16, 50), [SONY])
    assert due["next_stop"]["kind"] == "event"
    assert due["anchor_context"]["leaveBy"] == "4:45 PM"


def test_live_chooser_keeps_started_event_and_drops_ended_one():
    assert _choose(m(18, 0), [SONY])["next_stop"]["kind"] == "event"
    after = _choose(m(19, 1), [SONY], previous={"binding": "event:sony"})
    assert after["next_stop"].get("kind") != "event"


def test_live_chooser_recurring_visit_never_anchors():
    out = _choose(m(15, 58), [BINGO])
    assert out["next_stop"]["stop_order"] in (1, 2, 3)
    assert out["reason_code"] == "planned"


SCHOOL_PLAN = PLAN + [{"stop_order": 4, "address": "Junipero Serra High School, San Mateo",
                       "arrive": "14:20", "lat": 37.53, "lng": -122.33}]


def test_due_event_beats_an_earlier_unserved_school_even_if_latched():
    early = school_slack.choose(SCHOOL_PLAN, served_orders=set(), skipped_orders=set(), events=[SONY],
                                now_minutes=m(14, 0), position={"lat": 37.5, "lng": -122.3},
                                travel=lambda o, d: 10, previous={})
    assert early["next_stop"]["kind"] == "school"
    later = school_slack.choose(SCHOOL_PLAN, served_orders=set(), skipped_orders=set(), events=[SONY],
                                now_minutes=m(17, 5), position={"lat": 37.5, "lng": -122.3},
                                travel=lambda o, d: 10, previous={"binding": "stop:4"})
    assert later["next_stop"]["kind"] == "event"
    assert later["anchor_context"]["leaveBy"] == "5:00 PM"

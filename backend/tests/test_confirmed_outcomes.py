"""Confirmed stop outcomes stay independent of Geotab estimates.

The route poll must not write served or skipped orders. These tests use in-memory
fakes only: no database, no live Geotab, and no session service.
"""
import json
from contextlib import contextmanager

import pytest

from app import adherence, decision_store, driven_path, liveness, routes, service
from app.geotab import haversine_m
from app.payload import assert_disguise_safe

LNG = -122.4
SID = "11111111-1111-1111-1111-111111111111"


def lat(order: int) -> float:
    return 37.0 + (order - 1) * 0.01


def pin(order: int) -> dict:
    return {
        "stop_order": order,
        "kind": "neighborhood",
        "address": f"{order} Example Street, Oakland",
        "arrive": "9:00 AM",
        "lat": lat(order),
        "lng": LNG,
        "plan_source": "master",
    }


def master(n: int = 29) -> list[dict]:
    return [pin(order) for order in range(1, n + 1)]


def park_at(order: int) -> dict:
    return {"lat": lat(order), "lng": LNG, "at": "2026-11-05T18:00:00+00:00", "gear": 126}


def test_fixture_spacing_matches_the_park_and_pass_radii():
    assert haversine_m(lat(29), LNG, lat(28), LNG) > 250
    assert haversine_m(lat(5), LNG, lat(4), LNG) > 250
    assert haversine_m(lat(29), LNG, lat(29), LNG) == 0


def test_park29_pass_by_and_departed_gear_parks_are_estimates_only():
    plan = master()
    # Gear 126 at two earlier pins the truck has left, plus the park it is in now.
    parks = [park_at(2), park_at(3), park_at(29)]
    estimate = adherence.adherence_estimate(
        plan, parks, now_lat=lat(29), now_lng=LNG,
    )
    assert estimate["source"] == "geotab"
    assert estimate["authoritative"] is False
    assert estimate["affectsCursor"] is False
    assert estimate["parkedStopOrder"] == 29
    assert estimate["estimatedServedStopOrders"] == [2, 3]
    assert 29 not in estimate["estimatedServedStopOrders"]
    assert estimate["estimatedPassedStopOrders"] == list(range(1, 29))

    # A wrong turn that is merely near stop 5, with no Park, skips nothing confirmed.
    wrong_turn = adherence.adherence_estimate(plan, [], now_lat=lat(5), now_lng=LNG)
    assert wrong_turn["estimatedServedStopOrders"] == []
    assert wrong_turn["parkedStopOrder"] is None
    assert wrong_turn["estimatedPassedStopOrders"] == [1, 2, 3, 4]
    assert wrong_turn["affectsCursor"] is False


def test_resolver_keeps_earlier_unfinished_master_pins_at_park29(monkeypatch):
    plan = master()
    parks = [park_at(2), park_at(3), park_at(29)]
    monkeypatch.setattr(routes, "truck_id_for", lambda *a, **k: "device-7")
    monkeypatch.setattr(routes, "read_truck_position", lambda *a, **k: {
        "lat": lat(29), "lng": LNG, "device_id": "device-7", "speed": 0, "fix_age_s": 1,
    })
    monkeypatch.setattr(driven_path, "driven_path", lambda *a, **k: {
        "driven": [{"lat": lat(29), "lng": LNG}], "stops": parks,
    })
    monkeypatch.setattr(routes, "_grade_stop", lambda cur, stop, *a, **k: stop)
    monkeypatch.setattr(routes, "_attach_live_eta", lambda cur, stop, *a, **k: stop)
    monkeypatch.setattr(
        "app.street_route.confirmed_cluster_leg", lambda *a, **k: None,
    )

    class Boom:
        def execute(self, *a, **k):
            raise AssertionError("resolver touched the database")

        def fetchone(self):
            raise AssertionError("resolver touched the database")

    served, skipped = set(), set()
    before = json.dumps(plan)
    result = routes.resolve_live_route(
        Boom(), 7, route_cluster_id=42, dow="Monday",
        frozen_plan=plan, served_orders=served, skipped_orders=skipped,
    )
    assert json.dumps(plan) == before
    assert served == set() and skipped == set()
    assert result["next_stop"]["stop_order"] == 1
    assert result["phase"] == "parked"
    assert result["parked_stop"]["stop_order"] == 29
    orders = [stop["order"] for stop in result["map"]["stops"]]
    assert orders[0] == 1
    assert orders == list(range(1, 30))
    assert result["map"]["drivenStops"] == parks

    # An explicit Done removes that pin only. The following stop stays at the
    # front of the remaining window, so the server does not also skip it.
    done_one = routes.resolve_live_route(
        Boom(), 7, route_cluster_id=42, dow="Monday",
        frozen_plan=plan, served_orders={1}, skipped_orders=set(),
    )
    done_orders = [stop["order"] for stop in done_one["map"]["stops"]]
    assert done_one["next_stop"]["stop_order"] == 2
    assert done_orders[0] == 2
    assert 1 not in done_orders and 3 in done_orders

    # Stored confirmed skips still hide those pins. Inference does not add more.
    historical = routes.resolve_live_route(
        Boom(), 7, route_cluster_id=42, dow="Monday",
        frozen_plan=plan, served_orders=set(), skipped_orders={1, 2},
    )
    historical_orders = [stop["order"] for stop in historical["map"]["stops"]]
    assert historical["next_stop"]["stop_order"] == 3
    assert 1 not in historical_orders and 2 not in historical_orders
    assert 3 in historical_orders and 29 in historical_orders


class Store:
    def __init__(self, plan, served=(), skipped=()):
        self.plan = plan
        self.served = list(served)
        self.skipped = list(skipped)
        self.outcome_writes = 0
        self.resets = 0


class Cursor:
    def __init__(self, store: Store):
        self.store = store
        self.statements = []
        self._text = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        text = sql if isinstance(sql, str) else str(sql)
        self.statements.append(text)
        self._text = text
        if "served_stop_orders='[]'" in text:
            self.store.served = []
            self.store.skipped = []
            self.store.resets += 1
        elif text.lstrip().upper().startswith("UPDATE") and "served_stop_orders" in text:
            self.store.served = json.loads(params[0])
            self.store.skipped = json.loads(params[1])
            self.store.outcome_writes += 1

    def fetchone(self):
        text = self._text
        if "SELECT truck_no, coached, route_cluster_id" in text:
            return (
                7, False, 42, "drv", self.store.plan,
                list(self.store.served), list(self.store.skipped),
            )
        if text.lstrip().startswith("SELECT plan_snapshot, served_stop_orders"):
            return (self.store.plan, list(self.store.served), list(self.store.skipped))
        if "SELECT truck_no, coached, driver_id" in text:
            return (7, False, "drv")
        return None


class Conn:
    def __init__(self, store: Store):
        self.cursor_obj = Cursor(store)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def outcome_updates(cursor: Cursor) -> list[str]:
    return [
        sql for sql in cursor.statements
        if "served_stop_orders" in sql and sql.lstrip().upper().startswith("UPDATE")
    ]


@pytest.fixture
def cab(monkeypatch):
    plan = master()
    store = Store(plan)
    conn = Conn(store)
    calls = []

    def resolve(cur, truck_no, route_cluster_id=None, dow=None, **kw):
        served = set(kw.get("served_orders") or ())
        skipped = set(kw.get("skipped_orders") or ())
        calls.append((set(served), set(skipped)))
        remaining = [
            stop for stop in plan
            if stop["stop_order"] not in served and stop["stop_order"] not in skipped
        ]
        nxt = dict(remaining[0]) if remaining else None
        return {
            "next_stop": nxt,
            "route_cluster_id": 42,
            "phase": "parked",
            "shift_phase": "plan",
            "route_count": f"{(nxt or {}).get('stop_order', 0)} / 29",
            "up_next": [],
            "turns": [],
            "plan_source": "master",
            "parked_stop": {"stop_order": 29, "lat": lat(29), "lng": LNG},
            "driven_stops": [park_at(2), park_at(3), park_at(29)],
            "match_plan": plan,
            "position": {"lat": lat(29), "lng": LNG, "speed": 0, "fix_age_s": 1},
            "map": {
                "truck": {"lat": lat(29), "lng": LNG},
                "stops": [
                    {"order": stop["stop_order"], "lat": stop["lat"], "lng": stop["lng"],
                     "name": stop["address"]}
                    for stop in remaining
                ],
                "driven": [{"lat": lat(29), "lng": LNG}],
                "drivenStops": [park_at(2), park_at(3), park_at(29)],
            },
        }

    @contextmanager
    def connect(schema="driver_coach", autocommit=False):
        yield conn

    latch = {"choice": "stop:1"}
    held = {"revision": 0, "targetOrder": None}

    def load(*args, **kwargs):
        return {"choice": latch.get("choice"), "driverProgress": dict(held)}

    def append(*args, **kwargs):
        advice = args[3] if len(args) > 3 else kwargs.get("advice") or {}
        raw = (advice.get("state") or {}).get("driverProgress")
        if isinstance(raw, dict):
            held["revision"] = raw.get("revision") or 0
            held["targetOrder"] = raw.get("targetOrder")
        return "audit"

    def resolving(*args, **kwargs):
        raw = kwargs.get("driver_progress")
        if isinstance(raw, dict):
            held["revision"] = raw.get("revision") or 0
            held["targetOrder"] = raw.get("targetOrder")
        return resolve(*args, **kwargs)

    monkeypatch.setattr(service, "connect", connect)
    monkeypatch.setattr(service.routes, "resolve_live_route", resolving)
    monkeypatch.setattr(service.routes, "ordered_stops_for_route", lambda *a, **k: plan)
    monkeypatch.setattr(service.jobber, "name_from_driver_id", lambda *a, **k: "")
    monkeypatch.setattr(service.events_mod, "get_today_events_with_status", lambda *a, **k: ([], {"status": "ok"}))
    monkeypatch.setattr(service.events_mod, "scheduled_visits_for_driver", lambda *a, **k: [])
    monkeypatch.setattr(service.scorecard, "live_ticker", lambda *a, **k: None)
    monkeypatch.setattr(service, "refuel_reminder_for_route_poll", lambda *a, **k: None)
    monkeypatch.setattr(service.breaks, "reconcile_on_poll", lambda *a, **k: (service.breaks.inactive_state(), False))
    monkeypatch.setattr(service.no_idle, "reconcile_on_poll", lambda *a, **k: (None, False))
    monkeypatch.setattr(decision_store, "load", load)
    monkeypatch.setattr(decision_store, "append", append)
    return {
        "store": store, "conn": conn, "calls": calls, "latch": latch,
        "plan": plan, "held": held,
    }


def test_repeated_gets_at_park29_do_not_persist_inference(cab):
    first = service.do_route(SID)
    second = service.do_route(SID)
    assert_disguise_safe(first)
    for body in (first, second):
        assert body["route"]["nextStop"]["stopOrder"] == 1
        assert body["phase"] == "parked"
        assert body["route"]["remainingStopOrders"] == list(range(1, 30))
        assert [stop["order"] for stop in body["route"]["stops"]] == list(range(1, 30))
        assert [stop["sessionState"] for stop in body["route"]["stops"]] == ["open"] * 29
        assert body["route"]["stops"][28]["lat"] == lat(29)
        assert body["route"]["progressRevision"] == 0
        assert body["route"]["driverTarget"] is None
        assert [stop["order"] for stop in body["map"]["stops"]] == list(range(1, 30))
        assert body["route"]["sessionOutcomes"] == {
            "source": "session",
            "legacyProvenanceUnknown": True,
            "servedStopOrders": [],
            "skippedStopOrders": [],
        }
        assert "confirmedOutcomes" not in body["route"]
        estimate = body["geotabAdherence"]
        assert estimate["authoritative"] is False
        assert estimate["affectsCursor"] is False
        assert estimate["parkedStopOrder"] == 29
        assert estimate["estimatedServedStopOrders"] == [2, 3]
        assert estimate["estimatedPassedStopOrders"] == list(range(1, 29))
        assert body["map"]["truck"] == {"lat": lat(29), "lng": LNG}
        assert body["map"]["drivenStops"]
    assert cab["calls"] == [(set(), set()), (set(), set())]
    assert cab["store"].served == [] and cab["store"].skipped == []
    assert cab["store"].outcome_writes == 0
    assert outcome_updates(cab["conn"].cursor_obj) == []


def test_explicit_done_and_skip_persist_and_reject_a_later_target(cab):
    service.do_route(SID)
    done = service.do_stop_outcome(service.StopOutcomeRequest(
        session_id=SID, stop_order=1, outcome="done",
    ))
    assert done["outcome"] == "done" and done["stop_order"] == 1
    assert done["progressRevision"] == 1
    assert cab["store"].served == [1]
    after_done = service.do_route(SID)
    assert after_done["route"]["nextStop"]["stopOrder"] == 2
    assert after_done["route"]["remainingStopOrders"] == list(range(2, 30))
    assert [stop["order"] for stop in after_done["route"]["stops"]] == list(range(1, 30))
    assert after_done["route"]["stops"][0]["sessionState"] == "served"
    assert after_done["route"]["stops"][1]["sessionState"] == "open"
    assert [stop["order"] for stop in after_done["map"]["stops"]][0] == 2
    assert 1 not in [stop["order"] for stop in after_done["map"]["stops"]]
    assert after_done["route"]["sessionOutcomes"]["servedStopOrders"] == [1]
    assert after_done["route"]["sessionOutcomes"]["legacyProvenanceUnknown"] is True
    assert after_done["route"]["sessionOutcomes"]["source"] == "session"
    assert after_done["geotabAdherence"]["estimatedPassedStopOrders"][0] == 1
    assert cab["calls"][-1] == ({1}, set())

    cab["latch"]["choice"] = "stop:2"
    skipped = service.do_stop_outcome(service.StopOutcomeRequest(
        session_id=SID, stop_order=2, outcome="skip",
    ))
    assert skipped["outcome"] == "skip"
    with pytest.raises(ValueError):
        service.do_stop_outcome(service.StopOutcomeRequest(
            session_id=SID, stop_order=29, outcome="skip",
        ))
    assert cab["store"].served == [1]
    assert cab["store"].skipped == [2]
    again = service.do_route(SID)
    assert again["route"]["sessionOutcomes"]["skippedStopOrders"] == [2]
    assert again["route"]["sessionOutcomes"]["legacyProvenanceUnknown"] is True
    assert again["route"]["remainingStopOrders"][0] == 3
    assert 29 in again["route"]["remainingStopOrders"]
    assert cab["store"].outcome_writes == 2
    assert len(outcome_updates(cab["conn"].cursor_obj)) == 2


def test_reset_clears_confirmed_outcomes_and_gets_do_not_restore_inference(cab):
    cab["store"].served = [8]
    cab["store"].skipped = list(range(1, 29))
    stale = service.do_route(SID)
    assert stale["route"]["nextStop"]["stopOrder"] == 29
    assert stale["route"]["remainingStopOrders"] == [29]
    assert stale["route"]["sessionOutcomes"]["skippedStopOrders"] == list(range(1, 29))
    assert stale["route"]["sessionOutcomes"]["legacyProvenanceUnknown"] is True
    assert stale["route"]["sessionOutcomes"]["source"] == "session"
    assert cab["store"].outcome_writes == 0

    service.do_confirm_assignment(service.ConfirmAssignmentRequest(
        session_id=SID, route_cluster_id=42,
    ))
    assert cab["store"].resets == 1
    assert cab["store"].served == [] and cab["store"].skipped == []
    fresh = service.do_route(SID)
    second = service.do_route(SID)
    assert fresh["route"]["remainingStopOrders"] == list(range(1, 30))
    assert second["route"]["sessionOutcomes"]["servedStopOrders"] == []
    assert second["route"]["sessionOutcomes"]["skippedStopOrders"] == []
    assert second["route"]["sessionOutcomes"]["legacyProvenanceUnknown"] is True
    assert second["route"]["nextStop"]["stopOrder"] == 1
    assert second["geotabAdherence"]["parkedStopOrder"] == 29
    assert cab["store"].served == [] and cab["store"].skipped == []
    # The reset UPDATE is the explicit clear. Later polls add no outcome writes.
    assert cab["store"].outcome_writes == 0
    assert cab["store"].resets == 1


def test_start_over_clears_outcomes_without_touching_the_frozen_plan(cab):
    from app.driver_progress import first_stop_order
    assert first_stop_order([pin(3), pin(1)]) == 3
    cab["store"].skipped = list(range(1, 29))
    cab["store"].served = [29]
    hidden = service.do_route(SID)
    assert hidden["route"]["remainingStopOrders"] == []
    assert [stop["order"] for stop in hidden["route"]["stops"]] == list(range(1, 30))
    assert hidden["route"]["stops"][0]["sessionState"] == "skipped"
    assert hidden["map"]["stops"] == []
    before = json.dumps(cab["plan"])
    body = service.do_start_over(service.StartOverRequest(session_id=SID, expected_revision=0))
    assert json.dumps(cab["plan"]) == before
    assert cab["store"].served == [] and cab["store"].skipped == []
    assert body["route"]["nextStop"]["stopOrder"] == 1
    assert body["route"]["driverTarget"] == {"stopOrder": 1, "source": "driver"}
    assert body["route"]["progressRevision"] == 1
    assert [stop["sessionState"] for stop in body["route"]["stops"]] == ["open"] * 29
    updates = [
        sql for sql in cab["conn"].cursor_obj.statements
        if sql.lstrip().upper().startswith("UPDATE")
    ]
    assert updates and all("plan_snapshot" not in sql for sql in updates)
    service.do_stop_outcome(service.StopOutcomeRequest(
        session_id=SID, stop_order=1, outcome="done", expected_revision=1,
    ))
    assert cab["store"].served == [1]
    with pytest.raises(service.ProgressConflict) as stale:
        service.do_start_over(service.StartOverRequest(session_id=SID, expected_revision=1))
    assert stale.value.revision == 2
    assert cab["store"].served == [1]
    assert cab["store"].resets == 1


def test_jump_does_not_complete_intervening_stops_and_back_can_target_one(cab):
    moved = service.do_set_target(service.SetTargetRequest(
        session_id=SID, stop_order=5, expected_revision=0,
    ))
    assert moved["route"]["nextStop"]["stopOrder"] == 5
    assert moved["route"]["driverTarget"]["stopOrder"] == 5
    assert cab["store"].served == [] and cab["store"].skipped == []
    orders = [stop["order"] for stop in moved["route"]["stops"]]
    assert orders == list(range(1, 30))
    assert 1 in [stop["order"] for stop in moved["map"]["stops"]]
    assert moved["map"]["stops"][0]["order"] == 5
    with pytest.raises(ValueError):
        service.do_stop_outcome(service.StopOutcomeRequest(
            session_id=SID, stop_order=4, outcome="done", expected_revision=1,
        ))
    assert cab["store"].outcome_writes == 0
    done = service.do_stop_outcome(service.StopOutcomeRequest(
        session_id=SID, stop_order=5, outcome="done", expected_revision=1,
    ))
    assert done["stop_order"] == 5
    assert cab["store"].served == [5]
    assert cab["store"].skipped == []
    again = service.do_route(SID)
    assert 1 in again["route"]["remainingStopOrders"]
    assert 4 in again["route"]["remainingStopOrders"]
    assert 5 not in again["route"]["remainingStopOrders"]
    assert again["route"]["stops"][4]["sessionState"] == "served"
    assert again["route"]["stops"][0]["sessionState"] == "open"
    back = service.do_set_target(service.SetTargetRequest(
        session_id=SID, stop_order=2, expected_revision=again["route"]["progressRevision"],
    ))
    assert back["route"]["nextStop"]["stopOrder"] == 2
    assert cab["store"].served == [5] and cab["store"].skipped == []
    with pytest.raises(ValueError):
        service.do_set_target(service.SetTargetRequest(
            session_id=SID, stop_order=99, expected_revision=back["route"]["progressRevision"],
        ))
    assert cab["held"]["targetOrder"] == 2


def test_school_outcome_stays_on_the_advised_stop_until_the_driver_selects_it(cab):
    cab["plan"][0]["kind"] = "school"
    cab["latch"]["choice"] = "stop:2"
    with pytest.raises(ValueError):
        service.do_stop_outcome(service.StopOutcomeRequest(
            session_id=SID, stop_order=1, outcome="done",
        ))
    assert cab["store"].outcome_writes == 0
    chosen = service.do_set_target(service.SetTargetRequest(
        session_id=SID, stop_order=1, expected_revision=0,
    ))
    assert chosen["route"]["driverTarget"]["stopOrder"] == 1
    service.do_stop_outcome(service.StopOutcomeRequest(
        session_id=SID, stop_order=1, outcome="done", expected_revision=1,
    ))
    assert cab["store"].served == [1]
    assert cab["store"].skipped == []


def test_repeated_get_does_not_apply_start_over(cab):
    cab["store"].skipped = [1, 2]
    service.do_route(SID)
    service.do_route(SID)
    assert cab["store"].skipped == [1, 2]
    assert cab["store"].resets == 0
    assert cab["held"]["revision"] == 0


def test_malformed_telemetry_does_not_fail_the_route_poll(cab, monkeypatch):
    garbage = adherence.adherence_estimate(
        plan=object(),
        park_stops={"lat": "nope", "lng": float("nan")},
        now_lat="37.2",
        now_lng=float("inf"),
    )
    assert garbage == adherence.empty_adherence()
    mixed = adherence.adherence_estimate(
        plan=[{"stop_order": True, "lat": float("nan"), "lng": -122.4}, pin(29)],
        park_stops=["park", {"lat": "x", "lng": -122.4}, park_at(29)],
        now_lat=lat(29),
        now_lng=LNG,
    )
    assert mixed["parkedStopOrder"] == 29
    assert mixed["authoritative"] is False
    assert mixed["affectsCursor"] is False

    def explode(*args, **kwargs):
        raise RuntimeError("telemetry blew up")

    monkeypatch.setattr(adherence, "adherence_estimate", explode)
    body = service.do_route(SID)
    assert body["route"]["nextStop"]["stopOrder"] == 1
    assert body["route"]["remainingStopOrders"] == list(range(1, 30))
    assert body["geotabAdherence"] == adherence.empty_adherence()
    assert body["route"]["sessionOutcomes"]["source"] == "session"
    assert cab["store"].outcome_writes == 0


def test_heartbeat_gps_is_not_a_route_outcome():
    class Cur:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append(sql if isinstance(sql, str) else str(sql))

        def fetchone(self):
            return (3,)

    cur = Cur()
    ack = liveness.ingest_batch(cur, SID, [{
        "seq": 4,
        "gps": {"lat": lat(29), "lng": LNG},
        "stop_ctx": {"stop_id": 29, "phase": "parked"},
    }])
    assert ack == 3
    blob = "\n".join(cur.sql).lower()
    assert "driver_coach_heartbeat" in blob
    assert "served_stop_orders" not in blob
    assert "skipped_stop_orders" not in blob

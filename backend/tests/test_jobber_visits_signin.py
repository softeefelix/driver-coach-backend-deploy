"""Sign-in assignment carries today's Jobber visits and optional route-match fields."""
import datetime

import pytest

from app import assignment, events, jobber
from app.events import filter_and_format
from app.jobber import format_assignment_visits, today_assignment_visits


DAY = datetime.date(2026, 10, 16)  # Friday, in-school season


@pytest.fixture(autouse=True)
def _clear_jobber_cache(monkeypatch):
    events._clear_cache()
    monkeypatch.delenv("MIN_ROUTE_STOPS", raising=False)
    yield
    events._clear_cache()


def _visit(**overrides):
    node = {
        "title": "Connecticut",
        "property": {
            "address": {
                "street": "410 Connecticut St",
                "city": "Redwood City",
                "province": "California",
            }
        },
        "job": {"jobType": "RECURRING", "title": "Redwood"},
        "assignedUsers": {
            "nodes": [
                {"name": {"full": "Emery N"}, "assignedVehicle": {"name": "13"}},
            ]
        },
    }
    node.update(overrides)
    return node


def test_visit_row_uses_property_address_and_vehicle():
    rows = format_assignment_visits([_visit()], "Emery N", 13)
    assert rows == [{
        "city": "Redwood City",
        "address": "410 Connecticut St, Redwood City, CA",
        "truck": 13,
        "assigned": True,
    }]


def test_assigned_when_truck_matches_even_if_driver_does_not():
    node = _visit(
        title="T13 - cruise",
        assignedUsers={"nodes": [{"name": {"full": "Other Person"}}]},
    )
    rows = format_assignment_visits([node], "Emery N", 13)
    assert rows[0]["truck"] == 13
    assert rows[0]["assigned"] is True


def test_unassigned_visit_keeps_its_own_truck():
    node = _visit(
        title="Softee 7",
        property={"address": {"street": "1 Broadway", "city": "Oakland", "province": "CA"}},
        assignedUsers={"nodes": [{"name": {"full": "Other Person"}}]},
    )
    rows = format_assignment_visits([node], "Emery N", 13)
    assert rows == [{
        "city": "Oakland",
        "address": "1 Broadway, Oakland, CA",
        "truck": 7,
        "assigned": False,
    }]


def test_vehicle_name_beats_a_truck_number_in_the_title():
    node = _visit(
        title="T7 leftover",
        assignedUsers={"nodes": [{"name": {"full": "Emery N"}, "assignedVehicle": {"name": "Truck #13"}}]},
    )
    assert format_assignment_visits([node], "Emery N", 13)[0]["truck"] == 13


def test_driver_match_survives_a_broken_name_map(monkeypatch):
    def boom(name, path=None):
        raise OSError("no map")

    monkeypatch.setattr(jobber, "resolve_square_name", boom)
    node = _visit(assignedUsers={"nodes": [
        {"name": {"full": "Other Person"}},
        {"name": {"full": "Emery N"}, "assignedVehicle": {"name": "13"}},
    ]})
    rows = format_assignment_visits([node], "Emery N", 99)
    assert rows[0]["assigned"] is True
    assert rows[0]["truck"] == 13


def test_visit_without_a_location_is_omitted_and_bad_nodes_are_skipped():
    located = _visit()
    rows = format_assignment_visits(
        [None, "nope", {"title": "T13"}, located],
        "Emery N",
        13,
    )
    assert [row["city"] for row in rows] == ["Redwood City"]


def test_empty_or_failed_jobber_is_an_empty_list(monkeypatch):
    monkeypatch.setattr(events, "_fetch_visits", lambda day: None)
    assert today_assignment_visits("Emery N", 13, DAY) == []
    monkeypatch.setattr(events, "_fetch_visits", lambda day: [])
    assert today_assignment_visits("Emery N", 13, DAY) == []

    def boom(day):
        raise RuntimeError("jobber down")

    monkeypatch.setattr(events, "_fetch_visits", boom)
    assert today_assignment_visits("Emery N", 13, DAY) == []


def test_events_formatter_ignores_the_extra_visit_fields():
    node = _visit()
    node["job"] = {"jobType": "ONE_OFF", "title": "Party"}
    rows = filter_and_format([node], "Emery N")
    assert rows[0]["address"] == "410 Connecticut St, Redwood City"
    assert set(rows[0]) >= {"title", "startTime", "address", "status"}
    assert "truck" not in rows[0]


class _FeedConn:
    """Fake psycopg2 connection serving one driver_coach_test.events_today row."""

    def __init__(self, row, calls):
        self.row, self.calls = row, calls

    def cursor(self):
        conn = self

        class C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                conn.calls.append((sql, params))

            def fetchone(self):
                return conn.row
        return C()

    def close(self):
        pass


def _feed(monkeypatch, row):
    import psycopg2
    calls = []
    monkeypatch.setattr(events, "load_database_url", lambda: "postgresql://fake")
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _FeedConn(row, calls))
    return calls


def test_backend_has_no_jobber_client():
    """Felix 2026-10-20: one Jobber reader (Hermes). The backend reads the feed table."""
    import inspect
    src = inspect.getsource(events)
    assert "api.getjobber.com" not in src
    assert "softy_dashboard_app_kv" not in src
    assert "refresh-token" not in src
    assert events.dispatch_titles_for_driver("Emery N", DAY) is None


def test_feed_row_is_read_and_cached_60s(monkeypatch):
    node = {"id": "v1", "property": {"address": {"street": "1 Main", "city": "Hayward"}}}
    calls = _feed(monkeypatch, (7, "ok", [node], 120.0, None))
    assert events._fetch_visits(DAY) == [node]
    assert events._fetch_visits(DAY) == [node]
    assert len(calls) == 1
    assert "driver_coach_test.events_today" in calls[0][0]
    assert events._CACHE_TTL_S == 60
    assert events.feed_state(DAY) == {"status": "ok", "version": 7, "reason": ""}


@pytest.mark.parametrize("row,reason", [
    (None, "no events published for today yet"),
    ((3, "error", [], 60.0, "boom"), "last Jobber pull failed"),
    ((3, "ok", [], 99999.0, None), "events feed is stale"),
])
def test_feed_failure_is_unknown_and_not_cached(monkeypatch, row, reason):
    calls = _feed(monkeypatch, row)
    assert events._fetch_visits(DAY) is None
    assert events._fetch_visits(DAY) is None
    assert len(calls) == 2
    assert events.feed_state(DAY)["status"] == "unknown"
    assert events.feed_state(DAY)["reason"] == reason


def test_events_with_status_reports_unknown(monkeypatch):
    _feed(monkeypatch, None)
    rows, status = events.get_today_events_with_status("Leila Temp", DAY)
    assert rows == [] and status["status"] == "unknown"


def test_recurring_visits_are_scheduled_stops_not_events(monkeypatch):
    bingo = {"id": "r1", "title": "Bingo Night", "startAt": "2026-10-17T01:00:00Z",
             "endAt": "2026-10-17T02:00:00Z", "visitStatus": "TODAY",
             "job": {"jobType": "RECURRING"},
             "property": {"address": {"street": "9 Elm", "city": "Hayward"}},
             "assignedUsers": {"nodes": [{"name": {"full": "Emery N"}}]}}
    _feed(monkeypatch, (1, "ok", [bingo], 10.0, None))
    assert events.filter_and_format([bingo], "Emery N") == []
    rows = events.scheduled_visits_for_driver("Emery N", DAY)
    assert rows[0]["kind"] == "visit" and rows[0]["startTime"] == "6:00 PM"


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        if "geotab_route_masters" in compact:
            self.rows = self.conn.live
        elif "JOIN route_timed_stops" in compact:
            self.rows = self.conn.candidates
        elif "FROM route_timed_stops" in compact:
            self.rows = self.conn.stops
        else:
            self.rows = []

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, live, candidates, stops):
        self.live = live
        self.candidates = candidates
        self.stops = stops

    def cursor(self):
        return _Cursor(self)

    def close(self):
        return None


ROUTES = [
    (1, "RedwoodCity-Connecticut"),
    (2, "SanJose-Lincoln"),
    (4, "Hayward-Main"),
    (5, "DalyCity-Mission"),
    (3, "Oakland-Broadway"),
    (6, "Fremont-Auto"),
]

STOPS = [
    (1, 1, "410, Connecticut St, Redwood City, CA"),
    (2, 1, "Lincoln Elementary, Panama Street, San Jose, CA"),
    (4, 1, "10, Main St, Hayward, CA"),
    (5, 1, "20, Mission St, Daly City, CA"),
    (3, 1, "1, Broadway, Oakland, CA"),
    (6, 1, "5, Auto Mall, Fremont, CA"),
]


def _visits():
    def row(city, street, truck, assigned_name):
        return {
            "title": f"T{truck}",
            "property": {"address": {"street": street, "city": city, "province": "CA"}},
            "job": {"jobType": "RECURRING", "title": city},
            "assignedUsers": {"nodes": [{"name": {"full": assigned_name}}]},
        }

    return [
        row("Redwood City", "410 Connecticut St", 13, "Emery N"),
        row("San Jose", "100 Lincoln", 13, "Emery N"),
        row("Hayward", "10 Main St", 13, "Emery N"),
        row("Daly City", "20 Mission St", 13, "Emery N"),
        row("Oakland", "1 Broadway", 7, "Other Person"),
        row("Pacifica", "9 Coast", 9, "Other Person"),
    ]


def test_picker_shortlists_likely_routes_without_changing_the_proposal(monkeypatch):
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(jobber, "driver_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(events, "_fetch_visits", lambda day: _visits())
    monkeypatch.setattr(assignment, "_master_connection", lambda: _Conn([], ROUTES, STOPS))

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert result["route_cluster_id"] is None
    assert result["route_name"] is None
    assert result["confidence"] == "picker"
    assert result["dow"] == "Friday"
    assert result["season_variant"] == "school"
    assert [row["city"] for row in result["jobber_visits"]] == [
        "Redwood City", "San Jose", "Hayward", "Daly City", "Oakland", "Pacifica",
    ]
    assert result["jobber_visits"][0]["assigned"] is True
    assert result["jobber_visits"][4]["assigned"] is False

    by_id = {row["route_cluster_id"]: row for row in result["candidates"]}
    redwood = by_id[1]
    assert redwood["name"] == "RedwoodCity-Connecticut"
    assert redwood["area"] == "Redwood City"
    assert redwood["cities"] == ["Redwood City"]
    assert redwood["stops"] == [{"name": "Connecticut St", "city": "Redwood City"}]
    assert redwood["match_score"] == 2
    assert redwood["likely"] is True
    assert by_id[3]["match_score"] == 1
    assert "likely" not in by_id[3]
    assert by_id[6]["match_score"] == 0
    assert "likely" not in by_id[6]
    likely_ids = sorted(row["route_cluster_id"] for row in result["candidates"] if row.get("likely"))
    assert likely_ids == [1, 2, 4, 5]
    assert [row["route_cluster_id"] for row in result["candidates"]] == [route[0] for route in ROUTES]
    mirrored = {row["route_cluster_id"]: row for row in result["all_candidates"]}
    assert mirrored[1]["likely"] is True
    assert mirrored[1]["stops"] == redwood["stops"]


def test_one_live_master_stays_the_proposal(monkeypatch):
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: "Redwood City")
    monkeypatch.setattr(events, "_fetch_visits", lambda day: _visits()[:1])
    monkeypatch.setattr(
        assignment,
        "_master_connection",
        lambda: _Conn([(11, "RedwoodCity-Connecticut")], ROUTES, STOPS),
    )

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert result["route_cluster_id"] == 11
    assert result["route_name"] == "RedwoodCity-Connecticut"
    assert result["source"] == "live"
    assert result["confidence"] == "high"
    assert result["area"] == "Redwood City"
    assert result["candidates"][0]["likely"] is True
    assert result["candidates"][0]["match_score"] == 2
    assert result["jobber_visits"][0]["address"] == "410 Connecticut St, Redwood City, CA"


def test_stop_city_and_south_sf_alias():
    assert assignment._city_from_stop_address("410, Connecticut St, Redwood City, CA") == "Redwood City"
    assert assignment._city_from_stop_address(
        "Lincoln Elementary, Panama Street, San Jose, CA"
    ) == "San Jose"
    assert assignment._city_from_stop_address("410, Connecticut St") is None
    detail = assignment._detail_from_stops(
        "SouthSanFrancisco-SignHill",
        [{"name": "Sign Hill", "city": "South San Francisco"}],
    )
    score = assignment._match_score(
        "SouthSanFrancisco-SignHill",
        detail,
        [{"city": "South SF", "assigned": True}],
    )
    assert detail["area"] == "South San Francisco"
    assert score == 2


def test_jobber_and_master_outages_still_return_an_assignment(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(events, "_fetch_visits", boom)
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(jobber, "driver_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(assignment, "_master_connection", boom)

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert result["jobber_visits"] == []
    assert result["candidates"] == []
    assert result["all_candidates"] == []
    assert result["route_cluster_id"] is None
    assert "likely" not in result


def test_signin_puts_jobber_visits_on_assignment_and_fills_a_missing_list(monkeypatch):
    from app import service
    from app.refcore import DriverProfile, ResolvedFlags

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            return None

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(service, "connect", lambda schema, autocommit=False: Conn())
    monkeypatch.setattr(
        service.profiles, "build_driver_profile", lambda *a, **k: DriverProfile(driver_id="jobber:emery_n")
    )
    monkeypatch.setattr(
        service.flagstate,
        "advance_for_signin",
        lambda *a, **k: ResolvedFlags(coached=False, arrival_voice=False),
    )
    monkeypatch.setattr(service.events_mod, "get_today_events", lambda *a, **k: [])
    monkeypatch.setattr(
        service.jobber,
        "resolve_driver_of_record",
        lambda name: {"driver_id": "jobber:emery_n", "square_name": name, "picked_name": name},
    )
    visits = [{
        "city": "Redwood City",
        "address": "410 Connecticut St, Redwood City, CA",
        "truck": 13,
        "assigned": True,
    }]
    proposed = {
        "route_cluster_id": None,
        "route_name": None,
        "source": "candidate",
        "season_variant": "school",
        "confidence": "picker",
        "area": None,
        "dow": "Friday",
        "candidates": [{"route_cluster_id": 1, "name": "RedwoodCity-Connecticut"}],
        "all_candidates": [{"route_cluster_id": 1, "name": "RedwoodCity-Connecticut"}],
        "jobber_visits": visits,
    }
    monkeypatch.setattr(service.assignment, "resolve_assignment", lambda *a, **k: proposed)

    body = service.do_signin(service.SignInRequest(name="Emery N", truck=13), schema="driver_coach")

    assert body["assignment"]["jobber_visits"] == visits
    assert body["assignment"]["candidates"][0]["route_cluster_id"] == 1
    assert "coached" not in body

    proposed.pop("jobber_visits")
    body = service.do_signin(service.SignInRequest(name="Emery N", truck=13), schema="driver_coach")
    assert body["assignment"]["jobber_visits"] == []


class _StopCursor:
    """Dispatches the sign-in reads, including timed-stop counts and live master counts."""

    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.conn.statements.append(compact)
        upper = compact.upper()
        assert "DELETE " not in upper
        assert "UPDATE " not in upper
        assert "INSERT " not in upper
        if "geotab_route_master_stops" in compact:
            self.rows = self.conn.geotab_counts
        elif "geotab_route_masters" in compact:
            self.rows = self.conn.live
        elif "JOIN route_timed_stops" in compact:
            self.rows = self.conn.candidates
        elif "FROM route_timed_stops" in compact:
            self.rows = self.conn.stops
        elif "FROM route_names" in compact:
            self.rows = self.conn.fallback
        else:
            self.rows = []

    def fetchall(self):
        return list(self.rows)


class _StopConn:
    def __init__(self, live, candidates, stops, geotab_counts=None, fallback=None):
        self.live = live
        self.candidates = candidates
        self.stops = stops
        self.geotab_counts = geotab_counts or []
        self.fallback = fallback or []
        self.statements = []

    def cursor(self):
        return _StopCursor(self)

    def close(self):
        return None


def _ids(rows):
    return [row["route_cluster_id"] for row in rows]


def test_min_route_stops_defaults_to_five_and_honors_overrides(monkeypatch):
    monkeypatch.delenv("MIN_ROUTE_STOPS", raising=False)
    assert assignment._min_route_stops() == 5
    assert assignment.DEFAULT_MIN_ROUTE_STOPS == 5
    for raw in ("", "   ", "nope", "-3", "1.5"):
        monkeypatch.setenv("MIN_ROUTE_STOPS", raw)
        assert assignment._min_route_stops() == 5
    monkeypatch.setenv("MIN_ROUTE_STOPS", "2")
    assert assignment._min_route_stops() == 2
    monkeypatch.setenv("MIN_ROUTE_STOPS", "0")
    assert assignment._min_route_stops() == 0


def test_picker_hides_routes_with_fewer_than_five_stops(monkeypatch):
    counted = [
        (1, "RedwoodCity-Connecticut", 22),
        (2, "SanJose-Lincoln", 5),
        (4, "Hayward-Main", 4),
        (5, "DalyCity-Mission", 1),
        (3, "Oakland-Broadway", 18),
        (6, "Fremont-Auto", 2),
    ]
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(jobber, "driver_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(events, "_fetch_visits", lambda day: [])
    monkeypatch.setattr(assignment, "_master_connection", lambda: _StopConn([], counted, []))

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert result["route_cluster_id"] is None
    assert _ids(result["candidates"]) == [1, 2, 3]
    assert _ids(result["all_candidates"]) == [1, 2, 3]
    assert result["candidates"][1]["name"] == "SanJose-Lincoln"


def test_min_route_stops_env_lowers_and_disables_the_floor(monkeypatch):
    counted = [
        (1, "RedwoodCity-Connecticut", 22),
        (5, "DalyCity-Mission", 1),
        (6, "Fremont-Auto", 2),
    ]
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(jobber, "driver_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(events, "_fetch_visits", lambda day: [])

    monkeypatch.setenv("MIN_ROUTE_STOPS", "2")
    monkeypatch.setattr(assignment, "_master_connection", lambda: _StopConn([], counted, []))
    result = assignment.resolve_assignment("Emery N", 13, DAY)
    assert _ids(result["candidates"]) == [1, 6]
    assert _ids(result["all_candidates"]) == [1, 6]

    monkeypatch.setenv("MIN_ROUTE_STOPS", "0")
    monkeypatch.setattr(assignment, "_master_connection", lambda: _StopConn([], counted, []))
    result = assignment.resolve_assignment("Emery N", 13, DAY)
    assert _ids(result["candidates"]) == [1, 5, 6]
    assert _ids(result["all_candidates"]) == [1, 5, 6]


def test_proposed_live_route_stays_even_with_one_stop(monkeypatch):
    counted = [
        (11, "RedwoodCity-Stub", 1),
        (1, "RedwoodCity-Connecticut", 22),
        (5, "DalyCity-Mission", 1),
        (2, "SanJose-Lincoln", 18),
    ]
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: "Redwood City")
    monkeypatch.setattr(events, "_fetch_visits", lambda day: [])
    monkeypatch.setattr(
        assignment,
        "_master_connection",
        lambda: _StopConn([(11, "RedwoodCity-Stub")], counted, []),
    )

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert result["route_cluster_id"] == 11
    assert result["route_name"] == "RedwoodCity-Stub"
    assert result["source"] == "live"
    assert _ids(result["candidates"]) == [11]
    assert _ids(result["all_candidates"]) == [11, 1, 2]


def test_proposed_city_route_stays_when_it_is_the_only_area_match(monkeypatch):
    counted = [
        (11, "RedwoodCity-Stub", 2),
        (5, "DalyCity-Mission", 1),
        (2, "SanJose-Lincoln", 18),
    ]
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: "Redwood City")
    monkeypatch.setattr(events, "_fetch_visits", lambda day: [])
    monkeypatch.setattr(assignment, "_master_connection", lambda: _StopConn([], counted, []))

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert result["route_cluster_id"] == 11
    assert result["confidence"] == "medium"
    assert _ids(result["candidates"]) == [11]
    assert _ids(result["all_candidates"]) == [11, 2]


def test_area_picker_drops_sparse_routes_from_both_lists(monkeypatch):
    counted = [
        (1, "RedwoodCity-Connecticut", 22),
        (11, "RedwoodCity-Stub", 1),
        (2, "SanJose-Lincoln", 18),
        (6, "Fremont-Auto", 3),
    ]
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: "Redwood City")
    monkeypatch.setattr(events, "_fetch_visits", lambda day: [])
    monkeypatch.setattr(assignment, "_master_connection", lambda: _StopConn([], counted, []))

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert result["route_cluster_id"] is None
    assert result["confidence"] == "picker"
    assert _ids(result["candidates"]) == [1]
    assert _ids(result["all_candidates"]) == [1, 2]


def test_live_master_with_a_full_stop_list_is_not_hidden_for_sparse_timed_rows(monkeypatch):
    counted = [
        (4, "Hayward-Main", 1),
        (1, "RedwoodCity-Connecticut", 22),
    ]
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(jobber, "driver_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(events, "_fetch_visits", lambda day: [])
    monkeypatch.setattr(
        assignment,
        "_master_connection",
        lambda: _StopConn([], counted, [], geotab_counts=[(4, 20)]),
    )

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert result["route_cluster_id"] is None
    assert _ids(result["candidates"]) == [4, 1]
    assert _ids(result["all_candidates"]) == [4, 1]


def test_short_live_master_does_not_keep_a_sparse_route(monkeypatch):
    counted = [
        (4, "Hayward-Main", 1),
        (1, "RedwoodCity-Connecticut", 22),
    ]
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(jobber, "driver_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(events, "_fetch_visits", lambda day: [])
    monkeypatch.setattr(
        assignment,
        "_master_connection",
        lambda: _StopConn([], counted, [], geotab_counts=[(4, 3)]),
    )

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert _ids(result["candidates"]) == [1]
    assert _ids(result["all_candidates"]) == [1]


def test_fallback_routes_without_a_stop_count_stay_visible(monkeypatch):
    monkeypatch.setattr(jobber, "dispatch_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(jobber, "driver_area_for_day", lambda *a, **k: None)
    monkeypatch.setattr(events, "_fetch_visits", lambda day: [])
    monkeypatch.setattr(
        assignment,
        "_master_connection",
        lambda: _StopConn([], [], [], fallback=[(8, "Oakland-Broadway"), (9, "Fremont-Auto")]),
    )

    result = assignment.resolve_assignment("Emery N", 13, DAY)

    assert _ids(result["candidates"]) == [8, 9]
    assert _ids(result["all_candidates"]) == [8, 9]

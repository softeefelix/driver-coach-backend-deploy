"""SERVE_MASTER rollout: serve Andrew's master verbatim, else fall back to the algorithm.

Live part skips unless MASTER_ROUTE_DATABASE_URL is set. READ-ONLY: the connection
is opened in Postgres read-only mode; nothing is written.
"""
import datetime
import os

import pytest

from app import assignment, master_plan, routes

PACIFICA = None  # resolved by name below
THU_SCHOOL_DAY = datetime.date(2026, 10, 22)  # a Thursday in school season


def test_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("SERVE_MASTER", raising=False)
    assert master_plan.serve_mode() == "off"
    monkeypatch.setenv("SERVE_MASTER", "bogus")
    assert master_plan.serve_mode() == "off"
    monkeypatch.setenv("SERVE_MASTER", "Approved")
    assert master_plan.serve_mode() == "approved"


def test_off_never_queries():
    class Boom:
        def execute(self, *a, **k):
            raise AssertionError("must not query when off")
    assert master_plan.resolve_master(Boom(), 1, "Monday", mode="off") is None


def test_leave_by_from_dwell():
    assert master_plan._add_minutes("13:55", 80 * 60) == "15:15"
    assert master_plan._add_minutes(None, 60) is None
    assert master_plan._add_minutes("23:50", 3600) == "23:59"


live = pytest.mark.skipif(not os.environ.get("MASTER_ROUTE_DATABASE_URL"),
                          reason="needs Master Route DB")


@pytest.fixture
def cur():
    c = assignment._master_connection()  # readonly=True
    with c.cursor() as k:
        yield k
    c.close()


def _rc(cur, name):
    cur.execute("SELECT route_cluster_id FROM route_names WHERE name = %s", (name,))
    return int(cur.fetchone()[0])


@live
def test_pacifica_thursday_serves_40_master_stops_in_order(cur, monkeypatch):
    monkeypatch.setenv("SERVE_MASTER", "all")
    monkeypatch.setattr(routes, "pacific_date", lambda: THU_SCHOOL_DAY)
    rc = _rc(cur, "Pacifica-CabrilloSchool")
    cur.execute(
        """SELECT s.stop_order, s.lat, s.lon FROM geotab_route_master_stops s
           JOIN geotab_route_masters m ON m.route_cluster_id = s.route_cluster_id
             AND m.dow = s.dow AND m.season_variant = s.season_variant
           WHERE s.route_cluster_id = %s AND s.dow = 'Thursday' AND m.active AND m.live
             AND s.season_variant = 'school' ORDER BY s.stop_order""", (rc,))
    expected = cur.fetchall()
    assert len(expected) == 40
    served = routes.ordered_stops_for_route(cur, rc, "Thursday")
    assert len(served) == 40
    assert {s["plan_source"] for s in served} == {"master"}
    assert [(s["stop_order"], s["lat"], s["lng"]) for s in served] == \
        [(o, float(a), float(b)) for o, a, b in expected]
    first = routes.next_stop_for_route(cur, rc, "Thursday", after_order=0)
    assert first["stop_order"] == 1 and first["plan_source"] == "master"


@live
def test_unapproved_falls_back_to_algorithm(cur, monkeypatch):
    # No master is approved yet (or the migration is not applied): 'approved' mode
    # must serve the timed build, tagged as algorithm.
    monkeypatch.setenv("SERVE_MASTER", "approved")
    monkeypatch.setattr(routes, "pacific_date", lambda: THU_SCHOOL_DAY)
    rc = _rc(cur, "Pacifica-CabrilloSchool")
    served = routes.ordered_stops_for_route(cur, rc, "Thursday")
    assert served, "timed build should exist for Pacifica Thursday"
    assert {s["plan_source"] for s in served} == {"algorithm"}


@live
def test_off_is_todays_behaviour(cur, monkeypatch):
    monkeypatch.delenv("SERVE_MASTER", raising=False)
    rc = _rc(cur, "Pacifica-CabrilloSchool")
    served = routes.ordered_stops_for_route(cur, rc, "Thursday")
    assert {s["plan_source"] for s in served} == {"algorithm"}


@live
def test_route_without_master_falls_back(cur, monkeypatch):
    monkeypatch.setenv("SERVE_MASTER", "all")
    cur.execute(
        """SELECT t.route_cluster_id FROM route_timed_stops t
           WHERE t.dow = 'Thursday' AND """ + routes.latest_build_sql("t") + """
             AND NOT EXISTS (SELECT 1 FROM geotab_route_masters m
                 WHERE m.route_cluster_id = t.route_cluster_id AND m.dow = 'Thursday')
           GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""")
    rc = int(cur.fetchone()[0])
    served = routes.ordered_stops_for_route(cur, rc, "Thursday")
    assert served and {s["plan_source"] for s in served} == {"algorithm"}

"""Idle episodes and breaks against a disposable Postgres, never a fleet database."""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import uuid

import psycopg2
import pgserver
import pytest

from app import breaks, no_idle, service

T0 = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
MIGRATION = (Path(__file__).parents[1] / "migrations" / "007_no_idle_breaks.sql").read_text()
SESSION = """
CREATE TABLE IF NOT EXISTS driver_coach_session (
    session_id uuid PRIMARY KEY,
    truck_no int,
    coached bool DEFAULT false,
    route_cluster_id int,
    driver_id text,
    plan_snapshot jsonb DEFAULT '[]',
    served_stop_orders jsonb DEFAULT '[]',
    skipped_stop_orders jsonb DEFAULT '[]'
)
"""


@pytest.fixture(scope="module")
def database():
    root = Path(__import__("tempfile").mkdtemp(prefix="no-idle-", dir=os.environ.get("TMPDIR", "/tmp")))
    server = None
    try:
        server = pgserver.get_server(root / "data", cleanup_mode="delete")
        yield server.get_uri()
    finally:
        if server is not None:
            server.cleanup()
        shutil.rmtree(root, ignore_errors=True)


def _prepare(database, schema):
    conn = psycopg2.connect(database)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(SESSION)
            cur.execute(MIGRATION)
            cur.execute(MIGRATION)
    finally:
        conn.close()


@pytest.fixture
def schema(database):
    name = "driver_coach"
    _prepare(database, name)
    return name


@contextmanager
def _connect(database, schema):
    conn = psycopg2.connect(database)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}", public')
        yield conn
    finally:
        conn.close()


def test_route_poll_survives_a_missing_migration(database, monkeypatch):
    """Navigation still returns if 007 has not been applied yet."""
    schema = "driver_coach_unmigrated"
    conn = psycopg2.connect(database)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(SESSION)
            cur.execute(
                "INSERT INTO driver_coach_session (session_id, truck_no, driver_id, route_cluster_id, plan_snapshot) "
                "VALUES (%s, 7, 'amina', 42, '[]')",
                ("11111111-1111-1111-1111-111111111111",),
            )
    finally:
        conn.close()
    _patch_connect(monkeypatch, database, schema)
    monkeypatch.setattr(service.routes, "resolve_live_route", lambda *a, **k: {
        "next_stop": None, "route_cluster_id": 42, "phase": "driving", "shift_phase": "plan",
        "route_count": "1 / 1", "up_next": [], "map": None, "turns": [], "parked_stop": None,
        "driven_stops": [], "match_plan": [], "position": {"lat": 37.8, "lng": -122.2, "speed": 0, "fix_age_s": 1},
    })
    monkeypatch.setattr(service.jobber, "name_from_driver_id", lambda *a: "")
    monkeypatch.setattr(service.events_mod, "get_today_events", lambda *a: [])
    monkeypatch.setattr(service.scorecard, "live_ticker", lambda *a: None)
    monkeypatch.setattr(service, "refuel_reminder_for_route_poll", lambda *a, **k: None)
    body = service.do_route("11111111-1111-1111-1111-111111111111", schema)
    assert body["phase"] == "driving"
    assert body["nudge"] is None
    assert body["break"]["active"] is False


def test_migration_is_idempotent_and_refuses_public(database):
    _prepare(database, "driver_coach")
    _prepare(database, "driver_coach_test")
    conn = psycopg2.connect(database)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            with pytest.raises(psycopg2.Error):
                cur.execute(MIGRATION)
        conn.rollback()
        with conn.cursor() as cur:
            for table in ("idle_episode", "shift_break", "shift_break_flag"):
                cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                assert cur.fetchone()[0] is None
    finally:
        conn.close()


def _session(cur, sid):
    cur.execute(
        "INSERT INTO driver_coach_session (session_id, truck_no, driver_id, route_cluster_id) VALUES (%s,%s,%s,%s)",
        (sid, 7, "amina_cole", 42),
    )


def _patch_connect(monkeypatch, database, schema):
    @contextmanager
    def connect(schema=schema, autocommit=False):
        conn = psycopg2.connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}", public')
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(service, "connect", connect)


def test_breaks_allowances_overrun_and_flags(database, schema, monkeypatch):
    _patch_connect(monkeypatch, database, schema)
    sid = str(uuid.uuid4())
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            _session(cur, sid)
        conn.commit()

    client_start = service.do_start_break(service.BreakStartRequest(session_id=sid, type="lunch"))
    assert client_start["break"]["active"] is True
    assert client_start["break"]["allowedS"] == 1800
    assert client_start["break"]["remainingS"] == 1800
    assert client_start["break"]["lunchUsed"] == 1

    with pytest.raises(breaks.BreakError) as limited:
        service.do_start_break(service.BreakStartRequest(session_id=sid, type="lunch"))
    assert limited.value.status == 409 and limited.value.detail["error"] == "break_active"

    ended = service.do_end_break(service.BreakEndRequest(session_id=sid))
    assert ended["ended"]["reason"] == "driver" and ended["break"]["active"] is False

    with pytest.raises(breaks.BreakError) as second_lunch:
        service.do_start_break(service.BreakStartRequest(session_id=sid, type="lunch"))
    assert second_lunch.value.status == 409
    assert second_lunch.value.detail["error"] == "over_limit"
    assert second_lunch.value.detail["break"]["latestFlag"]["kind"] == "over_limit"

    for _ in range(2):
        started = service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
        assert started["break"]["type"] == "break"
        service.do_end_break(service.BreakEndRequest(session_id=sid))
    with pytest.raises(breaks.BreakError) as third:
        service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
    assert third.value.detail["error"] == "over_limit"
    assert third.value.detail["message"] == "This shift already used 2 breaks."

    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shift_break WHERE session_id=%s AND break_type='lunch'", (sid,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM shift_break WHERE session_id=%s AND break_type='break'", (sid,))
            assert cur.fetchone()[0] == 2
            cur.execute(
                "SELECT kind FROM shift_break_flag WHERE session_id=%s ORDER BY created_at",
                (sid,),
            )
            assert [row[0] for row in cur.fetchall()] == ["over_limit", "over_limit"]

    with pytest.raises(breaks.BreakError) as bad_type:
        service.do_start_break(service.BreakStartRequest(session_id=sid, type="nope"))
    assert bad_type.value.status == 400 and bad_type.value.detail["error"] == "bad_type"
    with pytest.raises(KeyError):
        service.do_start_break(service.BreakStartRequest(session_id=str(uuid.uuid4()), type="lunch"))


def test_idle_episode_records_nudges_and_poll_hides_them_on_break(database, schema, monkeypatch):
    _patch_connect(monkeypatch, database, schema)
    sid = str(uuid.uuid4())
    pin = {"stop_order": 2, "address": "100, Panama Street, Oakland", "lat": 37.8, "lng": -122.2}
    position = {"lat": 37.8, "lng": -122.2, "speed": 0, "fix_age_s": 1}
    live = {
        "next_stop": pin,
        "route_cluster_id": 42,
        "phase": "driving",
        "shift_phase": "plan",
        "route_count": "1 / 3",
        "up_next": [],
        "map": None,
        "turns": [],
        "parked_stop": None,
        "driven_stops": [],
        "match_plan": [pin],
        "position": position,
    }
    monkeypatch.setattr(service.routes, "resolve_live_route", lambda *a, **k: live)
    monkeypatch.setattr(service.jobber, "name_from_driver_id", lambda *a: "Amina Cole")
    monkeypatch.setattr(service.events_mod, "get_today_events", lambda *a: [])
    monkeypatch.setattr(service.scorecard, "live_ticker", lambda *a: None)
    monkeypatch.setattr(service.scorecard, "last_truck_sale", lambda **k: {"known": True, "last_sale_at": None})
    monkeypatch.setattr(service, "refuel_reminder_for_route_poll", lambda *a, **k: None)

    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            _session(cur, sid)
            cur.execute(
                "UPDATE driver_coach_session SET plan_snapshot=%s::jsonb WHERE session_id=%s",
                ('[{"stop_order": 2}]', sid),
            )
        conn.commit()

    first = service.do_route(sid, schema)
    assert first["nudge"] is None
    assert first["break"]["active"] is False
    assert first["break"]["lunchAllowed"] == 1
    assert "banner" not in first

    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE idle_episode SET started_at = started_at - interval '90 seconds' WHERE session_id=%s",
                (sid,),
            )
        conn.commit()

    second = service.do_route(sid, schema)
    assert second["nudge"]["level"] == "banner"
    assert second["nudge"]["atStop"] is True
    assert "Panama Street" in second["nudge"]["message"]
    assert second["nudge"]["managerAlert"] is False
    assert second["nudge"]["noSaleForS"] is None

    view = no_idle.motion_view(position, phase="driving", parked_stop=None, plan=[pin])
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nudge_since FROM idle_episode WHERE session_id=%s AND ended_at IS NULL", (sid,))
            nudge_since = cur.fetchone()[0]
            voice, wrote = no_idle.reconcile_on_poll(
                cur, session_id=sid, truck_no=7, driver_id="amina_cole", view=view,
                sale={"known": True, "last_sale_at": None},
                now=nudge_since + timedelta(seconds=60),
            )
            assert voice["level"] == "voice" and wrote
            manager, _ = no_idle.reconcile_on_poll(
                cur, session_id=sid, truck_no=7, driver_id="amina_cole", view=view,
                sale={"known": True, "last_sale_at": None},
                now=nudge_since + timedelta(seconds=180),
            )
            assert manager["level"] == "manager" and manager["managerAlertFor"] == "Felix"
            cur.execute(
                """
                SELECT manager_alerted, manager_alert_for, nudges, lat, lng, at_stop
                FROM idle_episode WHERE session_id=%s AND ended_at IS NULL
                """,
                (sid,),
            )
            flagged, who, nudges, lat, lng, at_stop = cur.fetchone()
            if isinstance(nudges, str):
                nudges = json.loads(nudges)
            assert flagged is True and who == "Felix" and at_stop is True
            assert lat == pytest.approx(37.8) and lng == pytest.approx(-122.2)
            assert [item["level"] for item in nudges] == ["banner", "voice", "manager"]

            stale = no_idle.MotionView(
                fresh=False, stationary=False, moving=False, at_stop=False, fix_age_s=10_000,
            )
            hidden, stale_wrote = no_idle.reconcile_on_poll(
                cur, session_id=sid, truck_no=7, driver_id="amina_cole", view=stale,
                sale={"known": True, "last_sale_at": None}, now=nudge_since + timedelta(seconds=200),
            )
            assert hidden is None and stale_wrote is False

            moved_view = no_idle.MotionView(
                fresh=True, stationary=False, moving=True, at_stop=False, speed_mph=8, fix_age_s=1,
            )
            gone, _ = no_idle.reconcile_on_poll(
                cur, session_id=sid, truck_no=7, driver_id="amina_cole", view=moved_view,
                sale={"known": True, "last_sale_at": None}, now=nudge_since + timedelta(seconds=210),
            )
            assert gone is None
            cur.execute(
                "SELECT end_reason, duration_s IS NOT NULL, ended_at IS NOT NULL FROM idle_episode WHERE session_id=%s",
                (sid,),
            )
            reason, has_duration, has_end = cur.fetchone()
            assert reason == "moved" and has_duration and has_end
        conn.commit()

    service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
    # A due idle opened while the break is on must be closed, not nudged.
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO idle_episode (
                    id, session_id, truck_no, driver_id, started_at, lat, lng, at_stop, nudges
                ) VALUES (%s,%s,7,'amina_cole', %s, 37.8, -122.2, true, '[]')
                """,
                (str(uuid.uuid4()), sid, datetime.now(timezone.utc) - timedelta(minutes=5)),
            )
        conn.commit()
    during = service.do_route(sid, schema)
    assert during["break"]["active"] is True and during["break"]["type"] == "break"
    assert during["nudge"] is None
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT end_reason FROM idle_episode WHERE session_id=%s AND end_reason='break'",
                (sid,),
            )
            assert cur.fetchone()[0] == "break"

    # Movement and expiry auto-end the break. A late expiry is flagged.
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, started_at, allowed_s FROM shift_break WHERE session_id=%s AND ended_at IS NULL", (sid,))
            break_id, started_at, allowed = cur.fetchone()
            state, wrote = breaks.reconcile_on_poll(
                cur, session_id=sid, truck_no=7, driver_id="amina_cole",
                fresh=True, moving=True, now=started_at + timedelta(seconds=30),
            )
            assert wrote and state["active"] is False
            cur.execute("SELECT end_reason, flagged FROM shift_break WHERE id=%s", (break_id,))
            assert cur.fetchone() == ("moving", False)
            lunch = breaks.start(cur, session_id=sid, break_type="lunch", now=T0)
            assert lunch["break"]["active"] is True and lunch["break"]["allowedS"] == 1800
            late, wrote_late = breaks.reconcile_on_poll(
                cur, session_id=sid, truck_no=7, driver_id="amina_cole",
                fresh=True, moving=False, now=T0 + timedelta(seconds=1861),
            )
            assert wrote_late and late["active"] is False
            assert late["latestFlag"]["kind"] == "overrun"
            assert late["latestFlag"]["overrunS"] == 61
            cur.execute(
                "SELECT end_reason, flagged, overrun_s FROM shift_break WHERE session_id=%s AND break_type='lunch'",
                (sid,),
            )
            assert cur.fetchone() == ("expired", True, 61)
        conn.commit()

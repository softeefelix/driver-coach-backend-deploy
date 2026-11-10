"""Office key, Square-offline totals, and the Hermes idle scorecard ingest.

Disposable Postgres only. No fleet database and no environment secrets are set
outside monkeypatch.
"""
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import psycopg2
import pgserver
import pytest
from fastapi.testclient import TestClient

from app import idle_scorecard, idle_status, service

T0 = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1]
MIGRATION = (ROOT / "migrations" / "009_idle_scorecard.sql").read_text()
SESSION = """
CREATE TABLE IF NOT EXISTS driver_coach_session (
    session_id uuid PRIMARY KEY,
    truck_no int,
    coached bool DEFAULT false,
    route_cluster_id int,
    driver_id text,
    plan_snapshot jsonb DEFAULT '[]',
    served_stop_orders jsonb DEFAULT '[]',
    skipped_stop_orders jsonb DEFAULT '[]',
    started_at timestamptz
)
"""
DRIVER = {
    "driverId": "amina_cole",
    "truckNo": 7,
    "idleMinutesToday": 12,
    "idleMinutesWeek": 40,
    "fleetRank": 3,
    "fleetSize": 18,
    "cleanDayStreak": 4,
}
BODY = {
    "day": "2026-10-15",
    "asOf": "2026-10-15T18:00:00Z",
    "drivers": [DRIVER],
}


@pytest.fixture(scope="module")
def database():
    root = Path(__import__("tempfile").mkdtemp(prefix="office-score-", dir=os.environ.get("TMPDIR", "/tmp")))
    server = None
    try:
        server = pgserver.get_server(root / "data", cleanup_mode="delete")
        yield server.get_uri()
    finally:
        if server is not None:
            server.cleanup()
        __import__("shutil").rmtree(root, ignore_errors=True)


def _prepare(database, schema, *, scorecard: bool):
    conn = psycopg2.connect(database)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(SESSION)
            if scorecard:
                cur.execute(MIGRATION)
                cur.execute("TRUNCATE idle_scorecard, driver_coach_session")
            else:
                cur.execute("TRUNCATE driver_coach_session")
    finally:
        conn.close()


def _patch(monkeypatch, database, schema):
    @contextmanager
    def connect(schema=schema, autocommit=False, readonly=False):
        conn = psycopg2.connect(database)
        try:
            if readonly:
                conn.set_session(readonly=True, autocommit=False)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}", public')
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(service, "connect", connect)


def _route_stubs(monkeypatch):
    monkeypatch.setattr(service.no_idle, "utcnow", lambda: T0)
    monkeypatch.setattr(service.routes, "resolve_live_route", lambda *a, **k: {
        "next_stop": None, "route_cluster_id": 42, "phase": "driving", "shift_phase": "plan",
        "route_count": "1 / 1", "up_next": [], "map": None, "turns": [], "parked_stop": None,
        "driven_stops": [], "match_plan": [], "position": {"lat": 37.8, "lng": -122.2, "speed": 10, "fix_age_s": 1},
    })
    monkeypatch.setattr(service.jobber, "name_from_driver_id", lambda *a, **k: "")
    monkeypatch.setattr(service.events_mod, "get_today_events_with_status", lambda *a, **k: ([], {"status": "ok"}))
    monkeypatch.setattr(service.events_mod, "scheduled_visits_for_driver", lambda *a, **k: [])
    monkeypatch.setattr(service.scorecard, "live_ticker", lambda *a, **k: None)
    monkeypatch.setattr(service.scorecard, "last_truck_sale", lambda **k: {"known": False, "last_sale_at": None, "reason": "no_token"})
    monkeypatch.setattr(service, "refuel_reminder_for_route_poll", lambda *a, **k: None)


def _session(database, schema, sid):
    conn = psycopg2.connect(database)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(
                """
                INSERT INTO driver_coach_session
                    (session_id, truck_no, driver_id, route_cluster_id, started_at)
                VALUES (%s, 7, 'amina_cole', 42, %s)
                """,
                (sid, T0),
            )
    finally:
        conn.close()


def test_square_offline_totals_chain_within_five_minutes():
    start = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "a", "truck_no": 7, "driver_id": "amina", "started_at": start,
         "ended_at": start + timedelta(minutes=10), "duration_s": 600, "allowed_s": 3600},
        {"id": "b", "truck_no": 7, "driver_id": "amina", "started_at": start + timedelta(minutes=14),
         "ended_at": start + timedelta(minutes=20), "duration_s": 360, "allowed_s": 3600},
        {"id": "c", "truck_no": 7, "driver_id": "amina", "started_at": start + timedelta(minutes=30),
         "ended_at": start + timedelta(minutes=40), "duration_s": 600, "allowed_s": 3600},
    ]
    totals = idle_status.square_offline_totals(rows, start + timedelta(hours=2))
    assert totals == [{
        "truckNo": 7,
        "driverId": "amina",
        "count": 3,
        "minutes": 26,
        "longestChainMinutes": 16,
    }]
    # A start exactly five minutes after the previous end stays in the chain.
    edge = dict(rows[1], id="edge", started_at=start + timedelta(minutes=15),
                ended_at=start + timedelta(minutes=16), duration_s=60)
    chained = idle_status.square_offline_totals([rows[0], edge], start + timedelta(hours=2))
    assert chained[0]["longestChainMinutes"] == 11
    broken = dict(edge, started_at=start + timedelta(minutes=15, seconds=1))
    split = idle_status.square_offline_totals([rows[0], broken], start + timedelta(hours=2))
    assert split[0]["longestChainMinutes"] == 10


def test_clock_capped_open_period_counts_its_cap():
    now = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)
    open_row = {
        "id": "open", "truck_no": 8, "driver_id": "bee",
        "started_at": now - timedelta(hours=2), "ended_at": None,
        "duration_s": None, "allowed_s": 3600,
    }
    shown = idle_status._project(open_row, now)
    assert shown["duration_s"] == 3600 and shown["end_reason"] == "expired"
    totals = idle_status.square_offline_totals([shown], now)
    assert totals[0]["minutes"] == 60 and totals[0]["count"] == 1


def test_office_key_gates_the_read_and_the_ingest(monkeypatch):
    monkeypatch.delenv("DC_OFFICE_KEY", raising=False)
    monkeypatch.setattr(service, "do_idle_exclusions", lambda day=None, schema="driver_coach": {"day": "2026-10-15"})
    client = TestClient(service.app)
    assert client.get("/driver-coach/v1/idle-exclusions").status_code == 200
    assert client.post("/driver-coach/v1/idle-scorecard", json=BODY).status_code == 503

    monkeypatch.setenv("DC_OFFICE_KEY", "office-secret")

    def refuse(*_a, **_k):
        raise AssertionError("idle read ran without a key")

    monkeypatch.setattr(service, "do_idle_exclusions", refuse)
    assert client.get("/driver-coach/v1/idle-exclusions").status_code == 401
    assert client.get(
        "/driver-coach/v1/idle-exclusions", headers={"X-DC-Office-Key": "wrong"}
    ).status_code == 401
    monkeypatch.setattr(service, "do_idle_exclusions", lambda day=None, schema="driver_coach": {"day": "2026-10-15"})
    opened = client.get("/driver-coach/v1/idle-exclusions", headers={"X-DC-Office-Key": "office-secret"})
    assert opened.status_code == 200
    assert client.post("/driver-coach/v1/idle-scorecard", json=BODY).status_code == 401
    assert client.post(
        "/driver-coach/v1/idle-scorecard", json=BODY, headers={"X-DC-Office-Key": "wrong"}
    ).status_code == 401


def test_ingest_rejects_bad_shapes(monkeypatch):
    monkeypatch.setenv("DC_OFFICE_KEY", "office-secret")
    client = TestClient(service.app)
    headers = {"X-DC-Office-Key": "office-secret"}

    def post(body):
        return client.post("/driver-coach/v1/idle-scorecard", json=body, headers=headers)

    assert post({"day": "10/15/2026", "asOf": BODY["asOf"], "drivers": []}).status_code == 400
    assert post({"day": BODY["day"], "asOf": "2026-10-15T18:00:00", "drivers": []}).status_code == 400
    assert post({"day": BODY["day"], "asOf": BODY["asOf"], "drivers": [dict(DRIVER, driverId="")] }).status_code == 400
    assert post({"day": BODY["day"], "asOf": BODY["asOf"], "drivers": [dict(DRIVER, idleMinutesToday=True)] }).status_code == 400
    assert post({"day": BODY["day"], "asOf": BODY["asOf"], "drivers": [dict(DRIVER, fleetRank=19)] }).status_code == 400
    doubled = post({"day": BODY["day"], "asOf": BODY["asOf"], "drivers": [DRIVER, DRIVER]})
    assert doubled.status_code == 400
    assert doubled.json()["detail"]["error"] == "duplicate_driver"


def test_migration_refuses_public(database):
    conn = psycopg2.connect(database)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            with pytest.raises(psycopg2.Error):
                cur.execute(MIGRATION)
        conn.rollback()
    finally:
        conn.close()


def test_ingest_upserts_and_the_poll_reads_one_driver(database, monkeypatch):
    schema = "driver_coach_score"
    _prepare(database, schema, scorecard=True)
    _patch(monkeypatch, database, schema)
    _route_stubs(monkeypatch)
    monkeypatch.setenv("DC_OFFICE_KEY", "office-secret")
    sid = str(uuid.uuid4())
    _session(database, schema, sid)
    client = TestClient(service.app)
    headers = {"X-DC-Office-Key": "office-secret"}
    saved = client.post("/driver-coach/v1/idle-scorecard", json=BODY, headers=headers)
    assert saved.status_code == 200
    assert saved.json() == {"day": "2026-10-15", "upserted": 1}
    revised = dict(BODY, drivers=[dict(DRIVER, idleMinutesToday=15, cleanDayStreak=5)])
    again = client.post("/driver-coach/v1/idle-scorecard", json=revised, headers=headers)
    assert again.status_code == 200 and again.json()["upserted"] == 1

    bare = service.do_route(sid, schema)
    assert bare["scorecard"] == {
        "idleMinutesToday": 15,
        "idleMinutesWeek": 40,
        "fleetRank": 3,
        "fleetSize": 18,
        "cleanDayStreak": 5,
        "asOf": "2026-10-15T18:00:00Z",
    }
    other = str(uuid.uuid4())
    conn = psycopg2.connect(database)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(
                """
                INSERT INTO driver_coach_session
                    (session_id, truck_no, driver_id, route_cluster_id, started_at)
                VALUES (%s, 8, 'other_driver', 42, %s)
                """,
                (other, T0),
            )
            cur.execute("SELECT count(*) FROM idle_scorecard")
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()
    without = service.do_route(other, schema)
    assert "scorecard" not in without


def test_poll_omits_scorecard_when_the_table_is_missing(database, monkeypatch, caplog):
    schema = "driver_coach_noscore"
    _prepare(database, schema, scorecard=False)
    _patch(monkeypatch, database, schema)
    _route_stubs(monkeypatch)
    service._scorecard_table_missing_logged = False
    sid = str(uuid.uuid4())
    _session(database, schema, sid)
    with caplog.at_level(logging.WARNING):
        first = service.do_route(sid, schema)
        second = service.do_route(sid, schema)
    assert "scorecard" not in first and "scorecard" not in second
    notes = [rec for rec in caplog.records if "migration 009" in rec.message]
    assert len(notes) == 1


def test_parse_ingest_accepts_a_missing_truck():
    day, as_of, rows = idle_scorecard.parse_ingest({
        "day": "2026-10-15",
        "asOf": "2026-10-15T18:00:00Z",
        "drivers": [{k: v for k, v in DRIVER.items() if k != "truckNo"}],
    })
    assert day == "2026-10-15"
    assert as_of == T0
    assert rows[0]["truck_no"] is None

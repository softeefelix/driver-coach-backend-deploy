"""Daily break, Square offline periods, and the Hermes idle-exclusion read.

Disposable Postgres only. No fleet database and no live session writes.
"""
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import psycopg2
import pgserver
import pytest
from fastapi.testclient import TestClient

from app import breaks, holds, idle_status, nudge_suggestions, service, square_offline

T0 = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1]
MIGRATIONS = [
    (ROOT / "migrations" / "007_no_idle_breaks.sql").read_text(),
    (ROOT / "migrations" / "008_square_offline.sql").read_text(),
]
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


@pytest.fixture(scope="module")
def database():
    root = Path(__import__("tempfile").mkdtemp(prefix="idle-holds-", dir=os.environ.get("TMPDIR", "/tmp")))
    server = None
    try:
        server = pgserver.get_server(root / "data", cleanup_mode="delete")
        yield server.get_uri()
    finally:
        if server is not None:
            server.cleanup()
        __import__("shutil").rmtree(root, ignore_errors=True)


def _prepare(database, schema):
    conn = psycopg2.connect(database)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(SESSION)
            for sql in MIGRATIONS:
                cur.execute(sql)
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


def _patch_connect(monkeypatch, database, schema):
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


def _session(cur, sid, driver="amina_cole", truck=7, started=T0):
    cur.execute(
        """
        INSERT INTO driver_coach_session
            (session_id, truck_no, driver_id, route_cluster_id, started_at, plan_snapshot)
        VALUES (%s,%s,%s,%s,%s,%s::jsonb)
        """,
        (
            sid, truck, driver, 42, started,
            json.dumps([
                {"stop_order": 1, "stop_cluster_id": 10, "address": "1, First Street, Oakland", "exp_per_visit": 10},
                {"stop_order": 2, "stop_cluster_id": 20, "address": "2, Panama Street, Oakland", "exp_per_visit": 80},
                {"stop_order": 3, "stop_cluster_id": 30, "address": "3, School Court, Oakland", "exp_per_visit": 40},
            ]),
        ),
    )


def test_migration_refuses_public(database):
    conn = psycopg2.connect(database)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            with pytest.raises(psycopg2.Error):
                cur.execute(MIGRATIONS[1])
        conn.rollback()
    finally:
        conn.close()


def test_suggestions_prefer_the_drivers_own_unsold_stops_and_skip_other_trucks():
    master = [
        {"stop_order": 1, "stop_cluster_id": 10, "address": "1, First Street, Oakland", "exp_per_visit": 10},
        {"stop_order": 2, "stop_cluster_id": 20, "address": "2, Panama Street, Oakland", "exp_per_visit": 80},
        {"stop_order": 3, "stop_cluster_id": 30, "address": "3, School Court, Oakland", "exp_per_visit": 40},
        {"stop_order": 4, "stop_cluster_id": 40, "address": "4, Dropped Avenue, Oakland", "exp_per_visit": 90},
        {"stop_order": 5, "stop_cluster_id": 50, "address": "5, Later Street, Oakland", "exp_per_visit": 70},
    ]
    timed = [stop for stop in master if stop["stop_cluster_id"] != 40]
    picked = nudge_suggestions.choose_suggestions(
        master_stops=master,
        timed_stops=timed,
        served_orders={1},
        skipped_orders=set(),
        current_order=2,
        covered_cluster_ids={30},
    )
    assert [row["stopClusterId"] for row in picked] == [40, 50]
    assert picked[0] == {
        "stopOrder": 4,
        "stopClusterId": 40,
        "name": "Dropped Avenue",
        "why": "dropped",
        "expPerVisit": 90.0,
    }
    assert picked[1]["why"] == "ahead"
    assert all(row["stopClusterId"] != 30 for row in picked)
    assert holds.BREAK_DURATION_S == 1800
    assert holds.SQUARE_OFFLINE_MAX_S == 3600
    assert holds.BREAKS_PER_DRIVER_DAY == 1


def test_break_expires_without_a_client_and_square_periods_are_all_logged(database, schema, monkeypatch):
    _patch_connect(monkeypatch, database, schema)
    clock = {"now": T0}
    monkeypatch.setattr(service.no_idle, "utcnow", lambda: clock["now"])
    sid = str(uuid.uuid4())
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            _session(cur, sid)
        conn.commit()

    started = service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
    assert started["break"]["active"] is True
    assert started["break"]["allowedS"] == 1800
    assert started["break"]["endsAt"].endswith("Z")

    clock["now"] = T0 + timedelta(minutes=40)
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ended_at, end_reason, duration_s FROM shift_break WHERE driver_id=%s",
                ("amina_cole",),
            )
            stored_before = cur.fetchall()
    office = service.do_idle_exclusions(schema=schema)
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ended_at, end_reason, duration_s FROM shift_break WHERE driver_id=%s",
                ("amina_cole",),
            )
            stored_after = cur.fetchall()
    assert stored_before == stored_after
    assert stored_after == [(None, None, None)]
    assert office["day"] == "2026-10-15"
    assert office["timezone"] == "America/Los_Angeles"
    assert office["breakDurationS"] == 1800
    assert office["squareOfflineMaxS"] == 3600
    assert office["trucks"][0]["truckNo"] == 7
    assert office["trucks"][0]["driverId"] == "amina_cole"
    assert office["trucks"][0]["break"]["active"] is False
    assert "sessionId" not in office["trucks"][0]
    logged = office["history"][0]
    assert logged["kind"] == "break"
    assert logged["endReason"] == "expired"
    assert logged["expired"] is True
    assert logged["durationS"] == 1800
    assert logged["endedAt"] == "2026-10-15T18:30:00Z"
    _assert_no_session_key(office)

    with pytest.raises(breaks.BreakError) as refused:
        service.do_start_break(service.BreakStartRequest(session_id=sid, type="lunch"))
    assert refused.value.detail["error"] == "over_limit"

    clock["now"] = T0
    offline = service.do_start_square_offline(service.SquareOfflineRequest(session_id=sid))
    assert offline["squareOffline"]["active"] is True
    assert offline["squareOffline"]["allowedS"] == 3600
    assert offline["squareOffline"]["todayCount"] == 1
    clock["now"] = T0 + timedelta(minutes=5)
    ended = service.do_end_square_offline(service.SquareOfflineRequest(session_id=sid))
    assert ended["ended"]["endReason"] == "driver"
    assert ended["ended"]["expired"] is False
    clock["now"] = T0 + timedelta(minutes=6)
    again = service.do_start_square_offline(service.SquareOfflineRequest(session_id=sid))
    assert again["squareOffline"]["todayCount"] == 2
    with pytest.raises(square_offline.SquareOfflineError) as active:
        service.do_start_square_offline(service.SquareOfflineRequest(session_id=sid))
    assert active.value.status == 409

    clock["now"] = T0 + timedelta(minutes=70)
    office = service.do_idle_exclusions(schema=schema)
    square_rows = [row for row in office["history"] if row["kind"] == "square_offline"]
    assert len(square_rows) == 2
    assert {row["endReason"] for row in square_rows} == {"driver", "expired"}
    expired = next(row for row in square_rows if row["expired"])
    assert expired["durationS"] == 3600
    assert expired["truckNo"] == 7
    assert office["trucks"][0]["squareOffline"]["active"] is False

    client = TestClient(service.app)
    missing = client.get("/driver-coach/v1/idle-exclusions", params={"day": "not-a-day"})
    assert missing.status_code == 400
    bare = client.get("/driver-coach/v1/idle-exclusions")
    assert bare.status_code == 200
    assert "breakDurationS" in bare.json()
    _assert_no_session_key(bare.json())
    _assert_no_session_key(office)


def _assert_no_session_key(payload) -> None:
    """A session id authorizes driver writes, so this office read must not carry one."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key not in {"sessionId", "session_id"}
            _assert_no_session_key(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_session_key(item)


def test_end_after_the_cap_returns_the_expired_hold(database, schema, monkeypatch):
    _patch_connect(monkeypatch, database, schema)
    clock = {"now": T0}
    monkeypatch.setattr(service.no_idle, "utcnow", lambda: clock["now"])
    sid = str(uuid.uuid4())
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            _session(cur, sid, driver="late_ender")
        conn.commit()
    service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
    clock["now"] = T0 + timedelta(minutes=40)
    ended = service.do_end_break(service.BreakEndRequest(session_id=sid))
    assert ended["ended"]["endReason"] == "expired"
    assert ended["ended"]["expired"] is True
    assert ended["ended"]["durationS"] == 1800
    again = service.do_end_break(service.BreakEndRequest(session_id=sid))
    assert again["ended"]["endReason"] == "expired"
    assert again["break"]["active"] is False


def test_break_requires_a_driver(database, schema, monkeypatch):
    _patch_connect(monkeypatch, database, schema)
    sid = str(uuid.uuid4())
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            _session(cur, sid, driver=None)
        conn.commit()
    with pytest.raises(breaks.BreakError) as missing:
        service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
    assert missing.value.status == 409
    assert missing.value.detail["error"] == "no_driver"


def test_concurrent_starts_allow_one_break(database, schema, monkeypatch):
    _patch_connect(monkeypatch, database, schema)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            _session(cur, first, driver="racer", truck=11)
            _session(cur, second, driver="racer", truck=12)
        conn.commit()
    barrier = threading.Barrier(2)
    outcome = []
    guard = threading.Lock()

    def attempt(sid):
        barrier.wait(timeout=5)
        try:
            service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
            result = "ok"
        except breaks.BreakError as exc:
            result = exc.detail["error"]
        with guard:
            outcome.append(result)

    threads = [threading.Thread(target=attempt, args=(sid,)) for sid in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert outcome.count("ok") == 1
    assert outcome.count("break_active") + outcome.count("over_limit") == 1
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shift_break WHERE driver_id=%s", ("racer",))
            assert cur.fetchone()[0] == 1


def test_break_allowance_resets_at_pacific_midnight(database, schema, monkeypatch):
    _patch_connect(monkeypatch, database, schema)
    clock = {"now": datetime(2026, 11, 10, 23, 59, tzinfo=holds.PACIFIC)}
    monkeypatch.setattr(service.no_idle, "utcnow", lambda: clock["now"])
    sid = str(uuid.uuid4())
    with _connect(database, schema) as conn:
        with conn.cursor() as cur:
            _session(cur, sid, driver="midnight", started=clock["now"])
        conn.commit()
    service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
    service.do_end_break(service.BreakEndRequest(session_id=sid))
    with pytest.raises(breaks.BreakError) as same_night:
        service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
    assert same_night.value.detail["error"] == "over_limit"
    clock["now"] = datetime(2026, 11, 11, 0, 1, tzinfo=holds.PACIFIC)
    nxt = service.do_start_break(service.BreakStartRequest(session_id=sid, type="break"))
    assert nxt["break"]["active"] is True
    assert nxt["break"]["day"] == "2026-11-11"
    assert nxt["break"]["used"] == 1


def test_idle_status_empty_day_shape():
    body = idle_status.empty_payload("2026-11-10", T0)
    assert body["trucks"] == [] and body["history"] == []
    assert body["squareOfflineMaxS"] == holds.SQUARE_OFFLINE_MAX_S

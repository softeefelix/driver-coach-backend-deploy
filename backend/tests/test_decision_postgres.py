"""Real disposable PostgreSQL; never reads DB_URL or contacts fleet databases."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import psycopg2
import pytest
import pgserver
from app import decision_store, routes
from test_school_slack_live import live, stop


@pytest.fixture(scope='module')
def database():
    root = Path(tempfile.mkdtemp(prefix='s1-postgres-', dir=os.environ['TMPDIR']))
    server = None
    subprocess.run(['df', '-h', str(root)], check=True)
    try:
        server = pgserver.get_server(root / 'data', cleanup_mode='delete')
        with psycopg2.connect(server.get_uri()) as conn:
            with conn.cursor() as cur:
                for schema in ('driver_coach', 'driver_coach_test'):
                    cur.execute(f'CREATE SCHEMA {schema}')
                    cur.execute(f'SET search_path TO {schema}')
                    cur.execute("CREATE TABLE driver_coach_session (session_id uuid PRIMARY KEY, truck_no int DEFAULT 7, coached bool DEFAULT false, route_cluster_id int DEFAULT 42, driver_id text DEFAULT 'test', plan_snapshot jsonb DEFAULT '[]', served_stop_orders jsonb DEFAULT '[]', skipped_stop_orders jsonb DEFAULT '[]')")
                    cur.execute((Path(__file__).parents[1] / 'migrations/006_school_slack_decisions.sql').read_text())
            conn.commit()
        yield server.get_uri()
    finally:
        if server is not None:
            server.cleanup()
        shutil.rmtree(root)
        subprocess.run(['df', '-h', os.environ['TMPDIR']], check=True)
        print('Disposable PostgreSQL stopped and owned data directory removed')


@pytest.mark.parametrize('schema', ['driver_coach', 'driver_coach_test'])
def test_live_persistence_idempotent_migration_append_only(database, schema, live):
    migration = (Path(__file__).parents[1] / 'migrations/006_school_slack_decisions.sql').read_text()
    sid = str(uuid.uuid4())
    with psycopg2.connect(database) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO {schema}')
            cur.execute('INSERT INTO driver_coach_session (session_id) VALUES (%s)', (sid,))
            cur.execute(migration)
            cur.execute(migration)
        conn.commit()
    # New connections emulate worker restart: no shared in-process state.
    for now, expected in [(840, 2), (918, 1), (850, 1)]:
        with psycopg2.connect(database) as conn:
            with conn.cursor() as cur:
                result = routes.resolve_live_route(cur, 7, route_cluster_id=42, dow='Monday',
                    frozen_plan=[stop(1, 'school', '3:30 PM'), stop(2)], now_minutes=now,
                    session_id=sid, schema=schema)
                assert result['next_stop']['stop_order'] == expected
                audit_id = result['next_stop']['decision_audit_id']
                cur.execute(f'SELECT inputs, state FROM {schema}.next_stop_decision WHERE decision_id=%s', (audit_id,))
                inputs, state = cur.fetchone()
                assert inputs['choice'] == f'stop:{expected}'
                assert state['choice'] == f'stop:{expected}'
            conn.commit()
    with psycopg2.connect(database) as conn:
        with conn.cursor() as cur:
            state = decision_store.load(cur, sid, schema)
            assert state['binding'] == 'stop:1'
            decision_store.append(cur, sid, schema, dict(audit={'test': 'rollback'}, state={}))
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM {schema}.next_stop_decision WHERE session_id=%s', (sid,))
            assert cur.fetchone()[0] == 3
        for command in ['UPDATE {s}.next_stop_decision SET state=\'{{}}\'',
                        'DELETE FROM {s}.next_stop_decision', 'TRUNCATE {s}.next_stop_decision']:
            with pytest.raises(psycopg2.errors.RaiseException):
                with conn.cursor() as cur:
                    cur.execute(command.format(s=schema))
            conn.rollback()
    with psycopg2.connect(database) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.next_stop_decision')")
            assert cur.fetchone()[0] is None


@pytest.mark.parametrize('schema', ['driver_coach', 'driver_coach_test'])
def test_service_poll_outcomes_reconfirm(database, schema, live, monkeypatch):
    import json
    from contextlib import contextmanager
    from app import service, school_slack
    sid = str(uuid.uuid4())
    plan = [stop(1, 'school', '11:59 PM'), stop(2)]
    @contextmanager
    def connection(schema, autocommit=False):
        conn = psycopg2.connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO {schema}')
            yield conn
        finally:
            conn.close()
    monkeypatch.setattr(service, 'connect', connection)
    monkeypatch.setattr(service.jobber, 'name_from_driver_id', lambda *a: '')
    monkeypatch.setattr(service.events_mod, 'get_today_events', lambda *a: [])
    monkeypatch.setattr(service.scorecard, 'live_ticker', lambda *a: None)
    monkeypatch.setattr(service, 'refuel_reminder_for_route_poll', lambda *a: None)
    real_resolve = routes.resolve_live_route
    monkeypatch.setattr(routes, 'resolve_live_route', lambda *a, **kw: real_resolve(*a, now_minutes=840, **kw))
    with connection(schema) as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO driver_coach_session (session_id, plan_snapshot) VALUES (%s,%s::jsonb)', (sid, json.dumps(plan)))
        conn.commit()
    first = service.do_route(sid, schema)['route']['nextStop']
    assert first['stopOrder'] == 2 and first['decisionAuditId']
    with pytest.raises(ValueError):
        service.do_stop_outcome(service.StopOutcomeRequest(session_id=sid, stop_order=1, outcome='done'), schema)
    service.do_stop_outcome(service.StopOutcomeRequest(session_id=sid, stop_order=2, outcome='skip'), schema)
    assert service.do_route(sid, schema)['route']['nextStop']['stopOrder'] == 1
    with connection(schema) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT skipped_stop_orders, plan_snapshot FROM driver_coach_session WHERE session_id=%s', (sid,))
            skipped, stored_plan = cur.fetchone()
            assert skipped == [2] and stored_plan == plan
    # Reconfirm the exact same route: old binding must not survive the reset.
    monkeypatch.setattr(routes, 'ordered_stops_for_route', lambda *a: plan)
    payload = service.do_confirm_assignment(service.ConfirmAssignmentRequest(session_id=sid, route_cluster_id=42), schema)
    assert payload['route']['nextStop']['stopOrder'] == 2
    service.do_stop_outcome(service.StopOutcomeRequest(session_id=sid, stop_order=2, outcome='done'), schema)
    assert service.do_route(sid, schema)['route']['nextStop']['stopOrder'] == 1


def test_session_lock_serializes_poll_and_outcome(database):
    schema = 'driver_coach'
    with psycopg2.connect(database) as first, psycopg2.connect(database) as second:
        with first.cursor() as a, second.cursor() as b:
            sid = str(uuid.uuid4())
            a.execute(f'INSERT INTO {schema}.driver_coach_session (session_id) VALUES (%s)', (sid,))
            first.commit()
            decision_store.load(a, sid, schema)
            b.execute("SET lock_timeout = '50ms'")
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                decision_store.load(b, sid, schema)
            second.rollback()
            first.commit()
            assert decision_store.load(b, sid, schema) == {}

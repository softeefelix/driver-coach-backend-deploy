"""Durable append-only decisions and restart-safe chooser state.

The caller owns the transaction/commit. Session lock serializes polls with outcomes.
All identifiers are schema-qualified; no search_path or public-schema dependency.
"""
import json
import uuid
import math
from psycopg2 import sql
from .db import assert_safe_schema


def table(schema, name):
    return sql.Identifier(assert_safe_schema(schema), name)


def load(cur, session_id, schema):
    cur.execute(sql.SQL('SELECT session_id FROM {} WHERE session_id=%s FOR UPDATE').format(
        table(schema, 'driver_coach_session')), (session_id,))
    if cur.fetchone() is None:
        raise ValueError('unknown chooser session')
    cur.execute(sql.SQL('SELECT state FROM {} WHERE session_id=%s ORDER BY sequence DESC LIMIT 1').format(
        table(schema, 'next_stop_decision')), (session_id,))
    row = cur.fetchone()
    return row[0] if row else {}


def append(cur, session_id, schema, advice):
    def safe(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe(item) for item in value]
        return value
    audit_id = str(uuid.uuid4())
    cur.execute(sql.SQL('INSERT INTO {} (decision_id, session_id, inputs, state) VALUES (%s,%s,%s::jsonb,%s::jsonb)').format(
        table(schema, 'next_stop_decision')),
        (audit_id, session_id, json.dumps(safe(advice['audit']), default=str, allow_nan=False),
         json.dumps(advice['state'], allow_nan=False)))
    return audit_id

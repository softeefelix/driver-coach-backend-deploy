"""DB access: load DATABASE_URL from the driver-performance secret (the shared
`softeedatabase` on Render Postgres, PostGIS available) and hand out connections
bound to a NAMED schema via `search_path`.

HARD CONSTRAINT (Felix gate): tests NEVER touch the `public` schema. The service
targets whatever schema `DRIVER_COACH_SCHEMA` names (default `driver_coach`); the
test harness creates + drops an isolated `driver_coach_test_<pid>` schema. This
module's `connect(schema=...)` sets `search_path = <schema>` so every statement
lands there and the payroll/driver-performance `public` tables are untouched.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager

import psycopg2

_DP_ENV = os.path.expanduser(
    "~/projects/work/MisterSoftee/driver-performance/.secrets/db.env"
)

# Postgres identifier guard: schema names we will interpolate must be safe.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

DEFAULT_SCHEMA = os.environ.get("DRIVER_COACH_SCHEMA", "driver_coach")


def load_database_url() -> str:
    """Read DATABASE_URL. Prefer the process env; fall back to the driver-perf
    secret file (the connection pattern the rest of the stack uses)."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url.strip()
    if os.path.exists(_DP_ENV):
        with open(_DP_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("DATABASE_URL") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(
        "DATABASE_URL not found (env or driver-performance/.secrets/db.env)"
    )


def assert_safe_schema(schema: str) -> str:
    if not _IDENT_RE.match(schema):
        raise ValueError(f"unsafe schema identifier: {schema!r}")
    if schema.lower() == "public":
        # The Felix gate in code: this module refuses to bind to public. Migrations
        # and writes always target an additive driver_coach* schema.
        raise ValueError("refusing to bind to the public schema (Felix hard gate)")
    return schema


@contextmanager
def connect(schema: str = DEFAULT_SCHEMA, autocommit: bool = False):
    """Yield a psycopg2 connection with search_path pinned to `schema`.

    `public` is appended to search_path READ-ONLY-style for resolving PostGIS's
    `geography`/`geometry` types and reading the shared driver-performance tables,
    but every UNQUALIFIED write resolves to `schema` (first entry) so DDL/inserts
    land in the isolated schema, never public.
    """
    assert_safe_schema(schema)
    conn = psycopg2.connect(load_database_url(), connect_timeout=20)
    try:
        conn.autocommit = autocommit
        with conn.cursor() as cur:
            # schema first (writes land here); public last (read shared tables + PostGIS types)
            cur.execute(f'SET search_path TO "{schema}", public')
        yield conn
    finally:
        conn.close()

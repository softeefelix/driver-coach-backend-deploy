"""Apply the idempotent Driver Coach DDL to a NAMED schema.

HARD CONSTRAINT (Felix gate): NEVER the public schema. This runner refuses to
migrate `public` (db.assert_safe_schema), creates the target schema if needed, and
applies migrations/*.sql inside it. Re-runnable — the DDL is CREATE ... IF NOT
EXISTS, so applying twice is a no-op.

Usage (build/test only):
    python -m app.migrate --schema driver_coach_test_1234 --create
    python -m app.migrate --schema driver_coach_test_1234 --drop     # teardown

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import argparse
import glob
import os

from .db import assert_safe_schema, connect

_MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__)).replace(
    os.path.join("app"), "migrations"
)
# app/migrate.py lives in backend/app; migrations live in backend/migrations
_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def migration_files() -> list:
    return sorted(glob.glob(os.path.join(_MIGRATIONS_DIR, "*.sql")))


def ensure_schema(schema: str) -> None:
    assert_safe_schema(schema)
    # autocommit for DDL; connect() binds search_path to the schema after it exists,
    # so create the schema on a public-search_path connection first.
    import psycopg2

    from .db import load_database_url

    conn = psycopg2.connect(load_database_url(), connect_timeout=20)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    finally:
        conn.close()


def apply_migrations(schema: str) -> list:
    """Create the schema (if needed) and apply every migration file. Returns the
    list of applied files. Idempotent."""
    ensure_schema(schema)
    applied = []
    with connect(schema=schema, autocommit=True) as conn:
        with conn.cursor() as cur:
            for path in migration_files():
                with open(path) as fh:
                    cur.execute(fh.read())
                applied.append(os.path.basename(path))
    return applied


def drop_schema(schema: str) -> None:
    """Drop an isolated schema (test teardown). Refuses public."""
    assert_safe_schema(schema)
    import psycopg2

    from .db import load_database_url

    conn = psycopg2.connect(load_database_url(), connect_timeout=20)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        conn.close()


def _main() -> None:
    ap = argparse.ArgumentParser(description="Driver Coach migrations (named schema only)")
    ap.add_argument("--schema", required=True, help="target schema (never 'public')")
    ap.add_argument("--create", action="store_true", help="apply migrations")
    ap.add_argument("--drop", action="store_true", help="drop the schema (teardown)")
    args = ap.parse_args()
    if args.drop:
        drop_schema(args.schema)
        print(f"dropped schema {args.schema}")
        return
    applied = apply_migrations(args.schema)
    print(f"applied {len(applied)} migration(s) to schema {args.schema}: {applied}")


if __name__ == "__main__":
    _main()

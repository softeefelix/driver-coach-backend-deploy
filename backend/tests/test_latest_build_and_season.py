"""Latest-build filter + season fallback, against the read-only Master Route DB.

Skips unless MASTER_ROUTE_DATABASE_URL is set. Read-only: nothing is written.
"""
import os

import pytest

from app import assignment, routes

pytestmark = pytest.mark.skipif(
    not os.environ.get("MASTER_ROUTE_DATABASE_URL"), reason="needs Master Route DB"
)

QUAIL = 2239       # CastroValley-Quail-Mon: only Aug 13 rows, not in the latest build
SUNNYVALE = 1735   # Sunnyvale-NimitzElementary: in the latest build
PARADISE = 1805    # CastroValley-ParadiseKnoll-Mon: Monday master tagged '' only


def test_latest_build_sql_shape():
    assert routes.latest_build_sql("rts").startswith("rts.generated_at >= (SELECT max")
    assert routes.latest_build_sql().startswith("generated_at >= ")


@pytest.fixture
def conn():
    c = assignment._master_connection()
    yield c
    c.close()


def test_stale_route_not_served(conn):
    ids = {r[0] for r in assignment._all_candidate_rows(conn, "Monday")}
    assert QUAIL not in ids
    assert SUNNYVALE in ids
    with conn.cursor() as cur:
        assert routes.ordered_stops_for_route(cur, QUAIL, "Monday") == []
        assert len(routes.ordered_stops_for_route(cur, SUNNYVALE, "Monday")) >= 5


def test_sunnyvale_resolves_live_for_school(conn):
    live = [r for r in assignment._live_rows(conn, "Monday", "school")
            if assignment._name_for_city(r[1], "Sunnyvale")]
    assert [r[0] for r in live] == [SUNNYVALE]


def test_blank_season_master_is_fallback(conn):
    ids = [r[0] for r in assignment._live_rows(conn, "Monday", "school")]
    assert PARADISE in ids
    assert len(ids) == len(set(ids))
    assert assignment._live_stop_counts(conn, "Monday", "school").get(PARADISE, 0) > 0

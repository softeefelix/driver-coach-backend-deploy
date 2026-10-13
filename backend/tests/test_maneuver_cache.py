"""One stored leg costs one Mapbox call, and the cab only hears what is still ahead.

The cache key is (route_cluster_id, dow, next stop order, the first stored
trace point of the leg rounded to 5 decimals). Locations stay server-side.
A failed fetch is remembered for at most 30 seconds, never invented around.
"""
import pytest

from app import maneuvers as maneuvers_mod
from app.maneuvers import maneuvers_along_line


_CLOCK = {"now": 1_000_000.0}


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    _CLOCK["now"] = 1_000_000.0
    monkeypatch.setattr(maneuvers_mod, "_now", lambda: _CLOCK["now"])
    maneuvers_mod._leg_cache.clear()
    yield
    maneuvers_mod._leg_cache.clear()


def _advance(seconds):
    _CLOCK["now"] += seconds


def _line(n=80, *, lat0=37.5, lng0=-122.0):
    return [[lat0 + i * 0.0001, lng0 - i * 0.0001] for i in range(n)]


def _mapbox_body(maneuvers):
    """maneuvers: (street, distance_m, modifier, lat, lng)."""
    steps = []
    for street, distance_m, modifier, lat, lng in maneuvers:
        steps.append({
            "name": street,
            "distance": distance_m,
            "maneuver": {
                "instruction": f"Turn onto {street}",
                "modifier": modifier,
                "location": [lng, lat],
            },
        })
    return {"routes": [{"legs": [{"steps": steps}]}]}


_NAMED = (
    ("Mowry Avenue", 400, "right", 37.5010, -122.0010),
    ("Paseo Padre Parkway", 500, "left", 37.5040, -122.0040),
    ("Decoto Road", 300, "right", 37.5070, -122.0070),
    ("Alvarado Boulevard", 200, "straight", 37.5090, -122.0090),
)


def _fetch_factory(body=None, *, fail=False):
    seen = {"calls": 0, "tokens": []}

    def fetch(coords, token, timeout):
        seen["calls"] += 1
        seen["tokens"].append(token)
        seen["coords"] = coords
        if fail:
            raise RuntimeError("mapbox down")
        return body if body is not None else _mapbox_body(_NAMED)

    fetch.seen = seen
    return fetch


def _poll(line=None, *, truck=None, route_cluster_id=1778, dow="mon",
          next_stop_order=4, fetch=None, token="pk.test-secret"):
    return maneuvers_along_line(
        line if line is not None else _line(),
        token=token,
        fetch=fetch,
        truck=truck,
        route_cluster_id=route_cluster_id,
        dow=dow,
        next_stop_order=next_stop_order,
    )


def test_n_polls_of_one_leg_make_exactly_one_mapbox_call():
    fetch = _fetch_factory()
    first = _poll(fetch=fetch)
    for _ in range(19):
        assert _poll(fetch=fetch) == first
    assert fetch.seen["calls"] == 1
    assert first[0] == {"street": "Mowry Avenue", "distance_m": 400, "modifier": "right"}
    assert len(first) == 3
    assert all(set(step) == {"street", "distance_m", "modifier"} for step in first)


def test_truck_passing_a_maneuver_drops_it_without_a_new_call():
    fetch = _fetch_factory()
    # Truck is still before the first maneuver.
    before = _poll(truck=(37.5002, -122.0002), fetch=fetch)
    assert [step["street"] for step in before] == [
        "Mowry Avenue", "Paseo Padre Parkway", "Decoto Road",
    ]
    # Projected past Mowry Avenue, still short of Paseo Padre. No refetch.
    after = _poll(truck=(37.5020, -122.0020), fetch=fetch)
    assert fetch.seen["calls"] == 1
    assert [step["street"] for step in after] == [
        "Paseo Padre Parkway", "Decoto Road", "Alvarado Boulevard",
    ]


def test_a_new_next_stop_refetches():
    fetch = _fetch_factory()
    _poll(next_stop_order=4, fetch=fetch)
    _poll(next_stop_order=4, fetch=fetch)
    _poll(next_stop_order=5, fetch=fetch)
    assert fetch.seen["calls"] == 2


def test_a_different_route_or_weekday_never_shares_an_entry():
    fetch = _fetch_factory()
    _poll(route_cluster_id=1778, dow="mon", fetch=fetch)
    _poll(route_cluster_id=1779, dow="mon", fetch=fetch)
    _poll(route_cluster_id=1778, dow="tue", fetch=fetch)
    assert fetch.seen["calls"] == 3


def test_a_failure_is_not_cached_beyond_30_seconds():
    fetch = _fetch_factory(fail=True)
    assert _poll(fetch=fetch) == []
    _advance(30)
    assert _poll(fetch=fetch) == []
    assert fetch.seen["calls"] == 1
    _advance(1)
    assert _poll(fetch=fetch) == []
    assert fetch.seen["calls"] == 2


def test_a_successful_leg_is_reused_for_at_least_15_minutes():
    fetch = _fetch_factory()
    _poll(fetch=fetch)
    _advance(15 * 60)
    _poll(fetch=fetch)
    assert fetch.seen["calls"] == 1
    _advance(1)
    _poll(fetch=fetch)
    assert fetch.seen["calls"] == 2


def test_the_cache_stays_bounded():
    fetch = _fetch_factory()
    for order in range(600):
        _poll(next_stop_order=order, fetch=fetch)
    assert fetch.seen["calls"] == 600
    assert len(maneuvers_mod._leg_cache) <= 500
    # An early stop was evicted, so asking for it again is a new call.
    before = fetch.seen["calls"]
    _poll(next_stop_order=0, fetch=fetch)
    assert fetch.seen["calls"] == before + 1


def test_locations_token_and_key_never_leave_the_process(caplog):
    import logging
    fetch = _fetch_factory()
    with caplog.at_level(logging.DEBUG):
        steps = _poll(fetch=fetch, token="pk.test-secret")
    blob = repr(steps) + caplog.text
    assert "pk.test-secret" not in blob
    assert "37.501" not in blob
    assert "-122.001" not in blob
    assert "access_token" not in blob

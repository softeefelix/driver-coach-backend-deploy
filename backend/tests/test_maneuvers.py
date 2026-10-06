"""The cab's maneuvers follow the stored line and name a street."""
import pytest

from app.maneuvers import (
    is_heading_only,
    maneuvers_along_line,
    named_maneuvers,
    sample_trace_waypoints,
)


def _line(n=80):
    return [[37.5 + i * 0.0001, -122.0 - i * 0.0001] for i in range(n)]


def test_samples_keep_both_ends_and_step_about_every_25_points():
    line = _line(80)
    samples = sample_trace_waypoints(line)
    assert samples[0] == line[0]
    assert samples[-1] == line[-1]
    assert samples[1] == line[25]
    assert samples[2] == line[50]
    assert len(samples) == 5


def test_no_stored_line_produces_no_maneuvers():
    def boom(*_args, **_kwargs):
        raise AssertionError("a missing line must not call Mapbox")

    assert maneuvers_along_line(None, token="pk.test", fetch=boom) == []
    assert maneuvers_along_line([], token="pk.test", fetch=boom) == []
    assert maneuvers_along_line([[37.5, -122.0]], token="pk.test", fetch=boom) == []
    assert maneuvers_along_line(_line(), token="", fetch=boom) == []


def test_request_uses_the_sampled_line_not_the_two_endpoints():
    seen = {}

    def fetch(coords, token, timeout):
        seen["coords"] = coords
        seen["token"] = token
        return {"routes": [{"legs": []}]}

    maneuvers_along_line(_line(80), token="pk.test", fetch=fetch)
    pairs = seen["coords"].split(";")
    assert len(pairs) == 5
    assert pairs[0] == "-122.0,37.5"
    assert pairs[1] != pairs[-1]


def test_blank_street_and_heading_only_are_dropped():
    steps = [
        {"street": "", "instruction": "Head north", "distance_m": 40, "modifier": "straight"},
        {"street": " ", "instruction": "Drive southeast", "distance_m": 80, "modifier": "straight"},
        {"street": "Fremont Boulevard", "instruction": "Head north", "distance_m": 12, "modifier": "straight"},
        {"street": "Fremont Boulevard", "instruction": "Continue", "distance_m": 30, "modifier": ""},
        {"street": "Mowry Avenue", "instruction": "Turn right onto Mowry Avenue", "distance_m": 640, "modifier": "right"},
        {"street": "Paseo Padre Parkway", "instruction": "Turn left onto Paseo Padre Parkway", "distance_m": 900, "modifier": "left"},
        {"street": "Decoto Road", "instruction": "Turn right onto Decoto Road", "distance_m": 400, "modifier": "right"},
        {"street": "Alvarado Boulevard", "instruction": "Continue on Alvarado Boulevard", "distance_m": 200, "modifier": "straight"},
    ]
    kept = named_maneuvers(steps)
    assert [step["street"] for step in kept] == [
        "Mowry Avenue", "Paseo Padre Parkway", "Decoto Road",
    ]
    assert kept[0] == {"street": "Mowry Avenue", "distance_m": 640, "modifier": "right"}
    assert "instruction" not in kept[0]
    assert "lat" not in kept[0] and "lng" not in kept[0]


def test_continue_on_a_named_street_is_kept():
    assert is_heading_only("Continue") is True
    assert is_heading_only("Head north") is True
    assert is_heading_only("Drive southeast.") is True
    assert is_heading_only("Continue on Mowry Avenue") is False
    assert is_heading_only("Turn right onto Fremont Boulevard") is False


def test_at_most_three_and_a_failed_fetch_is_empty():
    def fetch(*_args, **_kwargs):
        return None

    assert maneuvers_along_line(_line(), token="pk.test", fetch=fetch) == []

"""Today's paid/public EVENTS for the signed-in driver, live from Jobber.

The map-centric driving screen leaves the right panel EMPTY unless the driver has
booked gigs today; this module fills it. An "event" is a Jobber VISIT whose job is
`jobType == "ONE_OFF"` (the booked parties / corporate / school gigs) — the regular
cruising route is RECURRING and never appears in the visits query, so ONE_OFF is a
clean discriminator (Felix locked 2026-09-17; live-verified: today's 10 one-off
visits are all the real events). SAME-DAY only, filtered to the SIGNED-IN DRIVER.

Disguise + simplicity: the returned rows are an operational fact of the truck's day,
identical for every driver — {title, startTime, address, status}. NO $, NO grade, NO
coached content. `events` rides on the /route + signin payload in BOTH modes and the
disguise test asserts the two modes are byte-identical (see payload.assert_disguise_safe).

SOURCE — NO JOBBER CLIENT HERE (Felix 2026-10-20). The backend reads the visits that
Hermes publishes to `driver_coach_test.events_today` (one row per Pacific day, events
only, never tokens; dc_test_host has SELECT on that table only). Hermes's single Jobber
reader rewrites events_today_cache.json and that row hourly 07-10 and every 15 min
11-21 PT, with a version stamp and a diff vs the previous publish (reassignments show
up as a changed assignedUsers).

DEGRADATION — "EVENTS UNKNOWN", NEVER SILENT (Felix 2026-10-20, supersedes the
2026-09-17 "[] on failure" rule). `get_today_events` still returns a list and never
raises, but a failure (no row, failed pull, stale row, DB error) is now distinguishable:
`get_today_events_with_status` returns status "unknown" and the payload carries
`eventsStatus: "unknown"`, so the cab shows "Events unknown" instead of an empty panel.
Success is cached 60 s per (driver, day); failure is never cached.

ONE_OFF jobs are events: they ride `events` and take precedence over the remaining
stops. RECURRING jobs (Bingo Night, weekly school stops) are returned separately by
`scheduled_visits_for_driver` and appear as normal stops at their booked time.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import datetime
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional
from zoneinfo import ZoneInfo

from . import jobber
from .config import mapbox_token
from .db import load_database_url
from .payload import fmt_clock_ampm

PACIFIC = ZoneInfo("America/Los_Angeles")

# DB connect budget is inside the same overall spirit — a slow cold Render Postgres
# must not hang sign-in. Kept short; a miss just means [] (panel hidden).
DB_CONNECT_TIMEOUT_S = 4

# Cache 60 s per (driver_lower, day_iso). The visits now come from one Postgres row
# (driver_coach_test.events_today) that Hermes republishes every 15 min, so a short
# cache costs one indexed read a minute and a reassignment reaches the cab within ~1 min
# of the publish.
_CACHE_TTL_S = 60

# EVENTS FEED (Felix 2026-10-20): the backend no longer calls Jobber. Hermes's single
# Jobber reader (idle_live_monitor.load_event_windows via fleet_check's token path,
# cron "Driver Coach Events Feed") rewrites events_today_cache.json and publishes the
# same visits to this table. dc_test_host has SELECT on this table only — no token.
EVENTS_TABLE = os.environ.get("DRIVER_COACH_EVENTS_TABLE", "driver_coach_test.events_today")
# Publishes run hourly 07-10 and every 15 min 11-21 PT. Older than this = unknown.
EVENTS_MAX_AGE_S = int(os.environ.get("DRIVER_COACH_EVENTS_MAX_AGE_S", str(75 * 60)))
# Last feed read per day: {"status": "ok"|"unknown", "version": int|None, "reason": str}
_feed_state: dict[str, dict] = {}
_cache: dict[tuple[str, str, bool], tuple[float, list[dict]]] = {}
# Raw visit nodes, shared by the events panel and sign-in assignment. Success only.
_visits_cache: dict[str, tuple[float, list]] = {}

# Jobber supplies only a street + city. Geocoding happens before these rows reach
# advise_plan so booked events can become real map pins; the total keeps sign-in/poll
# responsive even when several addresses are not resolvable.
MAPBOX_GEOCODE_URL = "https://api.mapbox.com/search/geocode/v6/forward"
GEOCODE_TIMEOUT_S = 2.0
GEOCODE_TOTAL_TIMEOUT_S = 4.0

# ── Name matching (reuse the existing Jobber<->Square resolution seam) ─────────
def _norm(name: Optional[str]) -> str:
    """Normalize a name for comparison: trimmed, collapsed whitespace, lowercase."""
    return " ".join((name or "").split()).lower()


def _same_driver(want: str, jobber_full: str) -> bool:
    """Jobber 'Emery N' is the signed-in 'Emery' when that first name is unique.

    Exact and mapped Square names still win. A shared first name does not match,
    so two Emilys cannot steal each other's sheet.
    """
    if _norm(jobber_full) == _norm(want):
        return True
    mapped = _norm(jobber.resolve_square_name(jobber_full))
    if mapped == _norm(want):
        return True
    want_first = _norm(want).split(" ")[0] if _norm(want) else ""
    got_first = mapped.split(" ")[0] if mapped else ""
    if not want_first or want_first != got_first or " " in _norm(want):
        return False
    try:
        mapping = jobber._load_map(jobber._map_path())
    except Exception:
        return False
    same = {
        _norm(str(value)) for key, value in mapping.items()
        if value and _norm(str(key)).split(" ")[0] == want_first
    }
    return len(same) == 1


def _driver_matches(driver_canonical: str, assigned_full_names: list[str]) -> bool:
    """True iff the signed-in driver is one of a visit's assigned users."""
    return any(_same_driver(driver_canonical, full) for full in assigned_full_names)


# ── Pure parse/filter/format (no I/O — unit-testable) ─────────────────────────
def filter_and_format(nodes: list, driver_canonical: str) -> list[dict]:
    """Fold raw Jobber visit nodes -> the client event rows for ONE driver.

    Keeps ONLY `job.jobType == "ONE_OFF"` visits assigned to `driver_canonical`,
    then maps each to {title, startTime, address, status}:
      - startTime : startAt (UTC ISO) -> 12-hour "H:MM AM/PM" Pacific
      - address   : "street, city" (either part omitted if missing; None if neither)
    Sorted by start time. Pure + total: bad/missing fields are skipped, never raised.
    """
    rows: list[dict] = []
    for v in nodes or []:
        if not isinstance(v, dict):
            continue
        if (v.get("job") or {}).get("jobType") != "ONE_OFF":
            continue
        assigned = [
            (n.get("name") or {}).get("full")
            for n in ((v.get("assignedUsers") or {}).get("nodes") or [])
            if isinstance(n, dict)
        ]
        assigned = [a for a in assigned if a]
        if not _driver_matches(driver_canonical, assigned):
            continue
        addr = (v.get("property") or {}).get("address") or {}
        address = ", ".join(p for p in (addr.get("street"), addr.get("city")) if p) or None
        extra = {'id': v.get('id'), 'endTime': _fmt_pt_time(v.get('endAt'))}
        rows.append(
            {
                **extra,
                "title": v.get("title") or "Event",
                "startTime": _fmt_pt_time(v.get("startAt")),
                "address": address,
                "status": v.get("visitStatus"),
                "_sort": v.get("startAt") or "",
            }
        )
    rows.sort(key=lambda r: r["_sort"])
    for r in rows:
        r.pop("_sort", None)
    return rows


def _default_geocode_fetch(url: str, timeout: float) -> Optional[dict]:
    """Fetch one Mapbox forward-geocode response, swallowing transport failures."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # nosec - fixed Mapbox host
            if getattr(response, "status", 200) != 200:
                return None
            result = json.loads(response.read().decode())
            return result if isinstance(result, dict) else None
    except Exception:
        return None


def _parse_geocode(data: Optional[dict]) -> Optional[tuple[float, float]]:
    """Return (lat, lng) from a Mapbox FeatureCollection, or None for bad data."""
    if not isinstance(data, dict):
        return None
    features = data.get("features") or []
    if not features or not isinstance(features[0], dict):
        return None
    coordinates = ((features[0].get("geometry") or {}).get("coordinates") or [])
    if (not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2
            or not isinstance(coordinates[0], (int, float)) or not isinstance(coordinates[1], (int, float))):
        return None
    lng, lat = float(coordinates[0]), float(coordinates[1])
    return lat, lng


def geocode_event_rows(rows: list[dict], *, token: Optional[str] = None, fetch=None) -> list[dict]:
    """Add lat/lng before advise_plan, retaining and flagging every un-geocoded event."""
    token = mapbox_token() if token is None else token
    fetch = fetch or _default_geocode_fetch
    deadline = time.monotonic() + GEOCODE_TOTAL_TIMEOUT_S
    enriched: list[dict] = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        lat, lng = row.get("lat"), row.get("lng")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            row["lat"], row["lng"] = float(lat), float(lng)
            row.pop("coordinates_missing", None)
            enriched.append(row)
            continue
        point = None
        address = row.get("address")
        remaining = deadline - time.monotonic()
        if token and remaining > 0 and isinstance(address, str) and address.strip():
            url = MAPBOX_GEOCODE_URL + "?" + urllib.parse.urlencode({
                "q": address, "limit": 1, "access_token": token,
            })
            try:
                data = fetch(url, min(GEOCODE_TIMEOUT_S, remaining))
            except Exception:
                data = None
            point = _parse_geocode(data)
        if point is not None:
            row["lat"], row["lng"] = point
            row.pop("coordinates_missing", None)
        else:
            row["coordinates_missing"] = True
        enriched.append(row)
    return enriched


def _fmt_pt_time(start_at_iso: Optional[str]) -> Optional[str]:
    """A UTC ISO8601 `startAt` -> 12-hour 'H:MM AM/PM' Pacific (cab-facing, never 24h).

    Reuses payload.fmt_clock_ampm for the final 12-hour formatting so events render
    with the exact same clock convention as the next-stop card. None/unparseable -> None.
    """
    if not start_at_iso:
        return None
    s = str(start_at_iso)
    try:
        # Jobber returns e.g. "2026-09-17T20:30:00Z"; also tolerate +00:00 offsets.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        local = dt.astimezone(PACIFIC)
    except (ValueError, TypeError):
        return None
    return fmt_clock_ampm(local.time())


# ── Day window (Pacific day -> UTC ISO) ───────────────────────────────────────
def _pt_day_window_utc(day: datetime.date) -> tuple[str, str]:
    """[start, end) of a PACIFIC calendar day as UTC ISO strings for the visits filter."""
    start_local = datetime.datetime.combine(day, datetime.time.min, tzinfo=PACIFIC)
    end_local = start_local + datetime.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        start_local.astimezone(datetime.timezone.utc).strftime(fmt),
        end_local.astimezone(datetime.timezone.utc).strftime(fmt),
    )


def dispatch_titles_for_driver(driver_canonical: str, day: datetime.date) -> Optional[list[str]]:
    """Cruise-sheet task titles. The backend no longer calls Jobber (events feed,
    Felix 2026-10-20), and the Hermes feed carries visits, not tasks, so this is a
    miss (None): the caller already treats None as "do not invent a city"."""
    return None


def _fetch_visits(day: datetime.date) -> Optional[list]:
    """Raw Pacific-day visit nodes from the Hermes events feed table, READ-ONLY.

    Returns the nodes list on SUCCESS (possibly empty: a real "no visits today") and
    `None` when the feed is unknown: no row for the day, the last Jobber pull failed
    (status='error'), the row is older than EVENTS_MAX_AGE_S, or the read failed. The
    reason is kept in feed_state(day) so the cab can say "Events unknown" instead of
    silently showing nothing. Success is cached _CACHE_TTL_S; failure is not. Never raises.
    """
    key = day.isoformat()
    now = time.monotonic()
    hit = _visits_cache.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL_S:
        return hit[1]
    nodes = _fetch_visits_db(day)
    if nodes is not None:
        _visits_cache[key] = (time.monotonic(), nodes)
    return nodes


def feed_state(day: Optional[datetime.date] = None) -> dict:
    """{"status": "ok"|"unknown", "version": int|None, "reason": str} for the last read."""
    day = day or datetime.datetime.now(PACIFIC).date()
    return dict(_feed_state.get(day.isoformat()) or
                {"status": "unknown", "version": None, "reason": "not read yet"})


def _fetch_visits_db(day: datetime.date) -> Optional[list]:
    import psycopg2

    def unknown(reason: str, version=None):
        _feed_state[day.isoformat()] = {"status": "unknown", "version": version, "reason": reason}
        return None

    try:
        conn = psycopg2.connect(load_database_url(), connect_timeout=DB_CONNECT_TIMEOUT_S)
    except Exception:
        return unknown("database unreachable")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT version, status, visits, EXTRACT(EPOCH FROM (now() - published_at)), error "
                f"FROM {EVENTS_TABLE} WHERE service_date = %s",
                (day,),
            )
            row = cur.fetchone()
    except Exception:
        return unknown("events table unreadable")
    finally:
        conn.close()
    if not row:
        return unknown("no events published for today yet")
    version, status, visits, age_s, _error = row
    if status != "ok":
        return unknown("last Jobber pull failed", version)
    if age_s is not None and float(age_s) > EVENTS_MAX_AGE_S:
        return unknown("events feed is stale", version)
    if isinstance(visits, str):
        try:
            visits = json.loads(visits)
        except ValueError:
            return unknown("events row unreadable", version)
    if not isinstance(visits, list):
        return unknown("events row unreadable", version)
    _feed_state[day.isoformat()] = {"status": "ok", "version": version, "reason": ""}
    return visits


# ── Fleet ticker inputs (same Jobber fetch; never a second client) ────────────
def _scorecard_square_name(name: str, mapping: dict) -> str:
    """Map a Jobber assignee without fuzzy matching a known map entry."""
    if name in mapping and mapping[name]:
        return str(mapping[name]).strip()
    normalized = _norm(name)
    for jobber_name, square_name in mapping.items():
        if _norm(jobber_name) == normalized and square_name:
            return str(square_name).strip()
    # No map entry: the Jobber name is the only honest candidate.  Do not infer a
    # similarly named Square person.
    return str(name).strip()


def get_today_scorecard_inputs(day: Optional[datetime.date] = None) -> Optional[dict]:
    """Return fleet ONE_OFF windows and every driver working today from one fetch.

    ``None`` means Jobber is unavailable.  The ticker consumes this stricter result
    rather than showing a partial rank.  Every assigned Jobber visit, not merely a
    ONE_OFF visit, establishes that a driver is working today.
    """
    day = day or datetime.datetime.now(PACIFIC).date()
    nodes = _fetch_visits(day)
    if nodes is None:
        return None
    try:
        mapping = jobber._load_map(jobber._map_path())
    except Exception:
        return None
    windows: list[dict] = []
    drivers: set[str] = set()
    for visit in nodes:
        if not isinstance(visit, dict):
            continue
        for assigned in ((visit.get("assignedUsers") or {}).get("nodes") or []):
            name = ((assigned.get("name") or {}).get("full") if isinstance(assigned, dict) else None)
            if not name:
                continue
            square_name = _scorecard_square_name(name, mapping)
            if square_name:
                drivers.add(square_name)
                if ((visit.get("job") or {}).get("jobType") == "ONE_OFF"
                        and visit.get("startAt") and visit.get("endAt")):
                    windows.append({"driver": square_name, "start": visit["startAt"], "end": visit["endAt"]})
    return {"windows": windows, "drivers": drivers}


def get_today_scorecard_windows(day: Optional[datetime.date] = None) -> Optional[list[dict]]:
    """Compatibility wrapper for callers that only need the ONE_OFF windows."""
    inputs = get_today_scorecard_inputs(day)
    return None if inputs is None else inputs["windows"]


# ── Public API ────────────────────────────────────────────────────────────────
def get_today_events(
    driver_canonical: str, day: Optional[datetime.date] = None
) -> list[dict]:
    """Today's ONE_OFF Jobber events for `driver_canonical` (Square canonical name).

    Returns a list of {title, startTime, address, status} for the driver's booked gigs
    on the PACIFIC `day` (default: today PT), sorted by start time. ALWAYS a list, NEVER
    `None`, NEVER raises. An empty list `[]` means the panel is HIDDEN and the map goes
    full-width — this is returned BOTH when Jobber succeeded but this driver has none
    today (a trusted "no events today" that clears a cancelled/reassigned booking or a
    day-rollover) AND on ANY failure (no token, DB/Jobber down, timeout, bad shape). The
    locked graceful-degradation contract is "token/Jobber error/timeout -> [] (panel
    hidden)": showing nothing during an outage is safe; holding a stale/wrong event that
    directs staff to a gig that may no longer exist is not.

    Caching is the ONLY place success and failure differ: a SUCCESSFUL result (incl. an
    empty `[]`) is cached ~5 min per (driver, day) to bound the costly visits query; a
    FAILURE returns `[]` but is NEVER cached, so the very next poll retries Jobber
    immediately rather than serving a cached blank.

    Filter: `job.jobType == "ONE_OFF"` AND the signed-in driver is in the visit's
    assignedUsers (resolved through the existing Jobber<->Square name map). An unmatched
    driver -> [] (a real, trusted "no events for you"), never another driver's events.
    """
    try:
        if day is None:
            day = datetime.datetime.now(PACIFIC).date()
        if not driver_canonical or not str(driver_canonical).strip():
            # No identity to match -> a trusted empty result (clear the panel), not a
            # failure: there is genuinely nothing to show for an unknown driver.
            return []
        from .school_slack import enabled
        key = (_norm(driver_canonical), day.isoformat(), enabled())
        now = time.monotonic()
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL_S:
            return hit[1]
        nodes = _fetch_visits(day)
        if nodes is None:
            # Jobber/token/DB failure -> clear the panel per the locked contract, but do
            # NOT cache: the next poll retries Jobber immediately instead of serving a
            # cached blank for 5 min. Returning [] (not None) means an outage that
            # coincides with a cancellation still clears the stale/wrong event.
            return []
        rows = geocode_event_rows(filter_and_format(nodes, driver_canonical))
        _cache[key] = (now, rows)  # cache SUCCESS only (incl. an empty [])
        return rows
    except Exception:
        # Absolute belt-and-braces: this feed must NEVER crash sign-in / the poll.
        # A crash is a failure -> [] (clear the panel per the locked contract), uncached.
        return []


def get_today_events_with_status(
    driver_canonical: str, day: Optional[datetime.date] = None
) -> tuple[list[dict], dict]:
    """(rows, {"status": "ok"|"unknown", "version": int|None}). Never raises."""
    try:
        day = day or datetime.datetime.now(PACIFIC).date()
        rows = get_today_events(driver_canonical, day)
        state = feed_state(day)
        return rows, {"status": state["status"], "version": state.get("version")}
    except Exception:
        return [], {"status": "unknown", "version": None}


def scheduled_visits_for_driver(driver_canonical: str, day: Optional[datetime.date] = None) -> list[dict]:
    """Today's RECURRING Jobber visits for this driver as plain timed stops.

    Rows: {kind: "visit", id, title, address, startTime, endTime}. They never take
    precedence over the plan (Master Route rule); they are shown at their booked time.
    """
    try:
        day = day or datetime.datetime.now(PACIFIC).date()
        if not driver_canonical or not str(driver_canonical).strip():
            return []
        nodes = _fetch_visits(day) or []
        out = []
        for v in nodes:
            if not isinstance(v, dict) or (v.get("job") or {}).get("jobType") != "RECURRING":
                continue
            assigned = [(n.get("name") or {}).get("full")
                        for n in ((v.get("assignedUsers") or {}).get("nodes") or []) if isinstance(n, dict)]
            if not _driver_matches(driver_canonical, [a for a in assigned if a]):
                continue
            addr = (v.get("property") or {}).get("address") or {}
            out.append({
                "kind": "visit", "id": v.get("id"), "title": v.get("title") or "Scheduled visit",
                "address": ", ".join(p for p in (addr.get("street"), addr.get("city")) if p) or None,
                "startTime": _fmt_pt_time(v.get("startAt")), "endTime": _fmt_pt_time(v.get("endAt")),
                "_sort": v.get("startAt") or "",
            })
        out.sort(key=lambda r: r["_sort"])
        for r in out:
            r.pop("_sort", None)
        return out
    except Exception:
        return []


def _clear_cache() -> None:
    """Test hook: drop the per-(driver,day) cache and the shared visit-node cache."""
    _cache.clear()
    _visits_cache.clear()
    _feed_state.clear()

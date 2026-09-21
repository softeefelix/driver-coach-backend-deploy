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

TOKEN — READ-ONLY consumer, NEVER refresh (jobber-automation single-refresher rule):
the ONLY Jobber-token refresher in the fleet is softeedashboard's server.py on Render.
This module READS the fleet token from Postgres `softy_dashboard_app_kv` key
`jobber_tokens_fleet` (app 9bcc2676, which has the needed scopes). On a 401 ONLY it
POSTs the Render poke-refresh endpoint and re-reads the row — it NEVER runs its own
refresh grant. This mirrors fleet-monitor/fleet_check.py's `_db_load_tokens` +
`jobber_gql`, but WITHOUT the self-refresh path (which fleet_check owns as the
primary refresher; the driver-coach backend must not be a second refresher).

GRACEFUL DEGRADATION — LOCKED CONTRACT (Felix 2026-09-17, reaffirmed by Warden): the
public function `get_today_events(...)` ALWAYS returns a `list` — it NEVER returns
`None` and NEVER raises. Both a successful "no events today" AND any failure return the
SAME thing on the wire: `[]` (panel hidden, map full-width). This is the locked
graceful-degradation rule ("token/Jobber error/timeout -> [] (panel hidden)"): a
cancelled/reassigned booking, a Pacific-day rollover, OR a Jobber/token/Postgres outage
must ALL clear the operational panel rather than leave stale/wrong booking info directing
staff to a gig that may no longer exist. Showing nothing is safe; showing a stale event
during an outage is not.

The success/failure distinction lives ONLY in CACHING, not in the returned value: a
SUCCESSFUL Jobber round-trip (incl. an empty `[]`) is cached ~5 min per (driver, day) to
bound the costly visits query; a FAILURE (no token, DB down, Jobber error/timeout <=4s
budget, bad shape, exception) returns `[]` but is NEVER cached, so the very next poll
retries Jobber immediately instead of serving a cached blank for 5 minutes. `_fetch_visits`
returns `None` INTERNALLY to signal "failure, don't cache" vs a real empty `[]`; that
internal `None` is converted to a public `[]` here and never leaks past this module.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.request
from typing import Optional
from zoneinfo import ZoneInfo

from . import jobber
from .db import load_database_url
from .payload import fmt_clock_ampm

# ── Constants (mirror fleet_check.py's working values) ────────────────────────
JOBBER_GQL = "https://api.getjobber.com/api/graphql"
JOBBER_VER = "2026-02-17"
FLEET_TOKENS_KEY = "jobber_tokens_fleet"
# The ONE refresher (softeedashboard on Render). We only POKE it on a 401 — we never
# run our own refresh grant (single-refresher rule, jobber-automation skill).
REFRESH_POKE_URL = "https://softeedashboard.onrender.com/api/jobber/refresh-token"

PACIFIC = ZoneInfo("America/Los_Angeles")

# Total wall-clock budget for the whole Jobber round-trip. The route poll / sign-in
# must not stall on a slow Jobber; past this we give up and return [] (panel hidden).
JOBBER_TIMEOUT_S = 4.0
# DB connect budget is inside the same overall spirit — a slow cold Render Postgres
# must not hang sign-in. Kept short; a miss just means [] (panel hidden).
DB_CONNECT_TIMEOUT_S = 4

# Cache ~5 min per (driver_lower, day_iso) — the visits query cost is high.
_CACHE_TTL_S = 300
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

# The verified working query (card §VERIFIED JOBBER MECHANICS; probed live today).
# `first: 30` is the visits-query cost cap — NEVER raise it.
_DAY_VISITS_QUERY = """
query DayVisits($after: ISO8601DateTime!, $before: ISO8601DateTime!) {
  visits(filter: { startAt: { after: $after, before: $before } }, first: 30) {
    nodes {
      title startAt visitStatus
      property { address { street city } }
      job { jobType }
      assignedUsers { nodes { name { full } } }
    }
  }
}
"""


# ── Name matching (reuse the existing Jobber<->Square resolution seam) ─────────
def _norm(name: Optional[str]) -> str:
    """Normalize a name for comparison: trimmed, collapsed whitespace, lowercase."""
    return " ".join((name or "").split()).lower()


def _driver_matches(driver_canonical: str, assigned_full_names: list[str]) -> bool:
    """True iff the signed-in driver is one of a visit's assigned users.

    The coach's driver is a CANONICAL (Square) name; Jobber shows a display form
    ("Emery N", "Brian G", "Nate"). We resolve EACH assigned Jobber name through the
    SAME jobber_name_map seam the rest of the stack uses (jobber.resolve_square_name)
    and compare the resolved Square name to the driver's canonical (normalized). We
    ALSO accept a direct normalized match (a Jobber name that is already the canonical
    form, or one with no map entry). If nothing matches -> False, so an unmatched
    driver shows 0 events and NEVER another driver's events.
    """
    want = _norm(driver_canonical)
    if not want:
        return False
    for full in assigned_full_names:
        if _norm(full) == want:
            return True
        if _norm(jobber.resolve_square_name(full)) == want:
            return True
    return False


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
        rows.append(
            {
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


# ── Token (READ-ONLY) + Jobber GraphQL (poke-refresh on 401, never self-grant) ─
def _db_load_token() -> Optional[str]:
    """Read the fleet Jobber access token from Postgres, READ-ONLY. None on any miss.

    Reuses db.load_database_url() (the same shared Render Postgres the backend already
    talks to). If the hosted backend cannot reach that Postgres this simply returns
    None and the panel stays hidden — we do NOT hardcode creds in the public mirror.
    """
    import psycopg2

    try:
        conn = psycopg2.connect(load_database_url(), connect_timeout=DB_CONNECT_TIMEOUT_S)
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM softy_dashboard_app_kv WHERE key = %s",
                (FLEET_TOKENS_KEY,),
            )
            row = cur.fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    try:
        tokens = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    except (ValueError, TypeError):
        return None
    tok = tokens.get("access_token")
    return tok or None


def _poke_refresh(deadline: float) -> None:
    """Ask the ONE refresher (softeedashboard on Render) to refresh the token.

    We NEVER run our own refresh grant (single-refresher rule). This is a fire-and-
    (best-effort)-forget POST; the refresher rotates the DB row and we re-read it.
    Bounded by the remaining time budget; any error is swallowed (caller falls back).
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        return
    try:
        req = urllib.request.Request(REFRESH_POKE_URL, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=min(remaining, JOBBER_TIMEOUT_S))
    except Exception:
        return


def _jobber_gql(token: str, query: str, variables: dict, timeout: float) -> Optional[dict]:
    """POST a GraphQL query. Returns the parsed body, or raises urllib.error.HTTPError
    (so the caller can detect a 401), or returns None on any other transport error."""
    data = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(JOBBER_GQL, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-JOBBER-GRAPHQL-VERSION": JOBBER_VER,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fetch_visits(day: datetime.date) -> Optional[list]:
    """Fetch the raw Pacific-day visit nodes from Jobber, READ-ONLY, within the time
    budget. On a 401 (expired token) poke the ONE refresher ONCE, re-read the token,
    and retry.

    Returns the raw nodes list on SUCCESS (possibly an EMPTY list — a real "no visits
    today"); returns `None` on ANY failure (no token, timeout, HTTP/transport error,
    GraphQL errors, bad shape) so the caller can distinguish a trusted empty day (clear
    the panel) from a transient blip (hold the last list). Never raises.
    """
    deadline = time.monotonic() + JOBBER_TIMEOUT_S
    after, before = _pt_day_window_utc(day)
    variables = {"after": after, "before": before}

    token = _db_load_token()
    if not token:
        return None

    def _remaining() -> float:
        return deadline - time.monotonic()

    for attempt in (1, 2):
        rem = _remaining()
        if rem <= 0.2:
            return None
        try:
            body = _jobber_gql(token, _DAY_VISITS_QUERY, variables, timeout=rem)
        except urllib.error.HTTPError as e:
            # 401 -> token expired. Poke the single refresher ONCE, re-read, retry.
            if e.code == 401 and attempt == 1:
                _poke_refresh(deadline)
                token = _db_load_token()
                if not token:
                    return None
                continue
            return None
        except Exception:
            return None
        if not isinstance(body, dict) or body.get("errors"):
            return None
        return ((body.get("data") or {}).get("visits") or {}).get("nodes") or []
    return None


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
        key = (_norm(driver_canonical), day.isoformat())
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
        rows = filter_and_format(nodes, driver_canonical)
        _cache[key] = (now, rows)  # cache SUCCESS only (incl. an empty [])
        return rows
    except Exception:
        # Absolute belt-and-braces: this feed must NEVER crash sign-in / the poll.
        # A crash is a failure -> [] (clear the panel per the locked contract), uncached.
        return []


def _clear_cache() -> None:
    """Test hook: drop the per-(driver,day) cache."""
    _cache.clear()

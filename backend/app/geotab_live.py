"""LIVE Geotab position from DeviceStatusInfo — the truck's CURRENT lat/lng, now.

Why this module exists (Felix road-test, truck 13 / Emery): the app resolved the
nearest stop from `public.log_records`, but that table is ~16h STALE and its ingester
(master-route/render-python/getGeotabLogRecord.py) deliberately SKIPS moving trucks
(`if record["speed"] > 0: continue` — parked-only, great for route analysis, useless
for a live coach). So the app showed HAYWARD while the truck was really in SOUTH SAN
FRANCISCO. `DeviceStatusInfo` returns the device's CURRENT position whether moving or
parked — the correct primary source for a live coach.

`live_position(truck_no|device_id) -> {lat, lng, fix_age_s, speed}`
  - queries `DeviceStatusInfo` scoped to the device (`deviceSearch:{id:<device>}`)
  - parses the freshest matching fix; caches ~15s per device to bound API calls
  - authenticates the mygeotab client ONCE (module-level, lazy) and reuses the session;
    re-auths on a dropped session
  - returns None on ANY failure (unconfigured / no creds / api error / timeout / no fix)
    so the caller (routes.resolve_live_route) can fall back to the stale log_records
    position and NOTHING crashes.

READ-ONLY (Geotab reads only, no writes anywhere). Creds from env
GEOTAB_DATABASE/GEOTAB_USERNAME/GEOTAB_PASSWORD (Felipe sets them on the Render
service; ~/.hermes/secrets/geotab.env for local build/test). No coordinates ever
leave the server — this feeds the server-side phase/ETA math only.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import datetime
import os
import threading
import time
from typing import Optional

_UTC = datetime.timezone.utc

_KMH_TO_MPH = 0.621371192      # Geotab DeviceStatusInfo.speed is km/h; fleet math is mph

_TIMEOUT_S = 4.0        # brief: bound the live call to <=4s, then graceful fallback
_CACHE_TTL_S = 15.0     # brief: cache the live position ~15s per truck/device

# device_id -> (expires_monotonic, position_or_None)
_cache: dict[str, tuple[float, Optional[dict]]] = {}
_cache_lock = threading.Lock()

# The module-level mygeotab client — authenticated ONCE, reused across requests
# (do NOT re-auth per poll). Guarded so a dropped session re-auths on the next call.
_client = None
_client_lock = threading.Lock()


def _load_creds() -> Optional[dict]:
    """The Geotab credentials from the environment, or None if not fully configured.

    Env is the source of truth (Felipe sets GEOTAB_* on the Render service). Returns
    None (never raises) when any of the three is missing so live_position degrades to
    the log_records fallback instead of crashing the route feed.
    """
    db = (os.environ.get("GEOTAB_DATABASE") or "").strip()
    user = (os.environ.get("GEOTAB_USERNAME") or "").strip()
    pwd = os.environ.get("GEOTAB_PASSWORD") or ""
    if not (db and user and pwd):
        return None
    return {"database": db, "username": user, "password": pwd}


def _new_client(creds: dict, timeout_s: float):
    """Build + authenticate a fresh mygeotab.API. Import is local so the module loads
    (and its pure parser stays testable) even where mygeotab is absent."""
    import mygeotab

    api = mygeotab.API(
        username=creds["username"],
        password=creds["password"],
        database=creds["database"],
        timeout=timeout_s,
    )
    api.authenticate()
    return api


def _get_client(timeout_s: float = _TIMEOUT_S):
    """The shared, lazily-authenticated Geotab client. Authenticates ONCE and reuses
    the session across polls. Raises if unconfigured or auth fails (the caller treats
    any exception as "fall back to log_records")."""
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        creds = _load_creds()
        if creds is None:
            raise RuntimeError("Geotab not configured (GEOTAB_DATABASE/USERNAME/PASSWORD unset)")
        _client = _new_client(creds, timeout_s)
        return _client


def _reset_client() -> None:
    """Drop the cached client so the next call re-authenticates (dropped session)."""
    global _client
    with _client_lock:
        _client = None


def _coerce_dt(value) -> Optional[datetime.datetime]:
    """A Geotab dateTime (aware datetime or ISO-8601 string) -> a tz-aware UTC datetime,
    or None if it cannot be parsed. Naive datetimes are assumed UTC."""
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def parse_device_status(
    records, device_id: str, now: Optional[datetime.datetime] = None
) -> Optional[dict]:
    """Fold DeviceStatusInfo records into the freshest {lat, lng, fix_age_s, speed}
    for `device_id`, or None when no usable fix exists for that device.

    Pure + deterministic (tests pin `now`). Filters to the requested device, keeps the
    record with the newest dateTime, and rejects records with missing coordinates. This
    is the CURRENT position moving-or-parked — the whole point over log_records.
    """
    now = now or datetime.datetime.now(_UTC)
    best_dt = None
    best = None
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        dev = rec.get("device") or {}
        if not isinstance(dev, dict) or dev.get("id") != device_id:
            continue
        lat, lng = rec.get("latitude"), rec.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            continue
        dt = _coerce_dt(rec.get("dateTime"))
        # A record with no parseable timestamp still counts, but a timestamped one
        # always wins (we want the freshest). Use epoch-min for the untimed case.
        cmp_dt = dt or datetime.datetime.min.replace(tzinfo=_UTC)
        if best_dt is None or cmp_dt > best_dt:
            best_dt, best = cmp_dt, (float(lat), float(lng), dt, rec.get("speed"))
    if best is None:
        return None
    lat, lng, dt, speed = best
    fix_age_s = 0.0
    if dt is not None:
        fix_age_s = round(max((now - dt).total_seconds(), 0.0), 1)
    # Geotab reports speed in km/h; the fleet's motion thresholds + geotab.latest_fix
    # are all mph, so convert here to keep the whole stack in one unit.
    speed_mph = round(float(speed) * _KMH_TO_MPH, 2) if isinstance(speed, (int, float)) else None
    return {
        "lat": lat,
        "lng": lng,
        "fix_age_s": fix_age_s,
        "speed": speed_mph,
    }


def _fetch_device_status(api, device_id: str) -> list:
    """One READ-ONLY DeviceStatusInfo query scoped to the device. Returns [] on a
    None body. Scoping by deviceSearch keeps the payload tiny (one device)."""
    return api.get("DeviceStatusInfo", search={"deviceSearch": {"id": device_id}}) or []


def resolve_device_id(
    truck_no: Optional[int] = None,
    device_id: Optional[str] = None,
    cur=None,
) -> Optional[str]:
    """The Geotab device id for a truck. Prefers an explicit `device_id`; otherwise
    maps `truck_no` via public.trucks.truck_id (truck 13 -> b18) using `cur`. Returns
    None when it cannot resolve one (never guesses)."""
    if device_id:
        return device_id
    if truck_no is not None and cur is not None:
        try:
            cur.execute(
                "SELECT truck_id FROM public.trucks WHERE truck_number = %s", (truck_no,)
            )
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            return None
    return None


def live_position(
    truck_no: Optional[int] = None,
    device_id: Optional[str] = None,
    cur=None,
    *,
    api=None,
    now: Optional[datetime.datetime] = None,
    use_cache: bool = True,
    timeout_s: float = _TIMEOUT_S,
) -> Optional[dict]:
    """The truck's CURRENT position from Geotab DeviceStatusInfo, or None (graceful).

    Resolves the device (explicit `device_id`, else `truck_no` via public.trucks),
    reads its freshest DeviceStatusInfo fix, and returns {lat, lng, fix_age_s, speed}.
    Cached ~15s per device so a ~20s poll makes at most one Geotab call. `api` is
    injectable for tests (default: the shared, lazily-authenticated module client).

    Returns None on EVERY failure path — unconfigured, no device, api error/timeout,
    no fix — so routes.resolve_live_route falls back to the stale log_records position
    and nothing crashes. NEVER raises to the caller.
    """
    dev = resolve_device_id(truck_no=truck_no, device_id=device_id, cur=cur)
    if not dev:
        return None

    if use_cache:
        with _cache_lock:
            hit = _cache.get(dev)
            if hit is not None and hit[0] > time.monotonic():
                return hit[1]

    result: Optional[dict] = None
    try:
        client = api if api is not None else _get_client(timeout_s)
        try:
            records = _fetch_device_status(client, dev)
        except Exception:
            # A dropped/expired session (or transient error) on the shared client:
            # reset so the next call re-auths, then one retry with a fresh client.
            if api is None:
                _reset_client()
                client = _get_client(timeout_s)
                records = _fetch_device_status(client, dev)
            else:
                raise
        result = parse_device_status(records, dev, now)
    except Exception:
        result = None

    if use_cache:
        with _cache_lock:
            _cache[dev] = (time.monotonic() + _CACHE_TTL_S, result)
    return result


def clear_cache() -> None:
    """Drop the position cache (tests; harmless in prod)."""
    with _cache_lock:
        _cache.clear()

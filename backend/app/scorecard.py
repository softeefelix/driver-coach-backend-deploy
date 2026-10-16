"""Live operational sales ticker: Square payments + the existing Jobber events feed.

A missing Square token/API failure or Jobber failure returns ``None`` to omit the
whole ticker.  It never substitutes estimates, cached rankings, or fake dollars.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional
from zoneinfo import ZoneInfo

from . import events, jobber

PACIFIC = ZoneInfo("America/Los_Angeles")
SQUARE_API = "https://connect.squareup.com/v2/"
SQUARE_LOCATIONS = ("LCFDDTKMWV4A2", "L32M2CHVBQ17K")
SQUARE_VERSION = "2024-01-18"
PREPAID_CREDIT_PER_HOUR = 200.0
VENUE_SALES_FACTOR = 0.67
PSEUDO_LOGINS = {"trainee trainee", "untracked teammember"}


def _norm(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _parse_dt(value: object) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _event_hours(start: dt.datetime, end: dt.datetime) -> float:
    return max((end - start).total_seconds() / 3600.0, 1.0)


def _inside(when: dt.datetime, window: dict) -> bool:
    start, end = _parse_dt(window.get("start")), _parse_dt(window.get("end"))
    return bool(start and end and start <= when <= end)


def build_scorecard(
    *, payments: list[dict], team: dict[str, str], event_windows: list[dict],
    signed_in: str, candidates: set[str],
) -> list[dict]:
    """Pure fleet scorecard, using cents and exact mapped Square employee names.

    ``event_windows`` entries are ``{driver, start, end}``; every completed payment in
    a cash-sale window receives the locked 67% factor. A window with no payments earns
    $200/hour. Pseudo logins never produce a row.
    """
    usable = {_norm(name): str(name).strip() for name in candidates if _norm(name) not in PSEUDO_LOGINS}
    sales: dict[str, float] = {name: 0.0 for name in usable}
    window_sales = [0.0 for _ in event_windows]

    for payment in payments or []:
        if str(payment.get("status") or "").upper() not in {"COMPLETED", "APPROVED"}:
            continue
        name = team.get(payment.get("team_member_id"))
        key = _norm(name)
        when = _parse_dt(payment.get("created_at"))
        cents = ((payment.get("amount_money") or {}).get("amount"))
        if not name or key not in usable or when is None or not isinstance(cents, (int, float)) or cents <= 0:
            continue
        dollars = float(cents) / 100.0
        matching = [i for i, window in enumerate(event_windows)
                    if _norm(window.get("driver")) == key and _inside(when, window)]
        if matching:
            # A payment belongs to at most one scheduled window; retaining the first
            # makes overlapping bad Jobber data deterministic rather than double-counted.
            window_sales[matching[0]] += dollars
            sales[key] += dollars * VENUE_SALES_FACTOR
        else:
            sales[key] += dollars

    for index, window in enumerate(event_windows):
        key = _norm(window.get("driver"))
        start, end = _parse_dt(window.get("start")), _parse_dt(window.get("end"))
        if key in usable and start and end and end > start and window_sales[index] == 0:
            sales[key] += PREPAID_CREDIT_PER_HOUR * _event_hours(start, end)

    signed_key = _norm(signed_in)
    # ``candidates`` is the real working-today list.  A driver without a sale or a
    # ONE_OFF event still appears with $0.00; omitting them would falsely imply they
    # are not working.
    rows = [
        {"name": display, "dollars": round(sales[key], 2), "signedIn": key == signed_key}
        for key, display in usable.items()
    ]
    return sorted(rows, key=lambda row: (-row["dollars"], row["name"].casefold()))


def _square_token(path: str = "~/.hermes/secrets/softeedashboard.env") -> Optional[str]:
    try:
        with open(os.path.expanduser(path)) as handle:
            for line in handle:
                if line.startswith("SQUARE_TOKEN="):
                    return line.partition("=")[2].strip().strip('"') or None
    except OSError:
        return None
    return None


def _square_get(path: str, token: str) -> Optional[dict]:
    request = urllib.request.Request(
        urllib.parse.urljoin(SQUARE_API, path),
        headers={"Authorization": f"Bearer {token}", "Square-Version": SQUARE_VERSION},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:  # nosec B310: fixed API host
            body = json.loads(response.read())
            return body if isinstance(body, dict) else None
    except Exception:
        return None


def _square_post(path: str, token: str, payload: dict) -> Optional[dict]:
    request = urllib.request.Request(
        urllib.parse.urljoin(SQUARE_API, path),
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Square-Version": SQUARE_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:  # nosec B310: fixed API host
            body = json.loads(response.read())
            return body if isinstance(body, dict) else None
    except Exception:
        return None


def _team(token: str) -> Optional[dict[str, str]]:
    # Search is POST. A GET to /team-members 404s and used to hide the whole ticker.
    out: dict[str, str] = {}
    cursor = None
    while True:
        payload = {"limit": 100}
        if cursor:
            payload["cursor"] = cursor
        body = _square_post("team-members/search", token, payload)
        if body is None:
            return None
        for member in body.get("team_members") or []:
            ident = member.get("id")
            name = " ".join(p for p in (member.get("given_name"), member.get("family_name")) if p).strip()
            if ident and name:
                out[ident] = name
        cursor = body.get("cursor")
        if not cursor:
            return out


def _today_payments(token: str, day: dt.date) -> Optional[list[dict]]:
    start = dt.datetime.combine(day, dt.time.min, PACIFIC).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, PACIFIC).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    result: list[dict] = []
    for location in SQUARE_LOCATIONS:
        cursor = None
        while True:
            query = {"begin_time": start, "end_time": end, "location_id": location, "limit": "100"}
            if cursor:
                query["cursor"] = cursor
            body = _square_get("payments?" + urllib.parse.urlencode(query), token)
            if body is None:
                return None
            result.extend(p for p in (body.get("payments") or []) if isinstance(p, dict))
            cursor = body.get("cursor")
            if not cursor:
                break
    return result


_sale_lock = threading.Lock()
_sale_cache: dict[str, tuple[float, Optional[list], Optional[dict]]] = {}
_SALE_TTL_S = 15.0
_SALE_FAIL_TTL_S = 10.0


def clear_sale_cache() -> None:
    with _sale_lock:
        _sale_cache.clear()


def _payment_counts(payment: dict) -> bool:
    if str(payment.get("status") or "").upper() not in {"COMPLETED", "APPROVED"}:
        return False
    cents = (payment.get("amount_money") or {}).get("amount")
    return isinstance(cents, (int, float)) and not isinstance(cents, bool) and cents > 0


def _mentions_truck(payment: dict, truck_no: int) -> bool:
    """True when a payment note, device, or reference names this truck.

    ``truck 7`` / ``t7`` match truck 7. ``truck 17`` and ``t17`` do not.
    """
    parts = []
    for key in ("note", "reference_id"):
        value = payment.get(key)
        if isinstance(value, str):
            parts.append(value)
    device = payment.get("device_details")
    if isinstance(device, dict):
        for key in ("device_name", "device_installation_id"):
            value = device.get(key)
            if isinstance(value, str):
                parts.append(value)
    text = " ".join(parts)
    if not text:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9])(?:truck[\s#\-]*{int(truck_no)}|t{int(truck_no)})(?!\d)",
        text,
        re.IGNORECASE,
    ) is not None


def _cached_today_payments(now: dt.datetime) -> tuple[Optional[list], Optional[dict]]:
    """Today's Square payments and team directory, shared across the ~20s polls."""
    day = now.astimezone(PACIFIC).date()
    key = day.isoformat()
    with _sale_lock:
        hit = _sale_cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1], hit[2]
    token = _square_token()
    if not token:
        with _sale_lock:
            _sale_cache[key] = (time.monotonic() + _SALE_FAIL_TTL_S, None, None)
        return None, None
    team, payments = _team(token), _today_payments(token, day)
    ttl = _SALE_TTL_S if team is not None and payments is not None else _SALE_FAIL_TTL_S
    with _sale_lock:
        _sale_cache[key] = (time.monotonic() + ttl, payments, team)
    return payments, team


def last_truck_sale(
    *,
    truck_no: int,
    driver_name: str = "",
    now: Optional[dt.datetime] = None,
    payments: Optional[list] = None,
    team: Optional[dict] = None,
) -> dict:
    """Latest completed Square sale for this truck, and whether that answer is known.

    A sale counts when the payment's team member is the signed-in driver, or when
    the payment itself names the truck. ``known`` is false when Square cannot be
    read, and when there is no driver name to attribute an otherwise untagged
    payment — absence is not proof of no sale in those cases. Pass ``payments``
    and ``team`` to skip the network (tests).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if payments is None or team is None:
        payments, team = _cached_today_payments(now)
    if payments is None or team is None:
        return {"known": False, "last_sale_at": None}
    driver_known = bool(_norm(driver_name))
    last = None
    matched = False
    for payment in payments:
        if not isinstance(payment, dict) or not _payment_counts(payment):
            continue
        member = team.get(payment.get("team_member_id"))
        by_driver = driver_known and bool(member) and _norm(member) == _norm(driver_name)
        by_truck = _mentions_truck(payment, truck_no)
        if not (by_driver or by_truck):
            continue
        matched = True
        when = _parse_dt(payment.get("created_at"))
        if when is not None and (last is None or when > last):
            last = when
    if not driver_known and not matched:
        return {"known": False, "last_sale_at": None}
    return {"known": True, "last_sale_at": last}


def live_ticker(signed_in: str, day: Optional[dt.date] = None) -> Optional[list[dict]]:
    """Fetch today's live inputs. ``None`` means omit ticker (never invent a rank)."""
    day = day or dt.datetime.now(PACIFIC).date()
    inputs = events.get_today_scorecard_inputs(day)
    if inputs is None:
        return None
    token = _square_token()
    if not token:
        return None
    team, payments = _team(token), _today_payments(token, day)
    if team is None or payments is None:
        return None
    return build_scorecard(
        payments=payments, team=team, event_windows=inputs["windows"], signed_in=signed_in,
        candidates=set(inputs["drivers"]),
    )

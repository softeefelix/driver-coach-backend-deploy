"""Live operational sales ticker: Square payments + the existing Jobber events feed.

A missing Square token/API failure or Jobber failure returns ``None`` to omit the
whole ticker.  It never substitutes estimates, cached rankings, or fake dollars.
"""
from __future__ import annotations

import datetime as dt
import json
import os
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


def _team(token: str) -> Optional[dict[str, str]]:
    # The list endpoint is sufficient: our map is authoritative and can match exact
    # employee names even if a non-active member must still appear in today's payments.
    body = _square_get("team-members?limit=100", token)
    if body is None:
        return None
    out: dict[str, str] = {}
    for member in body.get("team_members") or []:
        ident = member.get("id")
        name = " ".join(p for p in (member.get("given_name"), member.get("family_name")) if p).strip()
        if ident and name:
            out[ident] = name
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

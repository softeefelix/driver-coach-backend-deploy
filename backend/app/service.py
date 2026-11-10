"""Driver Coach v1 service — FastAPI. Endpoints replacing the shipped app's mock
seams + the production-ization roster/route feeds:

  POST /driver-coach/v1/signin     replaces app.js mockSignInPayload()
  POST /driver-coach/v1/heartbeat  replaces app.js startLiveness() transport ACK
  GET  /driver-coach/v1/roster     the REAL truck→driver roster (replaces app.js
                                   hardcoded NAMES/TRUCKS)
  GET  /driver-coach/v1/route      the LIVE route snapshot the app polls to follow
                                   the truck's real progress (nextStop + live phase).
                                   Also carries no-idle `nudge` and `break` state.
  POST /driver-coach/v1/break/start   start the one daily 30-minute break
  POST /driver-coach/v1/break/end     end that break early
  POST /driver-coach/v1/square-offline/start   mark Square offline (60-minute cap)
  POST /driver-coach/v1/square-offline/end     end that mark early
  GET  /driver-coach/v1/idle-exclusions        office read for Hermes, no driver session

signin: {name, truck} -> Jobber driver-of-record -> REAL driver_profiles snapshot
-> resolve_flags() (frozen for the session) -> disguise-safe payload (no boolean on
the wire) + real next stop + server-computed motion phase. Persists PriorFlagState
+ a driver_coach_session row.

heartbeat: accepts the liveness batch the client already sends (liveness.js shape),
writes driver_coach_heartbeat idempotently, ACKs the max stored seq, and runs the
conservative liveness classify scaffold when a gap is present.

roster: the real active fleet — active driver display names (from the profiles
snapshot, disclosure gated by ONE env switch) + real active truck numbers (public
trucks + recent route signal). READ-ONLY.

route: the live snapshot for a session_id — current nextStop, LIVE Geotab motion
phase, §11 shiftPhase, routeCount, and (disguise-safe, coached-only) the next-stop
grade. Confirmed Done/Skip orders are read-only on this poll. Geotab supplies truck
tracking and a separate non-authoritative adherence estimate; it does not write them.

HARD CONSTRAINT (Felix gate): this targets DRIVER_COACH_SCHEMA (default
'driver_coach'), NEVER public. It is BUILD + TEST ONLY — not deployed, the live
iPad endpoint is unchanged. Deploy is a separate Felix-gated step (see render.yaml,
which is the SHAPE only and is never executed here).

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import datetime
import json
import logging
import threading
import uuid
from typing import Any, Optional

import psycopg2
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import assignment, breaks, flagstate, geotab, jobber, liveness, no_idle, profiles, roster, routes
from . import idle_scorecard, idle_status, nudge_suggestions, office_key, square_offline
from . import events as events_mod


def _todays_bookings(driver_name: str):
    """(ONE_OFF events, events + RECURRING visits for the advisor, feed status). Never raises."""
    try:
        evs, status = events_mod.get_today_events_with_status(driver_name)
    except Exception:
        evs, status = [], {"status": "unknown", "version": None}
    try:
        visits = events_mod.scheduled_visits_for_driver(driver_name)
    except Exception:
        visits = []
    return evs, list(evs) + list(visits), status


def _with_leave_by(evs: list, next_stop) -> list:
    """Copy the live leave-by (ETA + buffer) onto the event it belongs to."""
    ctx = (next_stop or {}).get("anchor_context") if isinstance(next_stop, dict) else None
    if not isinstance(ctx, dict) or ctx.get("kind") != "event" or not ctx.get("leaveBy"):
        return evs
    out = []
    for e in evs:
        e = dict(e)
        if e.get("title") == ctx.get("name") or e.get("address") == ctx.get("name"):
            e["leaveBy"] = ctx["leaveBy"]
        out.append(e)
    return out
from . import scorecard
from . import school_slack
from .square_oauth import router as square_oauth_router
from .config import flag_config, mapbox_token
from .db import DEFAULT_SCHEMA, connect
from .motion import phase_for_fix
from .driver_progress import (
    ProgressConflict,
    bump,
    empty_progress,
    first_stop_order,
    from_state,
    plan_stop,
    require_revision,
)
from .payload import build_plan_stops, build_route_payload, build_session_payload, friendly_stop_name
from .refcore import DRIVING
from .refuel import RefuelNightBook, Reminder

app = FastAPI(title="Driver Coach v1", version="1.0.0")
app.include_router(square_oauth_router)
_log = logging.getLogger(__name__)
_square_missing_lock = threading.Lock()
_square_table_missing_logged = False
_scorecard_missing_lock = threading.Lock()
_scorecard_table_missing_logged = False


def _note_missing_square_table() -> None:
    """Log a missing 008 table once per process, not on every route poll."""
    global _square_table_missing_logged
    with _square_missing_lock:
        if _square_table_missing_logged:
            return
        _square_table_missing_logged = True
    _log.warning(
        "square_offline_period is missing; apply migration 008. "
        "Route polls keep serving navigation."
    )


def _note_missing_scorecard_table() -> None:
    """Log a missing 009 table once per process, not on every route poll."""
    global _scorecard_table_missing_logged
    with _scorecard_missing_lock:
        if _scorecard_table_missing_logged:
            return
        _scorecard_table_missing_logged = True
    _log.warning(
        "idle_scorecard is missing; apply migration 009. "
        "Route polls omit scorecard until it exists."
    )


# --------------------------------------------------------------------------- #
# Request models (mirror what the client already sends).
# --------------------------------------------------------------------------- #
class SignInRequest(BaseModel):
    """The two sign-in picks. The CLIENT sends `driver_id` (the opaque roster token —
    sha256(canonical)[:16]); the server resolves it back to the canonical name over
    the active snapshot so the full name never rides the public wire. `name` is a
    LEGACY back-compat path (resolved directly by canonical name) used only when no
    driver_id is supplied — nothing else in the stack should send a bare name."""

    truck: int
    driver_id: Optional[str] = None
    name: Optional[str] = None


class ConfirmAssignmentRequest(BaseModel):
    session_id: str
    route_cluster_id: int


class StopOutcomeRequest(BaseModel):
    """A deliberate driver outcome. Only the current frozen-plan stop may change.

    ``expected_revision`` is optional so an older cab can still post Done/Skip.
    When it is present it must match ``route.progressRevision`` or the write is
    refused and nothing is stored.
    """
    session_id: str
    stop_order: int
    outcome: str
    expected_revision: Optional[int] = None


class StartOverRequest(BaseModel):
    """Explicit driver tap. Clears stored outcomes and aims at the first uploaded pin."""
    session_id: str
    expected_revision: int


class SetTargetRequest(BaseModel):
    """Explicit Jump or Back. Selects one uploaded pin and does not complete any other."""
    session_id: str
    stop_order: int
    expected_revision: int


class Heartbeat(BaseModel):
    # Mirrors liveness.js buildHeartbeat output; extra keys allowed.
    seq: int
    truck_no: Optional[int] = None
    driver_id: Optional[str] = None
    device_uptime_s: Optional[float] = None
    boot_id: Optional[str] = None
    first_launch_after_boot: Optional[bool] = None
    app_state: Optional[str] = None
    kiosk: Optional[bool] = None
    battery: Optional[dict] = None
    net: Optional[dict] = None
    last_interaction_s: Optional[float] = None
    gps: Optional[dict] = None
    stop_ctx: Optional[dict] = None
    client_ts: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class HeartbeatRequest(BaseModel):
    session_id: str
    batch: list[Heartbeat] = Field(default_factory=list)


class BreakStartRequest(BaseModel):
    session_id: str
    type: str


class BreakEndRequest(BaseModel):
    session_id: str


class SquareOfflineRequest(BaseModel):
    session_id: str


# --------------------------------------------------------------------------- #
# Core logic (schema-injectable so tests bind an isolated scratch schema).
# --------------------------------------------------------------------------- #
def _resolve_canonical(req: SignInRequest) -> str:
    """Resolve the sign-in request to a CANONICAL driver name.

    Preferred path: the client sends the opaque `driver_id` (sha256(canonical)[:16]);
    we rebuild the same id->canonical map over the active snapshot and look it up, so
    the full name is derived SERVER-SIDE and never has to cross the public wire. An
    unknown driver_id is a clean 400 (surfaced by the route handler). LEGACY path: a
    bare `name` (no driver_id) resolves directly by canonical name for back-compat.
    """
    if req.driver_id:
        canonical = roster.id_to_canonical().get(req.driver_id)
        if not canonical:
            raise ValueError(f"unknown driver_id: {req.driver_id}")
        return canonical
    if req.name:
        return req.name
    raise ValueError("sign-in requires a driver_id (or a legacy name)")


def do_signin(req: SignInRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    driver_name = _resolve_canonical(req)
    dor = jobber.resolve_driver_of_record(driver_name)
    driver_id = dor["driver_id"]
    square_name = dor["square_name"]

    # REAL latest driver_profiles snapshot -> DriverProfile.
    profile = profiles.build_driver_profile(driver_id, square_name)

    # Today's ONE_OFF Jobber events for THIS driver (operational, both modes identical).
    # Fetched BEFORE the DB transaction so a slow Jobber never holds the session write
    # open. get_today_events ALWAYS returns a list ([] on a genuine empty day AND on any
    # failure — the locked "-> [] (panel hidden)" contract) and NEVER raises; the extra
    # try/except is belt-and-braces so the feed can NEVER crash sign-in. A crash here is a
    # failure -> [] (clear the panel per the contract), never None (which would omit the
    # key and leave a stale panel up).
    today_events, _advise_events, events_status = _todays_bookings(square_name)

    session_id = str(uuid.uuid4())
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            # hysteresis: one increment per Pacific day. A later sign-in the same
            # Pacific day keeps both counters and the coached decision as they were.
            flags = flagstate.advance_for_signin(
                cur, driver_id, profile, flag_config(),
            )

            # Assignment is Jobber area -> Master Route, NEVER truck GPS. The proposal
            # is shown on the client before this session receives an authoritative route.
            proposed = assignment.resolve_assignment(driver_name, req.truck, routes.pacific_date())
            proposed_route_id = proposed.get("route_cluster_id")
            live = (routes.resolve_live_route(cur, req.truck, route_cluster_id=proposed_route_id)
                    if proposed_route_id is not None else None)
            next_stop = live["next_stop"] if live else None
            phase = live["phase"] if live else DRIVING

            payload = build_session_payload(
                driver_id=driver_id,
                driver_name=driver_name,
                truck_no=req.truck,
                flags=flags,
                next_stop=next_stop,
                route_cluster_id=proposed_route_id,
                phase=phase,
                events=today_events,
                events_status=events_status,
            )
            # Operational assignment fields are byte-identical for both modes.  A later
            # friendly label belongs in assignment._candidate_objects, not this client seam.
            # jobber_visits is today's Jobber locations ([] when Jobber is empty or down).
            # Optional candidate area/cities/stops/match_score/likely ride on the same object.
            if not isinstance(proposed.get("jobber_visits"), list):
                proposed["jobber_visits"] = []
            payload["assignment"] = proposed

            # persist the session row (coached/arrival_voice are INTERNAL audit only,
            # never shipped — the payload above carries no boolean).
            import json as _json

            cur.execute(
                """
                INSERT INTO driver_coach_session
                    (session_id, truck_no, driver_id, coached, arrival_voice,
                     route_cluster_id, started_at, flag_reason)
                VALUES (%s,%s,%s,%s,%s,%s, now(), %s)
                """,
                (
                    session_id,
                    req.truck,
                    driver_id,
                    flags.coached,
                    flags.arrival_voice,
                    None,  # persists ONLY after the driver explicitly confirms
                    _json.dumps(flags.reason),
                ),
            )
        conn.commit()

    # The wire response: session_id + the disguise-safe payload. No boolean leaks.
    return {"session_id": session_id, **payload}


def do_confirm_assignment(req: ConfirmAssignmentRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    """Persist the driver's chosen route and return its authoritative live snapshot."""
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT truck_no, coached, driver_id FROM driver_coach_session WHERE session_id=%s FOR UPDATE",
                (req.session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown session_id: {req.session_id}")
            truck_no, coached, driver_id = row
            # Capture a concrete ordered plan ONCE. Later GPS polls read this immutable
            # session snapshot, never selecting another cluster or nearest later stop.
            frozen_plan = routes.ordered_stops_for_route(cur, req.route_cluster_id, routes.dow_name())
            if not frozen_plan:
                raise ValueError("route is unavailable for today")
            try:
                driver_name = jobber.name_from_driver_id(driver_id) or ""
            except Exception:
                driver_name = ""
            today_events, advise_events, events_status = _todays_bookings(driver_name)
            cur.execute(
                "UPDATE driver_coach_session SET route_cluster_id=%s, plan_snapshot=%s::jsonb, "
                "served_stop_orders='[]'::jsonb, skipped_stop_orders='[]'::jsonb WHERE session_id=%s",
                (req.route_cluster_id, json.dumps(frozen_plan, default=str), req.session_id),
            )
            fresh = empty_progress()
            live = routes.resolve_live_route(
                cur, truck_no, route_cluster_id=req.route_cluster_id,
                frozen_plan=frozen_plan, events=advise_events,
                session_id=req.session_id, schema=schema, reset_chooser=True,
                driver_progress=fresh,
            )
            payload = build_route_payload(
                next_stop=live["next_stop"], route_cluster_id=live["route_cluster_id"],
                phase=live["phase"], coached=bool(coached), shift_phase=live["shift_phase"],
                route_count=live["route_count"], up_next=live.get("up_next"),
                map_nav=live.get("map"), mapbox_token=mapbox_token() or None,
                events=_with_leave_by(today_events, live.get("next_stop")),
                events_status=events_status,
                turns=live.get("turns"),
                plan_source=live.get("plan_source"),
                ticker=scorecard.live_ticker(jobber.name_from_driver_id(driver_id) or ""),
                **_cursor_payload(frozen_plan, set(), set(), fresh),
            )
        conn.commit()
    return {"session_id": req.session_id, **payload}


def _orders(raw) -> set[int]:
    values = raw if isinstance(raw, list) else json.loads(raw or "[]")
    return {
        int(v) for v in values
        if isinstance(v, int) and not isinstance(v, bool)
    }


def _plan_rows(raw) -> list:
    return raw if isinstance(raw, list) else json.loads(raw or "[]")


def _advised_order(plan, served, skipped, state) -> Optional[int]:
    """Chooser current stop. A school/event latch still wins when the driver has no target."""
    from .advisor import advise_plan
    current = advise_plan(
        plan, served_orders=served, skipped_orders=skipped,
        now_minutes=datetime.datetime.now().hour * 60 + datetime.datetime.now().minute,
    )["next_stop"]
    if school_slack.enabled() and isinstance(state, dict):
        latched = next((
            s for s in plan
            if isinstance(s, dict) and school_slack.identity(s) == state.get("choice")
            and s.get("stop_order") not in served | skipped
        ), None)
        if latched is not None:
            current = latched
        elif state.get("choice"):
            current = None
    if not isinstance(current, dict):
        return None
    order = current.get("stop_order")
    if isinstance(order, bool) or not isinstance(order, int):
        return None
    return order


def _current_outcome_order(plan, served, skipped, state, progress) -> Optional[int]:
    """The only stop Done/Skip may write.

    An explicit driver target is that stop. Otherwise the advised/latched stop
    is, which keeps the school-before-filler rule when the driver has not jumped.
    """
    target = progress.get("targetOrder") if isinstance(progress, dict) else None
    if plan_stop(plan, target) is not None:
        return target
    return _advised_order(plan, served, skipped, state)


def do_stop_outcome(req: StopOutcomeRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    """Persist a deliberate Done/Skip of the current stop. The plan snapshot stays.

    The route poll, office estimate, Jump, and Back do not call this. Jump and
    Back use set-target, which does not mark the stops in between.
    """
    if req.outcome not in {"done", "skip"}:
        raise ValueError("outcome must be done or skip")
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT plan_snapshot, served_stop_orders, skipped_stop_orders "
                "FROM driver_coach_session WHERE session_id=%s FOR UPDATE",
                (req.session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown session_id: {req.session_id}")
            plan_raw, served_raw, skipped_raw = row
            plan = _plan_rows(plan_raw)
            served = _orders(served_raw)
            skipped = _orders(skipped_raw)
            from .decision_store import append, load
            state = load(cur, req.session_id, schema)
            progress = from_state(state)
            if req.expected_revision is not None:
                require_revision(progress, req.expected_revision)
            current_order = _current_outcome_order(plan, served, skipped, state, progress)
            if current_order is None or req.stop_order != current_order:
                raise ValueError("outcome must target the advised current planned stop")
            if req.outcome == "done":
                served.add(req.stop_order)
                skipped.discard(req.stop_order)
            else:
                skipped.add(req.stop_order)
                served.discard(req.stop_order)
            cur.execute(
                "UPDATE driver_coach_session SET served_stop_orders=%s::jsonb, "
                "skipped_stop_orders=%s::jsonb WHERE session_id=%s",
                (json.dumps(sorted(served)), json.dumps(sorted(skipped)), req.session_id),
            )
            target = progress.get("targetOrder")
            if target == req.stop_order:
                target = None
            stored = dict(state) if isinstance(state, dict) else {}
            stored["driverProgress"] = bump(progress, target)
            append(cur, req.session_id, schema, {
                "audit": {"kind": "stop-outcome", "stopOrder": req.stop_order, "outcome": req.outcome},
                "state": stored,
            })
        conn.commit()
    return {
        "session_id": req.session_id,
        "outcome": req.outcome,
        "stop_order": req.stop_order,
        "progressRevision": stored["driverProgress"]["revision"],
    }


def do_heartbeat(req: HeartbeatRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    # iPad GPS in the batch is liveness evidence only. Route progress reads Geotab
    # for the truck and never this payload, so an office iPad cannot move the cab.
    batch = [hb.model_dump() for hb in req.batch]
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            ack_seq = liveness.ingest_batch(cur, req.session_id, batch)
        conn.commit()
    # Mirrors the client transport contract: { ack_seq }.
    return {"ack_seq": ack_seq}


def do_roster(schema: str = DEFAULT_SCHEMA) -> dict:
    """The truck->driver roster the sign-in dropdowns render (READ-ONLY).

    Reads the real active trucks + each truck's driver-of-record and formats the
    display names through the single ROSTER_NAME_FORMAT knob. No write, no session.
    """
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            body = roster.roster_response(cur)
        # no writes — nothing to commit, but keep the same connect() contract.
        conn.rollback()
    return body


def refuel_reminder_for_route_poll(
    truck_no: int,
    *,
    now: Optional[datetime.datetime] = None,
    refuel_confirmed: bool = False,
) -> Optional[Reminder]:
    """Return the current poll's refuel-banner command, never an email action.

    A route poll is a state snapshot, so a still-unconfirmed truck receives the
    reminder on each eligible poll rather than only the first poll a process observes.
    The policy's 11:59 PM draft method is deliberately not called here.
    """
    if refuel_confirmed:
        return None
    current = now or datetime.datetime.now(datetime.timezone.utc)
    reminders = RefuelNightBook().due_reminders([truck_no], current)
    return reminders[0] if reminders else None


def _last_sale_wire(found: Optional[dict]) -> dict:
    """Cab-facing last Square sale. No idle-minute score.

    ``at`` is the sale time, or null. ``reason`` is null when ``at`` is set.
    Otherwise it is ``no_token``, ``no_match``, ``no_sale``, or ``unavailable``.
    The cab can start its own 7-minute parked-with-no-sale prompt from ``at``.
    """
    stamp = found.get("last_sale_at") if isinstance(found, dict) else None
    if stamp is not None:
        return {"at": no_idle.iso_z(stamp), "reason": None}
    reason = found.get("reason") if isinstance(found, dict) else None
    if not reason:
        reason = "no_sale" if isinstance(found, dict) and found.get("known") else "unavailable"
    return {"at": None, "reason": reason}


def _next_stop_label(stop: Optional[dict]) -> Optional[str]:
    if not isinstance(stop, dict):
        return None
    label = friendly_stop_name(stop.get("address"))
    if label:
        return label
    for key in ("title", "name"):
        value = stop.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def do_start_break(req: BreakStartRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    """Start the session's lunch or break and close any open idle episode."""
    now = no_idle.utcnow()
    with connect(schema=schema, autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                payload = breaks.start(cur, session_id=req.session_id, break_type=req.type, now=now)
                no_idle.close_open(cur, req.session_id, now=now, reason="break")
            conn.commit()
            return payload
        except breaks.BreakError as exc:
            if exc.commit:
                conn.commit()
            else:
                conn.rollback()
            raise


def do_end_break(req: BreakEndRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    now = no_idle.utcnow()
    with connect(schema=schema, autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                payload = breaks.end(cur, session_id=req.session_id, now=now)
            conn.commit()
            return payload
        except breaks.BreakError as exc:
            if exc.commit:
                conn.commit()
            else:
                conn.rollback()
            raise


def do_start_square_offline(req: SquareOfflineRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    """Mark Square offline and close any open idle episode for this session."""
    now = no_idle.utcnow()
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            payload = square_offline.start(cur, session_id=req.session_id, now=now)
            no_idle.close_open(cur, req.session_id, now=now, reason="square_offline")
        conn.commit()
        return payload


def do_end_square_offline(req: SquareOfflineRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    now = no_idle.utcnow()
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            payload = square_offline.end(cur, session_id=req.session_id, now=now)
        conn.commit()
        return payload


def do_idle_exclusions(day: Optional[str] = None, schema: str = DEFAULT_SCHEMA) -> dict:
    """Hermes read. No driver session and no writes.

    Same access as GET /roster: the service has no office credential, so this
    is an unauthenticated read. A hold past its cap is shown as expired from
    the clock. The stored row is left unchanged; the cab poll, start, and end
    paths close it. This does not create a session or write a stop outcome.
    """
    from .holds import pacific_day, parse_day

    now = no_idle.utcnow()
    day_value = pacific_day(now) if day is None else parse_day(day)
    with connect(schema=schema, readonly=True) as conn:
        with conn.cursor() as cur:
            body = idle_status.collect(cur, day_value, now)
        conn.rollback()
    return body


def session_outcome_snapshot(served_orders, skipped_orders) -> dict:
    """The session's stored stop-order arrays, without inventing who wrote each one.

    Explicit Done/Skip and any older Geotab inference share these columns. This
    poll does not rewrite them and cannot tell those rows apart, so the snapshot
    is source ``session`` with ``legacyProvenanceUnknown`` true. It is not a
    claim that every listed order was driver-confirmed.
    """
    return {
        "source": "session",
        "legacyProvenanceUnknown": True,
        "servedStopOrders": sorted(served_orders or ()),
        "skippedStopOrders": sorted(skipped_orders or ()),
    }


def remaining_master_orders(plan, served_orders, skipped_orders) -> list[int]:
    """Unfinished frozen-plan orders in snapshot order.

    Orders present in the stored served or skipped columns are removed. Those
    columns are not proof of a driver confirmation. Proximity is not an input.
    """
    done = set(served_orders or ()) | set(skipped_orders or ())
    orders = []
    seen = set()
    for stop in plan or []:
        if not isinstance(stop, dict):
            continue
        order = stop.get("stop_order")
        if isinstance(order, int) and order not in done and order not in seen:
            seen.add(order)
            orders.append(order)
    return orders


def _cursor_payload(plan, served, skipped, progress) -> dict:
    return {
        "session_outcomes": session_outcome_snapshot(served, skipped),
        "remaining_stop_orders": remaining_master_orders(plan, served, skipped),
        "plan_stops": build_plan_stops(plan, served, skipped),
        "progress_revision": progress["revision"],
        "driver_target": progress.get("targetOrder"),
    }


def _conflict_body(exc: ProgressConflict) -> dict:
    target = exc.target_order
    return {
        "error": "progress_revision_conflict",
        "progressRevision": exc.revision,
        "driverTarget": (
            {"stopOrder": target, "source": "driver"}
            if isinstance(target, int) and not isinstance(target, bool) else None
        ),
    }


def _apply_target(live, plan, progress):
    """Show the explicit driver pin without dropping the other uploaded pins."""
    if not isinstance(live, dict) or not isinstance(progress, dict):
        return live
    pinned = plan_stop(plan, progress.get("targetOrder"))
    if pinned is None:
        return live
    live = dict(live)
    live["next_stop"] = dict(pinned)
    nav = live.get("map")
    if isinstance(nav, dict) and isinstance(nav.get("stops"), list):
        order = pinned.get("stop_order")
        rest = [stop for stop in nav["stops"] if not isinstance(stop, dict) or stop.get("order") != order]
        pin = {"order": order, "name": pinned.get("address") or "Stop"}
        lat, lng = pinned.get("lat"), pinned.get("lng")
        if (
            isinstance(lat, (int, float)) and not isinstance(lat, bool)
            and isinstance(lng, (int, float)) and not isinstance(lng, bool)
        ):
            pin["lat"] = float(lat)
            pin["lng"] = float(lng)
        nav = dict(nav)
        nav["stops"] = [pin] + rest
        live["map"] = nav
    return live


def _cursor_response(
    cur, schema, *, session_id, truck_no, coached, route_cluster_id, driver_id,
    plan, served, skipped, progress,
) -> dict:
    """Poll-shaped body after an explicit cursor change. Does not write outcomes."""
    driver_name = ""
    try:
        driver_name = jobber.name_from_driver_id(driver_id) or ""
    except Exception:
        driver_name = ""
    today_events, advise_events, events_status = _todays_bookings(driver_name)
    live = routes.resolve_live_route(
        cur, truck_no, route_cluster_id=route_cluster_id, frozen_plan=plan,
        served_orders=served, skipped_orders=skipped, events=advise_events,
        session_id=session_id, schema=schema, driver_progress=progress,
    )
    live = _apply_target(live, plan, progress)
    from .adherence import adherence_estimate, empty_adherence
    nav = live.get("map") if isinstance(live.get("map"), dict) else {}
    truck = nav.get("truck") if isinstance(nav.get("truck"), dict) else {}
    fix = live.get("position") if isinstance(live.get("position"), dict) else {}
    try:
        adherence = adherence_estimate(
            plan, live.get("driven_stops"),
            now_lat=truck.get("lat", fix.get("lat")), now_lng=truck.get("lng", fix.get("lng")),
        )
    except Exception:
        adherence = empty_adherence()
    payload = build_route_payload(
        next_stop=live["next_stop"], route_cluster_id=live["route_cluster_id"],
        phase=live["phase"], coached=bool(coached), shift_phase=live["shift_phase"],
        route_count=live["route_count"], up_next=live.get("up_next"),
        map_nav=live.get("map"), mapbox_token=mapbox_token() or None,
        events=_with_leave_by(today_events, live.get("next_stop")),
        events_status=events_status, turns=live.get("turns"),
        plan_source=live.get("plan_source"),
        ticker=scorecard.live_ticker(driver_name or ""),
        refuel_reminder=refuel_reminder_for_route_poll(truck_no),
        geotab_adherence=adherence,
        **_cursor_payload(plan, served, skipped, progress),
    )
    return {"session_id": session_id, **payload}


def _lock_cursor(cur, session_id):
    cur.execute(
        "SELECT truck_no, coached, route_cluster_id, driver_id, plan_snapshot, "
        "served_stop_orders, skipped_stop_orders FROM driver_coach_session "
        "WHERE session_id = %s FOR UPDATE",
        (session_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown session_id: {session_id}")
    truck_no, coached, route_cluster_id, driver_id, plan_raw, served_raw, skipped_raw = row
    if route_cluster_id is None:
        raise ValueError("route confirmation required")
    plan = _plan_rows(plan_raw)
    if not plan:
        raise ValueError("confirmed plan is empty")
    return truck_no, coached, route_cluster_id, driver_id, plan, _orders(served_raw), _orders(skipped_raw)


def do_start_over(req: StartOverRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    """Clear stored outcomes and aim at the first uploaded pin.

    The frozen plan_snapshot is not read again and is not replaced. The school
    chooser latch is left in place. A stale expected_revision does not clear
    outcomes written after the client's last poll. Office reads must not call this.
    """
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            truck_no, coached, route_cluster_id, driver_id, plan, served, skipped = _lock_cursor(cur, req.session_id)
            from .decision_store import load
            progress = from_state(load(cur, req.session_id, schema))
            require_revision(progress, req.expected_revision)
            target = first_stop_order(plan)
            if target is None:
                raise ValueError("confirmed plan has no stops")
            cur.execute(
                "UPDATE driver_coach_session SET served_stop_orders='[]'::jsonb, "
                "skipped_stop_orders='[]'::jsonb WHERE session_id=%s",
                (req.session_id,),
            )
            progress = bump(progress, target)
            body = _cursor_response(
                cur, schema, session_id=req.session_id, truck_no=truck_no, coached=coached,
                route_cluster_id=route_cluster_id, driver_id=driver_id, plan=plan,
                served=set(), skipped=set(), progress=progress,
            )
        conn.commit()
    return body


def do_set_target(req: SetTargetRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    """Point the cab at one uploaded pin. No other stop is marked done or skipped."""
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            truck_no, coached, route_cluster_id, driver_id, plan, served, skipped = _lock_cursor(cur, req.session_id)
            if plan_stop(plan, req.stop_order) is None:
                raise ValueError("target must be a stop on the confirmed plan")
            from .decision_store import load
            progress = from_state(load(cur, req.session_id, schema))
            require_revision(progress, req.expected_revision)
            progress = bump(progress, req.stop_order)
            body = _cursor_response(
                cur, schema, session_id=req.session_id, truck_no=truck_no, coached=coached,
                route_cluster_id=route_cluster_id, driver_id=driver_id, plan=plan,
                served=served, skipped=skipped, progress=progress,
            )
        conn.commit()
    return body


def _driver_scorecard(driver_id: Optional[str], schema: str) -> Optional[dict]:
    """Today's Hermes totals for this driver. Outside the session row lock.

    Missing table or no posted row returns None so the poll omits ``scorecard``.
    """
    if not isinstance(driver_id, str) or not driver_id.strip():
        return None
    from .holds import pacific_day

    day = pacific_day(no_idle.utcnow())
    try:
        with connect(schema=schema, readonly=True) as conn:
            with conn.cursor() as cur:
                found = idle_scorecard.load_today(cur, driver_id, day)
            conn.rollback()
        return found
    except psycopg2.errors.UndefinedTable:
        _note_missing_scorecard_table()
        return None


def _session_truck_driver(session_id: str, schema: str) -> tuple:
    """Read the session identity without taking the row lock."""
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT truck_no, driver_id FROM driver_coach_session WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
        conn.rollback()
    if row is None:
        raise KeyError(f"unknown session_id: {session_id}")
    return row[0], row[1]


def do_route(session_id: str, schema: str = DEFAULT_SCHEMA) -> dict:
    """The live route state for a session (the ~15-20s poll).

    Looks up the session's truck + coached flag (driver_coach_session) and ships the
    disguise-safe current state: route.nextStop from confirmed outcomes only, live
    Geotab motion phase, §11 shiftPhase, routeCount and — coached only, inside the
    coach bundle — the updated grade.

    This poll does not write served_stop_orders or skipped_stop_orders. A credible
    parked completion is an explicit stop-outcome from the cab app, same as Done/Skip.
    Geotab still places the truck and is copied into geotabAdherence for a later
    read-only office view; that object does not move the cab cursor.
    """
    wrote_shift = False
    # Square is a network call. Resolve it before the session row lock so a
    # slow payment lookup cannot block other writers on this session.
    truck_hint, driver_hint = _session_truck_driver(session_id, schema)
    driver_name_for_events = ""
    try:
        driver_name_for_events = jobber.name_from_driver_id(driver_hint) or ""
    except Exception:
        driver_name_for_events = ""
    sale = no_idle._lookup_sale(int(truck_hint), driver_name_for_events, no_idle.utcnow())
    driver_scorecard = _driver_scorecard(driver_hint, schema)
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT truck_no, coached, route_cluster_id, driver_id, plan_snapshot, "
                "served_stop_orders, skipped_stop_orders FROM driver_coach_session WHERE session_id = %s FOR UPDATE",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown session_id: {session_id}")
            truck_no, coached, route_cluster_id, driver_id, plan_snapshot, served_raw, skipped_raw = row
            # psycopg returns jsonb as a native list in normal operation; tolerate a
            # string in lightweight test adapters without ever reconstructing a route.
            frozen_plan = plan_snapshot if isinstance(plan_snapshot, list) else json.loads(plan_snapshot or "[]")
            served_orders = {int(v) for v in (served_raw if isinstance(served_raw, list) else json.loads(served_raw or "[]"))}
            skipped_orders = {int(v) for v in (skipped_raw if isinstance(skipped_raw, list) else json.loads(skipped_raw or "[]"))}
            if route_cluster_id is None:
                # Never recreate the old truck/GPS route guess while an explicit
                # driver confirmation is pending.
                raise ValueError("route confirmation required")
            # Today's ONE_OFF Jobber events for THIS driver (operational, both modes
            # identical). The session row persists only driver_id, so recover a
            # comparable name from it (jobber.name_from_driver_id) for the events
            # match. Cached ~5 min, so the per-poll cost is bounded. get_today_events
            # ALWAYS returns a list ([] on a genuine empty day AND on any failure — the
            # locked "-> [] (panel hidden)" contract) and never raises; the try/except is
            # belt-and-braces so a Jobber hiccup never crashes the poll. A crash here is a
            # failure -> [] (clear the panel per the contract), never None.
            today_events, advise_events, events_status = _todays_bookings(driver_name_for_events)
            from .decision_store import load as load_progress
            # A database that has not applied the chooser table still serves the
            # route. There is no explicit driver target to restore.
            cur.execute("SAVEPOINT driver_progress_load")
            try:
                progress = from_state(load_progress(cur, session_id, schema))
                cur.execute("RELEASE SAVEPOINT driver_progress_load")
            except psycopg2.errors.UndefinedTable:
                cur.execute("ROLLBACK TO SAVEPOINT driver_progress_load")
                progress = empty_progress()
            live = routes.resolve_live_route(
                cur, truck_no, route_cluster_id=route_cluster_id, frozen_plan=frozen_plan,
                served_orders=served_orders, skipped_orders=skipped_orders, events=advise_events,
                session_id=session_id, schema=schema, driver_progress=progress,
            )
            live = _apply_target(live, frozen_plan, progress)
            # Geotab still tracks the truck. Park phase, gear-126 history, and a
            # pass-by near a later pin are an office estimate only — never folded
            # into served/skipped, and never used to rebuild the cab cursor.
            from .adherence import adherence_estimate, empty_adherence
            nav = live.get("map") if isinstance(live.get("map"), dict) else {}
            truck = nav.get("truck") if isinstance(nav.get("truck"), dict) else {}
            fix = live.get("position") if isinstance(live.get("position"), dict) else {}
            try:
                adherence = adherence_estimate(
                    frozen_plan,
                    live.get("driven_stops"),
                    now_lat=truck.get("lat", fix.get("lat")),
                    now_lng=truck.get("lng", fix.get("lng")),
                )
            except Exception:
                adherence = empty_adherence()
            position = live.get("position") if isinstance(live, dict) else None
            plan = live.get("match_plan") if isinstance(live, dict) else None
            if not plan:
                plan = frozen_plan
            view = no_idle.motion_view(
                position,
                phase=live.get("phase") if isinstance(live, dict) else None,
                parked_stop=live.get("parked_stop") if isinstance(live, dict) else None,
                plan=plan,
            )
            nudge = None
            break_state = breaks.inactive_state()
            square_state = square_offline.inactive_state()
            suggestions: list = []
            # A missing migration must not take down navigation. Roll back only this
            # savepoint; the route snapshot above still commits with the session.
            cur.execute("SAVEPOINT driver_coach_no_idle")
            try:
                break_state, break_wrote = breaks.reconcile_on_poll(
                    cur,
                    session_id=session_id,
                    truck_no=int(truck_no),
                    driver_id=driver_id,
                    fresh=view.fresh,
                    moving=view.moving,
                )
                square_state = square_offline.inactive_state()
                square_wrote = False
                cur.execute("SAVEPOINT driver_coach_square_offline")
                try:
                    square_state, square_wrote = square_offline.reconcile_on_poll(
                        cur, session_id=session_id, driver_id=driver_id,
                    )
                    cur.execute("RELEASE SAVEPOINT driver_coach_square_offline")
                except psycopg2.errors.UndefinedTable:
                    _note_missing_square_table()
                    cur.execute("ROLLBACK TO SAVEPOINT driver_coach_square_offline")
                    square_state = square_offline.inactive_state()
                    square_wrote = False
                except psycopg2.Error:
                    _log.exception("square offline reconcile skipped")
                    cur.execute("ROLLBACK TO SAVEPOINT driver_coach_square_offline")
                    square_state = square_offline.inactive_state()
                    square_wrote = False
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT driver_coach_square_offline")
                    square_state = square_offline.inactive_state()
                    square_wrote = False
                nudge, idle_wrote = no_idle.reconcile_on_poll(
                    cur,
                    session_id=session_id,
                    truck_no=int(truck_no),
                    driver_id=driver_id,
                    driver_name=driver_name_for_events,
                    view=view,
                    break_active=bool(break_state.get("active")),
                    square_offline_active=bool(square_state.get("active")),
                    next_stop_name=_next_stop_label(live.get("next_stop") if isinstance(live, dict) else None),
                    sale=sale,
                )
                target = progress.get("targetOrder") if isinstance(progress, dict) else None
                if isinstance(target, bool) or not isinstance(target, int):
                    target = None
                suggestions = nudge_suggestions.for_poll(
                    cur,
                    session_id=session_id,
                    route_cluster_id=route_cluster_id,
                    frozen_plan=frozen_plan,
                    served_orders=served_orders,
                    skipped_orders=skipped_orders,
                    current_order=target,
                    now=no_idle.utcnow(),
                )
                wrote_shift = bool(break_wrote or square_wrote or idle_wrote)
                cur.execute("RELEASE SAVEPOINT driver_coach_no_idle")
            except psycopg2.errors.UndefinedTable:
                cur.execute("ROLLBACK TO SAVEPOINT driver_coach_no_idle")
                nudge = None
                break_state = breaks.inactive_state()
                square_state = square_offline.inactive_state()
                suggestions = []
                wrote_shift = False
            payload = build_route_payload(
                next_stop=live["next_stop"],
                route_cluster_id=live["route_cluster_id"],
                phase=live["phase"],
                coached=bool(coached),
                shift_phase=live["shift_phase"],
                route_count=live["route_count"],
                up_next=live.get("up_next"),
                # LIVE MAP nav (truck + road-following line + stop pins) — pure
                # navigation, byte-identical both modes; None -> client simple view.
                map_nav=live.get("map"),
                # PUBLIC (pk.) Mapbox token for the client's map — safe to expose.
                mapbox_token=mapbox_token() or None,
                # Today's ONE_OFF Jobber events (operational, both modes identical).
                events=_with_leave_by(today_events, live.get("next_stop")),
                events_status=events_status,
                turns=live.get("turns"),
                plan_source=live.get("plan_source"),
                ticker=scorecard.live_ticker(driver_name_for_events or ""),
                refuel_reminder=refuel_reminder_for_route_poll(truck_no),
                nudge=nudge,
                break_state=break_state,
                square_offline=square_state,
                nudge_suggestions=suggestions,
                last_sale=_last_sale_wire(sale),
                driver_scorecard=driver_scorecard,
                geotab_adherence=adherence,
                **_cursor_payload(frozen_plan, served_orders, skipped_orders, progress),
            )
        # Confirmed stop outcomes are not written here. A no-idle/break transition
        # or school-slack decision audit may still commit; those are not outcomes.
        if school_slack.enabled() or wrote_shift:
            conn.commit()
        else:
            conn.rollback()
    return {"session_id": session_id, **payload}


# --------------------------------------------------------------------------- #
# Routes.
# --------------------------------------------------------------------------- #
@app.get("/driver-coach/v1/health")
def health() -> dict:
    return {"ok": True, "service": "driver-coach", "version": "1.0.0"}


@app.post("/driver-coach/v1/signin")
def signin(req: SignInRequest) -> dict:
    try:
        return do_signin(req)
    except ValueError as e:
        # unknown/absent driver_id (or legacy name) -> clean 400, not a 500.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # pragma: no cover - surfaced as 500 in prod
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/driver-coach/v1/assignment/confirm")
def confirm_assignment(req: ConfirmAssignmentRequest) -> dict:
    try:
        return do_confirm_assignment(req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/driver-coach/v1/stop-outcome")
def stop_outcome(req: StopOutcomeRequest) -> dict:
    try:
        return do_stop_outcome(req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ProgressConflict as e:
        raise HTTPException(status_code=409, detail=_conflict_body(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/driver-coach/v1/route/start-over")
def start_over(req: StartOverRequest) -> dict:
    try:
        return do_start_over(req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ProgressConflict as e:
        raise HTTPException(status_code=409, detail=_conflict_body(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/driver-coach/v1/route/target")
def set_target(req: SetTargetRequest) -> dict:
    try:
        return do_set_target(req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ProgressConflict as e:
        raise HTTPException(status_code=409, detail=_conflict_body(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/driver-coach/v1/heartbeat")
def heartbeat(req: HeartbeatRequest) -> dict:
    try:
        return do_heartbeat(req)
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/driver-coach/v1/roster")
def get_roster() -> dict:
    try:
        return do_roster()
    except Exception as e:  # pragma: no cover - surfaced as 500 in prod
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/driver-coach/v1/break/start")
def start_break(req: BreakStartRequest) -> dict:
    try:
        return do_start_break(req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except breaks.BreakError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/driver-coach/v1/break/end")
def end_break(req: BreakEndRequest) -> dict:
    try:
        return do_end_break(req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except breaks.BreakError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/driver-coach/v1/square-offline/start")
def start_square_offline(req: SquareOfflineRequest) -> dict:
    try:
        return do_start_square_offline(req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except square_offline.SquareOfflineError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    except psycopg2.errors.UndefinedTable:
        raise HTTPException(status_code=503, detail="square offline storage is not migrated")
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/driver-coach/v1/square-offline/end")
def end_square_offline(req: SquareOfflineRequest) -> dict:
    try:
        return do_end_square_offline(req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except square_offline.SquareOfflineError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    except psycopg2.errors.UndefinedTable:
        raise HTTPException(status_code=503, detail="square offline storage is not migrated")
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/driver-coach/v1/idle-exclusions")
def get_idle_exclusions(
    day: Optional[str] = None,
    x_dc_office_key: Optional[str] = Header(default=None, alias=office_key.HEADER_NAME),
) -> dict:
    """Office/Hermes read. No driver session.

    When ``DC_OFFICE_KEY`` is set, ``X-DC-Office-Key`` must match. When it is
    unset, the read stays open so a deploy before the key exists still works.
    ``day`` is an optional Pacific YYYY-MM-DD. Omitted means today.
    """
    office_key.require_office_read(x_dc_office_key)
    try:
        return do_idle_exclusions(day)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/driver-coach/v1/idle-scorecard")
async def post_idle_scorecard(
    request: Request,
    x_dc_office_key: Optional[str] = Header(default=None, alias=office_key.HEADER_NAME),
) -> dict:
    """Hermes posts idle totals. Requires DC_OFFICE_KEY; 503 when that env is unset."""
    office_key.require_office_ingest(x_dc_office_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "bad_body", "message": "JSON object required"})
    try:
        day, as_of, rows = idle_scorecard.parse_ingest(body)
    except idle_scorecard.ScorecardError as exc:
        raise HTTPException(status_code=400, detail=exc.detail)
    try:
        with connect(autocommit=False) as conn:
            with conn.cursor() as cur:
                count = idle_scorecard.upsert(cur, day, as_of, rows)
            conn.commit()
    except psycopg2.errors.UndefinedTable:
        raise HTTPException(status_code=503, detail="idle scorecard storage is not migrated")
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))
    return {"day": day, "upserted": count}


@app.get("/driver-coach/v1/route")
def get_route(session_id: str) -> dict:
    try:
        return do_route(session_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))

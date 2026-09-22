"""Driver Coach v1 service — FastAPI. Endpoints replacing the shipped app's mock
seams + the production-ization roster/route feeds:

  POST /driver-coach/v1/signin     replaces app.js mockSignInPayload()
  POST /driver-coach/v1/heartbeat  replaces app.js startLiveness() transport ACK
  GET  /driver-coach/v1/roster     the REAL truck→driver roster (replaces app.js
                                   hardcoded NAMES/TRUCKS)
  GET  /driver-coach/v1/route      the LIVE route snapshot the app polls to follow
                                   the truck's real progress (nextStop + live phase)

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
grade. READ-ONLY.

HARD CONSTRAINT (Felix gate): this targets DRIVER_COACH_SCHEMA (default
'driver_coach'), NEVER public. It is BUILD + TEST ONLY — not deployed, the live
iPad endpoint is unchanged. Deploy is a separate Felix-gated step (see render.yaml,
which is the SHAPE only and is never executed here).

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import datetime
import json
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import assignment, flagstate, geotab, jobber, liveness, profiles, roster, routes
from . import events as events_mod
from .config import flag_config, mapbox_token
from .db import DEFAULT_SCHEMA, connect
from .motion import phase_for_fix
from .payload import build_route_payload, build_session_payload
from .refcore import DRIVING, resolve_flags

app = FastAPI(title="Driver Coach v1", version="1.0.0")


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
    """A deliberate driver outcome. Only the current frozen-plan stop may change."""
    session_id: str
    stop_order: int
    outcome: str


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
    try:
        today_events = events_mod.get_today_events(square_name)
    except Exception:
        today_events = []

    session_id = str(uuid.uuid4())
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            # hysteresis: load prior, resolve, persist next_state (frozen for the day).
            prior = flagstate.load_prior(cur, driver_id)
            flags = resolve_flags(profile, prior, flag_config())
            flagstate.save_state(cur, driver_id, flags.next_state)

            # Assignment is Jobber area -> Master Route, NEVER truck GPS. The proposal
            # is shown on the client before this session receives an authoritative route.
            proposed = assignment.resolve_assignment(driver_name, req.truck, datetime.date.today())
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
            )
            # Operational assignment fields are byte-identical for both modes.  A later
            # friendly label belongs in assignment._candidate_objects, not this client seam.
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
                "SELECT truck_no, coached, driver_id FROM driver_coach_session WHERE session_id=%s",
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
                today_events = events_mod.get_today_events(driver_name)
            except Exception:
                today_events = []
            cur.execute(
                "UPDATE driver_coach_session SET route_cluster_id=%s, plan_snapshot=%s::jsonb, "
                "served_stop_orders='[]'::jsonb, skipped_stop_orders='[]'::jsonb WHERE session_id=%s",
                (req.route_cluster_id, json.dumps(frozen_plan, default=str), req.session_id),
            )
            live = routes.resolve_live_route(cur, truck_no, route_cluster_id=req.route_cluster_id,
                                             frozen_plan=frozen_plan, events=today_events)
            payload = build_route_payload(
                next_stop=live["next_stop"], route_cluster_id=live["route_cluster_id"],
                phase=live["phase"], coached=bool(coached), shift_phase=live["shift_phase"],
                route_count=live["route_count"], up_next=live.get("up_next"),
                map_nav=live.get("map"), mapbox_token=mapbox_token() or None,
                turns=live.get("turns"),
            )
        conn.commit()
    return {"session_id": req.session_id, **payload}


def do_stop_outcome(req: StopOutcomeRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    """Persist a driver-confirmed Done/Skip outcome without altering the frozen plan."""
    if req.outcome not in {"done", "skip"}:
        raise ValueError("outcome must be done or skip")
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT plan_snapshot, served_stop_orders, skipped_stop_orders FROM driver_coach_session WHERE session_id=%s", (req.session_id,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown session_id: {req.session_id}")
            plan_raw, served_raw, skipped_raw = row
            plan = plan_raw if isinstance(plan_raw, list) else json.loads(plan_raw or "[]")
            served = {int(v) for v in (served_raw if isinstance(served_raw, list) else json.loads(served_raw or "[]"))}
            skipped = {int(v) for v in (skipped_raw if isinstance(skipped_raw, list) else json.loads(skipped_raw or "[]"))}
            from .advisor import advise_plan
            current = advise_plan(plan, served_orders=served, skipped_orders=skipped,
                                  now_minutes=datetime.datetime.now().hour * 60 + datetime.datetime.now().minute)["next_stop"]
            if not current or current.get("stop_order") != req.stop_order:
                raise ValueError("outcome must target the advised current planned stop")
            (served if req.outcome == "done" else skipped).add(req.stop_order)
            cur.execute("UPDATE driver_coach_session SET served_stop_orders=%s::jsonb, skipped_stop_orders=%s::jsonb WHERE session_id=%s",
                        (json.dumps(sorted(served)), json.dumps(sorted(skipped)), req.session_id))
        conn.commit()
    return {"session_id": req.session_id, "outcome": req.outcome, "stop_order": req.stop_order}


def do_heartbeat(req: HeartbeatRequest, schema: str = DEFAULT_SCHEMA) -> dict:
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


def do_route(session_id: str, schema: str = DEFAULT_SCHEMA) -> dict:
    """The live route state for a session (the ~15-20s poll). READ-ONLY.

    Looks up the session's truck + coached flag (driver_coach_session), follows the
    truck along its ordered route from the live Geotab position, and ships the
    disguise-safe current state: route.nextStop, live motion phase, §11 shiftPhase,
    routeCount and — coached only, inside the coach bundle — the updated grade.
    """
    auto_advanced = False
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT truck_no, coached, route_cluster_id, driver_id, plan_snapshot, "
                "served_stop_orders, skipped_stop_orders FROM driver_coach_session WHERE session_id = %s",
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
            try:
                driver_name_for_events = jobber.name_from_driver_id(driver_id)
                today_events = events_mod.get_today_events(driver_name_for_events or "")
            except Exception:
                today_events = []
            live = routes.resolve_live_route(
                cur, truck_no, route_cluster_id=route_cluster_id, frozen_plan=frozen_plan,
                served_orders=served_orders, skipped_orders=skipped_orders, events=today_events,
            )
            # A deliberate Done/Skip is the normal advance path, but a truck that has
            # actually dwelled at the advised frozen-plan pin must advance too. The
            # route resolver's parked phase is produced from Geotab's stop geofence +
            # settle dwell state; it is not a nearest-later-stop shortcut. Persist it
            # before rebuilding the snapshot so the next poll cannot resurrect it.
            arrived = live.get("next_stop") or {}
            arrived_order = arrived.get("stop_order")
            if (live.get("phase") == "parked" and isinstance(arrived_order, int)
                    and arrived_order not in served_orders and arrived_order not in skipped_orders):
                served_orders.add(arrived_order)
                auto_advanced = True
            # Transmission Park at a planned pin, then leaving it, is the stop.
            # The speed-based phase above almost never fires: each poll is one
            # sample with no dwell history. Gear 126 is the same signal Geotab uses.
            truck = (live.get("map") or {}).get("truck") or {}
            if truck.get("lat") is not None and live.get("driven_stops"):
                from .driven_path import park_served_orders
                for order in park_served_orders(
                    live.get("match_plan") or frozen_plan, live["driven_stops"],
                    now_lat=truck["lat"], now_lng=truck["lng"],
                ):
                    if order not in served_orders and order not in skipped_orders:
                        served_orders.add(order)
                        auto_advanced = True
            if auto_advanced:
                cur.execute(
                    "UPDATE driver_coach_session SET served_stop_orders=%s::jsonb WHERE session_id=%s",
                    (json.dumps(sorted(served_orders)), session_id),
                )
                live = routes.resolve_live_route(
                    cur, truck_no, route_cluster_id=route_cluster_id, frozen_plan=frozen_plan,
                    served_orders=served_orders, skipped_orders=skipped_orders, events=today_events,
                )
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
                events=today_events,
                turns=live.get("turns"),
            )
        # The normal poll is read-only. A confirmed physical dwell is the one allowed
        # forward-cursor transition, so commit only when that state was persisted.
        if auto_advanced:
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

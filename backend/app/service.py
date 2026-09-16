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
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import flagstate, geotab, jobber, liveness, profiles, roster, routes
from .config import flag_config
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

    session_id = str(uuid.uuid4())
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            # hysteresis: load prior, resolve, persist next_state (frozen for the day).
            prior = flagstate.load_prior(cur, driver_id)
            flags = resolve_flags(profile, prior, flag_config())
            flagstate.save_state(cur, driver_id, flags.next_state)

            # truck -> today's route -> next stop (coords for motion distance).
            resolved = routes.resolve_next_stop(cur, req.truck)
            next_stop = resolved["next_stop"]
            route_cluster_id = resolved["route_cluster_id"]

            # server-side motion phase from the latest Geotab fix (best-effort).
            phase = DRIVING
            truck_id = routes.truck_id_for(cur, req.truck)
            if truck_id and next_stop:
                fix = geotab.latest_fix(
                    cur, truck_id, next_stop.get("lat"), next_stop.get("lng")
                )
                phase = phase_for_fix(fix, prior_phase=DRIVING)

            payload = build_session_payload(
                driver_id=driver_id,
                driver_name=driver_name,
                truck_no=req.truck,
                flags=flags,
                next_stop=next_stop,
                route_cluster_id=route_cluster_id,
                phase=phase,
            )

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
                    route_cluster_id,
                    _json.dumps(flags.reason),
                ),
            )
        conn.commit()

    # The wire response: session_id + the disguise-safe payload. No boolean leaks.
    return {"session_id": session_id, **payload}


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
    with connect(schema=schema, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT truck_no, coached, route_cluster_id "
                "FROM driver_coach_session WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown session_id: {session_id}")
            truck_no, coached, route_cluster_id = row
            live = routes.resolve_live_route(cur, truck_no, route_cluster_id=route_cluster_id)
            payload = build_route_payload(
                next_stop=live["next_stop"],
                route_cluster_id=live["route_cluster_id"],
                phase=live["phase"],
                coached=bool(coached),
                shift_phase=live["shift_phase"],
                route_count=live["route_count"],
                up_next=live.get("up_next"),
            )
        # READ-ONLY: nothing to commit.
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
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))

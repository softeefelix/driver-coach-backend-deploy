"""Driver Coach v1 service — FastAPI. Two endpoints replacing the shipped app's
two mock seams:

  POST /driver-coach/v1/signin     replaces app.js mockSignInPayload()
  POST /driver-coach/v1/heartbeat  replaces app.js startLiveness() transport ACK

signin: {name, truck} -> Jobber driver-of-record -> REAL driver_profiles snapshot
-> resolve_flags() (frozen for the session) -> disguise-safe payload (no boolean on
the wire) + real next stop + server-computed motion phase. Persists PriorFlagState
+ a driver_coach_session row.

heartbeat: accepts the liveness batch the client already sends (liveness.js shape),
writes driver_coach_heartbeat idempotently, ACKs the max stored seq, and runs the
conservative liveness classify scaffold when a gap is present.

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

from . import flagstate, geotab, jobber, liveness, profiles, routes
from .config import flag_config
from .db import DEFAULT_SCHEMA, connect
from .motion import phase_for_fix
from .payload import build_session_payload
from .refcore import DRIVING, resolve_flags

app = FastAPI(title="Driver Coach v1", version="1.0.0")


# --------------------------------------------------------------------------- #
# Request models (mirror what the client already sends).
# --------------------------------------------------------------------------- #
class SignInRequest(BaseModel):
    name: str
    truck: int


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
def do_signin(req: SignInRequest, schema: str = DEFAULT_SCHEMA) -> dict:
    dor = jobber.resolve_driver_of_record(req.name)
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
                driver_name=req.name,
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
    except Exception as e:  # pragma: no cover - surfaced as 500 in prod
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/driver-coach/v1/heartbeat")
def heartbeat(req: HeartbeatRequest) -> dict:
    try:
        return do_heartbeat(req)
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))

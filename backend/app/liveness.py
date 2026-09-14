"""Liveness: persist heartbeats (idempotent on (session_id, seq)) and run the
v1-conservative classify() scaffold that writes driver_coach_liveness_event.

The heartbeat POST is also the transport for the buffered offline log (turn-off
spec §3.1): each POST carries any un-acked local events; the server ACKs the
highest seq it durably stored and the client drops ACKed events. So `ingest_batch`
persists a whole batch idempotently and returns the max stored seq as ack_seq.

classify() is v1-CONSERVATIVE by mandate (turn-off spec §3.5, §7 accusation bias):
the WRITE PATH and the table are real, but the only class ever written from a
single stale gap here is a benign/held one — CONFIRMED_OFF requires post-resume
reboot confirmation or a never-returns session proven by Geotab, which v1 does not
auto-escalate (shadow/live-review only, spec §11). So this scaffold classifies to
SHIFT_END / OFFLINE_BENIGN / SERVING_LIKELY / UNKNOWN_STALE and never accuses.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

# Classes (turn-off spec §3.5). CONFIRMED_OFF intentionally NOT reachable from the
# v1 single-gap scaffold — accusation is gated on corroboration + never auto-escalated.
SHIFT_END = "SHIFT_END"
OFFLINE_BENIGN = "OFFLINE_BENIGN"
POWER_FAULT = "POWER_FAULT"
SERVING_LIKELY = "SERVING_LIKELY"
UNKNOWN_STALE = "UNKNOWN_STALE"
SUSPECT_OFF = "SUSPECT_OFF"      # moving/healthy/no-deadzone, awaiting reboot confirmation
CONFIRMED_OFF = "CONFIRMED_OFF"  # only with reboot fingerprint + confirm window; v1 never auto-escalates


def _hb_row(session_id: str, hb: dict) -> tuple:
    gps = hb.get("gps") or {}
    battery = hb.get("battery") or {}
    net = hb.get("net") or {}
    stop_ctx = hb.get("stop_ctx") or {}
    return (
        session_id,
        int(hb["seq"]),
        hb.get("client_ts"),
        hb.get("app_state"),
        hb.get("kiosk"),
        battery.get("level"),
        battery.get("state"),
        net.get("type"),
        net.get("status"),
        hb.get("device_uptime_s"),
        hb.get("boot_id"),
        hb.get("first_launch_after_boot"),
        gps.get("lat"),
        gps.get("lng"),
        stop_ctx.get("stop_id"),
        stop_ctx.get("phase"),
    )


def ingest_batch(cur, session_id: str, batch: list) -> Optional[int]:
    """Persist a heartbeat batch idempotently. Returns the max seq durably stored
    for this session (the ack_seq the client uses to drop its buffer). Duplicate
    (session_id, seq) rows are deduped (ON CONFLICT DO NOTHING) — store-and-forward
    re-sends are safe (turn-off spec §8)."""
    if not batch:
        # Still ACK the highest seq we already have (client may just be flushing).
        cur.execute(
            "SELECT max(seq) FROM driver_coach_heartbeat WHERE session_id = %s",
            (session_id,),
        )
        r = cur.fetchone()
        return int(r[0]) if r and r[0] is not None else None

    for hb in batch:
        cur.execute(
            """
            INSERT INTO driver_coach_heartbeat
                (session_id, seq, client_ts, app_state, kiosk, battery_level,
                 battery_state, net_type, net_status, device_uptime_s, boot_id,
                 first_launch_after_boot, lat, lng, stop_id, stop_phase)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (session_id, seq) DO NOTHING
            """,
            _hb_row(session_id, hb),
        )
    cur.execute(
        "SELECT max(seq) FROM driver_coach_heartbeat WHERE session_id = %s",
        (session_id,),
    )
    r = cur.fetchone()
    return int(r[0]) if r and r[0] is not None else None


def classify_gap(
    *,
    geotab_ignition: Optional[str] = None,
    geotab_speed: Optional[float] = None,
    geotab_deadzone: bool = False,
    square_txn_age_s: Optional[int] = None,
    last_net_status: Optional[str] = None,
    last_battery_state: Optional[str] = None,
    battery_low: bool = False,
    rebooted: bool = False,
    offline_but_alive: bool = False,
    confirmed_window_held: bool = False,
    shift_end_window: bool = False,
) -> dict:
    """Cross-signal classify (turn-off spec §3.5), first-match-wins, conservative
    on accusation (§7). Returns {classification, confidence, rebooted,
    offline_but_alive, evidence}.

    The linchpin invariant (asserted in tests): NO benign input ever yields
    CONFIRMED_OFF. CONFIRMED_OFF requires ALL of: truck working (ignition ON +
    moving), a healthy battery at the last beat, NOT a dead zone, a reboot
    fingerprint on resume, AND the confirm window held — anything short is at most
    SUSPECT_OFF, which v1 does NOT auto-escalate (shadow only, spec §11).

    Rule 0 (edge §8): if the independent witness (Geotab) is ALSO dark, never
    confirm — drop to UNKNOWN_STALE.
    """
    ign = (geotab_ignition or "").upper()
    ev = {
        "geotab_ignition": geotab_ignition,
        "geotab_speed": geotab_speed,
        "geotab_deadzone": geotab_deadzone,
        "square_txn_age_s": square_txn_age_s,
        "last_net_status": last_net_status,
        "last_battery_state": last_battery_state,
        "battery_low": battery_low,
        "shift_end_window": shift_end_window,
    }

    def result(cls, conf, rule):
        ev["rule"] = rule
        return {
            "classification": cls,
            "confidence": conf,
            "rebooted": bool(rebooted),
            "offline_but_alive": bool(offline_but_alive),
            "evidence": ev,
        }

    # Rule 1: ignition off OR end-of-shift window -> benign shift end.
    if ign == "OFF" or shift_end_window:
        return result(SHIFT_END, 0.95, "ignition_off_or_shift_window")

    # Rule 0 (edge §8): independent witness dark & not a clean ignition-off ->
    # cannot corroborate, never confirm.
    if ign in ("", "UNKNOWN") or geotab_speed is None:
        return result(UNKNOWN_STALE, 0.5, "geotab_dark_no_corroboration")

    # Rule 2: known dead zone OR net trending unsatisfied -> benign offline.
    if geotab_deadzone or (last_net_status or "").lower() == "unsatisfied":
        conf = 0.97 if offline_but_alive else 0.8
        return result(OFFLINE_BENIGN, conf, "deadzone_or_unsatisfied")

    # Rule 3: unplugged + low/falling battery while truck still working -> maintenance.
    if (last_battery_state or "").lower() == "unplugged" and battery_low:
        return result(POWER_FAULT, 0.85, "unplugged_low_battery")

    # Rule 5: recent Square ring while stopped -> probably serving mid-ring.
    if (
        square_txn_age_s is not None
        and square_txn_age_s < 90
        and (geotab_speed == 0)
    ):
        return result(SERVING_LIKELY, 0.7, "recent_square_txn")

    # Rule 4: the evasion signature — truck moving, healthy battery, not a deadzone.
    healthy = (last_battery_state or "").lower() in ("charging", "full")
    if ign == "ON" and (geotab_speed or 0.0) > 0 and healthy and not geotab_deadzone:
        # CONFIRM only with the reboot fingerprint AND the confirm window held
        # (spec §3.5 confidence gate). Even then, v1 does not auto-escalate.
        if rebooted and confirmed_window_held:
            return result(CONFIRMED_OFF, 0.9, "moving_healthy_rebooted_confirmed")
        return result(SUSPECT_OFF, 0.9, "moving_healthy_awaiting_confirm")

    # Rule 6 (else): hold, wait for resume/reconcile before any flag.
    return result(UNKNOWN_STALE, 0.5, "ambiguous_hold")


def write_liveness_event(
    cur,
    *,
    session_id: str,
    truck_no: Optional[int],
    driver_id: Optional[str],
    started_at,
    ended_at,
    duration_s: Optional[int],
    result: dict,
) -> str:
    """Write one driver_coach_liveness_event row from a classify_gap result.
    Returns the new event id."""
    event_id = str(uuid.uuid4())
    ev = result.get("evidence", {})
    cur.execute(
        """
        INSERT INTO driver_coach_liveness_event
            (id, session_id, truck_no, driver_id, started_at, ended_at, duration_s,
             classification, confidence, rebooted, offline_but_alive,
             geotab_ignition, geotab_speed, geotab_deadzone, square_txn_age_s, evidence)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            event_id,
            session_id,
            truck_no,
            driver_id,
            started_at,
            ended_at,
            duration_s,
            result["classification"],
            result["confidence"],
            result["rebooted"],
            result["offline_but_alive"],
            ev.get("geotab_ignition"),
            ev.get("geotab_speed"),
            ev.get("geotab_deadzone"),
            ev.get("square_txn_age_s"),
            json.dumps(ev),
        ),
    )
    return event_id

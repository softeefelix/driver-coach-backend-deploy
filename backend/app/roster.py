"""Truck→driver roster for the sign-in dropdowns — the real active fleet.

The shipped app used a HARDCODED demo roster (12 fake names + trucks 3..26). This
module serves the REAL roster the sign-in screen renders:

  trucks  — the real truck NUMBERS of the active fleet (public.trucks joined to the
            recent route signal in public.active_sale_stops). READ-ONLY on public.
  drivers — one entry per ACTIVE driver in the latest driver_profiles snapshot, as
            { id, display }:
              id      an OPAQUE, STABLE, NON-REVERSIBLE token for the canonical name
                      (sha256(canonical)[:16]). This is what the client POSTs on
                      sign-in; the server re-derives the same token over the active
                      snapshot to resolve it back to the canonical name. The full
                      canonical name NEVER appears in the payload.
              display the label the dropdown SHOWS, formatted per the disclosure knob.

PII (do-not-leak — see build/FORGE_BRIEF_OPAQUE_ROSTER_ID.md): the test backend is a
PUBLIC url. Felix chose "first name + last initial" disclosure. So the roster payload
carries NO full/canonical name at all — only an opaque id + the formatted display.
The served DISPLAY names are produced by ONE easy-to-change function,
`format_display_name`, gated by a single env switch DRIVER_COACH_ROSTER_NAME_FORMAT:
    'full'          (default) -> the real full name (internal beta needs real names)
    'first_initial'           -> \"First L.\" (first name + last-token initial)
    'truck'                   -> the drivers list has no per-driver truck, so it
                                 falls back to the SAFE 'first_initial' label (a full
                                 name is never emitted in the drivers list).
Felipe/Felix flip that one env var; no code change decides the disclosure level. The
opaque `id` is derived from the full real name so sign-in resolution never breaks,
yet the name itself stays server-side.

READ-ONLY against public. Maker: Forge. Reviewer of record: Warden. Nothing here
deploys without Felix.
"""
from __future__ import annotations

import csv
import hashlib
import os
from typing import Optional

from . import profiles

# Single, easy-to-change disclosure switch (PII note). Unset/unknown -> 'full'.
_NAME_FORMAT_ENV = "DRIVER_COACH_ROSTER_NAME_FORMAT"
_VALID_FORMATS = ("full", "first_initial", "truck")


def name_format() -> str:
    fmt = (os.environ.get(_NAME_FORMAT_ENV) or "").strip().lower()
    return fmt if fmt in _VALID_FORMATS else "full"


def driver_id_for(canonical_name: str) -> str:
    """THE opaque, stable, NON-REVERSIBLE roster token for a canonical name.

    sha256(canonical)[:16] — deterministic (the same name always yields the same
    token, so the client's pick round-trips through POST /signin) but the token can
    NOT be reversed to the name, so the public roster wire never carries the name.
    """
    return hashlib.sha256((canonical_name or "").encode("utf-8")).hexdigest()[:16]


def format_display_name(
    canonical_name: str, truck_no: Optional[int] = None, fmt: Optional[str] = None
) -> str:
    """THE single source for how a driver's name is DISCLOSED on the roster label.

    'full'          -> the real name verbatim.
    'first_initial' -> \"First L.\" (first token + last-token initial). A single-token
                       name is returned unchanged (nothing to abbreviate).
    'truck'         -> \"Truck <n>\" — no driver PII on the label at all.
    Unknown/blank format falls back to 'full'.
    """
    fmt = fmt or name_format()
    name = (canonical_name or "").strip()
    if fmt == "truck" and truck_no is not None:
        return f"Truck {truck_no}"
    if fmt == "first_initial":
        parts = name.split()
        if len(parts) >= 2:
            return f"{parts[0]} {parts[-1][0]}."
        return name
    return name


def _active_canonicals(csv_path: Optional[str] = None) -> list[str]:
    """The ordered, de-duplicated canonical (Square employee) names of every ACTIVE
    driver in the latest metrics snapshot, sorted by name.

    This is the ONE place the active snapshot is read. Both the served roster and the
    server-side id->canonical resolver build off it, so the opaque tokens the client
    receives are always the tokens the server can resolve.
    """
    csv_path = csv_path or profiles.latest_metrics_csv()
    seen: set[str] = set()
    canonicals: list[str] = []
    with open(csv_path) as fh:
        for row in csv.DictReader(fh):
            if (row.get("status") or "").strip().upper() != "ACTIVE":
                continue
            canonical = (row.get("driver") or "").strip()
            if not canonical:
                continue
            key = canonical.lower()
            if key in seen:
                continue
            seen.add(key)
            canonicals.append(canonical)
    canonicals.sort(key=lambda c: c.lower())
    return canonicals


def _display_format() -> str:
    """The format to DISPLAY in the drivers list. The 'truck' knob has no per-driver
    truck here, so it degrades to the SAFE 'first_initial' label — the drivers list
    NEVER emits a full canonical name under 'truck' (a truck-labelled roster is served
    by the truck dropdown, not the name list)."""
    fmt = name_format()
    return "first_initial" if fmt == "truck" else fmt


def active_drivers(csv_path: Optional[str] = None) -> list[dict]:
    """One { id (opaque), display (formatted) } per ACTIVE driver in the latest
    metrics snapshot, sorted by canonical name.

    NO canonical/full name appears in the returned dicts — only the opaque sha256[:16]
    token and the disclosure-gated display label. do_signin resolves the token back to
    the canonical name server-side (see id_to_canonical).
    """
    disp_fmt = _display_format()
    return [
        {"id": driver_id_for(canonical),
         "display": format_display_name(canonical, fmt=disp_fmt)}
        for canonical in _active_canonicals(csv_path)
    ]


def id_to_canonical(csv_path: Optional[str] = None) -> dict[str, str]:
    """The SERVER-SIDE id->canonical map, rebuilt over the SAME active snapshot the
    roster serves. do_signin uses it to resolve an opaque driver_id the client POSTs
    back to the real canonical name (which never leaves the server on the roster wire).
    """
    return {driver_id_for(canonical): canonical
            for canonical in _active_canonicals(csv_path)}


def active_truck_numbers(cur) -> list[int]:
    """Real truck NUMBERS of the active fleet: public.trucks identities that have a
    recent ranked-route signal in public.active_sale_stops. READ-ONLY on public."""
    cur.execute(
        """
        SELECT DISTINCT t.truck_number
        FROM public.trucks t
        JOIN public.active_sale_stops a ON a.truck_number = t.truck_number
        WHERE t.truck_number > 0 AND a.route_cluster_id IS NOT NULL
        ORDER BY t.truck_number
        """
    )
    return [int(r[0]) for r in cur.fetchall()]


def roster_response(cur, csv_path: Optional[str] = None) -> dict:
    """The full /roster body the client renders directly. READ-ONLY.

    { trucks: [int,...], drivers: [{id, display},...], name_format: <knob> }

    The payload carries NO canonical/full name — only an opaque id + the disclosure
    label per the name_format knob.
    """
    return {
        "trucks": active_truck_numbers(cur),
        "drivers": active_drivers(csv_path=csv_path),
        "name_format": name_format(),
    }

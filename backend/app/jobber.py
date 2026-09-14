"""Jobber truck->driver-of-record + name mapping.

The sign-in sends {name, truck}. The name the driver picks is a Jobber-side
display name; the driver-performance snapshot keys on the Square `employee_name`.
`jobber_name_map.json` (driver-performance) is the authoritative Jobber->Square
override table the whole stack already uses. We resolve the picked name through
it so `driver-of-record` and the profile snapshot line up.

driver_id of record: `jobber:<square_name_normalized>` — a stable per-driver id
used on the session/heartbeat rows (matches the client's `jobber:<name>` shape in
app.js and the heartbeat `driver_id`).

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Optional

_DEFAULT_MAP_PATH = os.path.expanduser(
    "~/projects/work/MisterSoftee/driver-performance/jobber_name_map.json"
)

# Hosting seam (Phase 3a): DRIVER_COACH_NAME_MAP names the Jobber->Square map
# JSON. Unset -> the mini's local default (unchanged). Resolved at call time so a
# serverless deploy can set it via env; see build/FORGE_BRIEF_V2.2_P3A_HOSTABLE.md.
_NAME_MAP_ENV = "DRIVER_COACH_NAME_MAP"


def _map_path() -> str:
    """Resolve the name-map path: env override, else local default."""
    env_path = (os.environ.get(_NAME_MAP_ENV) or "").strip()
    if env_path:
        return os.path.expanduser(env_path)
    return _DEFAULT_MAP_PATH


# Back-compat: some callers/tests reference the module-level default path.
_MAP_PATH = _DEFAULT_MAP_PATH


@lru_cache(maxsize=8)
def _load_map(path: str) -> dict:
    with open(path) as fh:
        d = json.load(fh)
    return d.get("map", d)


def resolve_square_name(picked_name: str, path: Optional[str] = None) -> str:
    """Resolve a picked (Jobber) name to the Square employee_name used by the
    driver_profiles snapshot. Falls through to the picked name when there is no
    explicit override (the snapshot may already key on that exact name)."""
    m = _load_map(path or _map_path())
    if picked_name in m and m[picked_name]:
        return m[picked_name]
    # case-insensitive fallback over keys
    low = picked_name.strip().lower()
    for k, v in m.items():
        if k.strip().lower() == low and v:
            return v
    return picked_name


def driver_id_for(square_name: str) -> str:
    """Stable driver-of-record id: jobber:<normalized square name>."""
    slug = re.sub(r"\s+", "_", square_name.strip().lower())
    return f"jobber:{slug}"


def resolve_driver_of_record(picked_name: str, path: Optional[str] = None) -> dict:
    """{picked_name, square_name, driver_id} for a sign-in name pick."""
    square = resolve_square_name(picked_name, path=path or _map_path())
    return {
        "picked_name": picked_name,
        "square_name": square,
        "driver_id": driver_id_for(square),
    }

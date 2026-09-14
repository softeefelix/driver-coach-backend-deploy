"""Read the REAL driver_profiles nightly snapshot -> a DriverProfile the reference
flag resolver consumes. No fabricated rows.

driver_profiles.py emits `research/driver_metrics_<date>.csv` nightly with, per
driver: tier, stops_per_day, days_worked, status, plus `_light` (LIGHT SAMPLE new
hires). We read the LATEST CSV (the cleanest deterministic per-driver
latest-snapshot read the brief asks for), and map one driver's row to the
reference `DriverProfile` dataclass — field names already mirror the CSV.

`profile_age_days` = age of the snapshot file relative to `today` (so a stale
nightly run fails safe to coached via the resolver's staleness gate).

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import csv
import datetime
import glob
import os
from typing import Optional

from .refcore import DriverProfile

_RESEARCH = os.path.expanduser(
    "~/projects/work/MisterSoftee/driver-performance/research"
)

# Hosting seam (Phase 3a): when DRIVER_COACH_METRICS_CSV points at a single
# metrics CSV, we read THAT file directly (no globbing) so a Vercel/serverless
# deploy can ship a bundled snapshot. Unset -> the mini's local-glob default
# (unchanged behavior). See build/FORGE_BRIEF_V2.2_P3A_HOSTABLE.md.
_METRICS_CSV_ENV = "DRIVER_COACH_METRICS_CSV"


def latest_metrics_csv(research_dir: str = _RESEARCH) -> str:
    """Resolve the metrics snapshot CSV.

    If DRIVER_COACH_METRICS_CSV is set it names a single bundled CSV (hosting
    seam) — returned directly, no globbing. Unset -> glob the local research dir
    for the newest driver_metrics_*.csv (the mini's unchanged default).
    """
    env_csv = (os.environ.get(_METRICS_CSV_ENV) or "").strip()
    if env_csv:
        path = os.path.expanduser(env_csv)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"{_METRICS_CSV_ENV}={path} does not exist"
            )
        return path
    files = sorted(glob.glob(os.path.join(research_dir, "driver_metrics_*.csv")))
    if not files:
        raise FileNotFoundError(f"no driver_metrics_*.csv under {research_dir}")
    return files[-1]


def _snapshot_date(csv_path: str) -> datetime.date:
    """Snapshot date for staleness gating.

    Prefer the driver_metrics_<date>.csv name stamp (deterministic, what the
    nightly emits). A bundled file whose name does not carry that stamp falls
    back to the file's mtime date so the resolver's staleness gate still works
    when hosted (fail-safe: a stale bundle -> coached).
    """
    base = os.path.basename(csv_path)
    if base.startswith("driver_metrics_") and base.endswith(".csv"):
        stamp = base[len("driver_metrics_"):-len(".csv")]
        try:
            return datetime.date.fromisoformat(stamp)
        except ValueError:
            pass
    return datetime.date.fromtimestamp(os.path.getmtime(csv_path))


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    v = str(v).strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _to_int(v, default: int = 0) -> int:
    f = _to_float(v)
    return int(f) if f is not None else default


def load_profile_row(
    driver_name: str,
    csv_path: Optional[str] = None,
    today: Optional[datetime.date] = None,
) -> Optional[dict]:
    """Return the raw CSV row (dict) for `driver_name`, or None if absent.
    Match is case-insensitive on the exact `driver` column (Square employee name)."""
    csv_path = csv_path or latest_metrics_csv()
    key = driver_name.strip().lower()
    with open(csv_path) as fh:
        for row in csv.DictReader(fh):
            if (row.get("driver") or "").strip().lower() == key:
                return row
    return None


def build_driver_profile(
    driver_id: str,
    driver_name: str,
    csv_path: Optional[str] = None,
    today: Optional[datetime.date] = None,
) -> DriverProfile:
    """Map the latest real snapshot row for `driver_name` to a DriverProfile.

    Unknown driver -> a conservative profile (tier=None -> resolver coaches). The
    resolver already fails safe on unknown/stale/new-hire, so a missing snapshot
    can never wrongly un-coach anyone.
    """
    csv_path = csv_path or latest_metrics_csv()
    today = today or datetime.date.today()
    age_days = max((today - _snapshot_date(csv_path)).days, 0)
    row = load_profile_row(driver_name, csv_path=csv_path, today=today)
    if row is None:
        # No snapshot for this driver -> conservative default (resolver -> coached).
        return DriverProfile(
            driver_id=driver_id,
            tier=None,
            days_worked=0,
            stops_per_day=None,
            status="ACTIVE",
            profile_age_days=age_days,
        )
    # `_light` new hires carry tier "LIGHT SAMPLE"; the CSV already sets that.
    tier = (row.get("tier") or "").strip() or None
    status = (row.get("status") or "ACTIVE").strip() or "ACTIVE"
    return DriverProfile(
        driver_id=driver_id,
        tier=tier,
        days_worked=_to_int(row.get("days_worked")),
        stops_per_day=_to_float(row.get("stops_per_day")),
        status=status,
        profile_age_days=age_days,
    )

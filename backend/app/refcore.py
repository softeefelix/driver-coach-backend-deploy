"""Import the Warden-PASSed reference cores VERBATIM — no logic fork.

The brief is explicit: "Do NOT rewrite their logic — wrap them." The two pure
cores live at build/reference/{flag_resolver,motion_state}.py and are the tested
(26/26 green) decision cores. This module puts build/reference on sys.path and
re-exports them so the service imports the exact reviewed code, and the reference
test-suite keeps them green independently of anything the service does.

Maker: Forge. Reviewer of record: Warden. Nothing here deploys without Felix.
"""
from __future__ import annotations

import os
import sys

# backend/app/refcore.py -> repo root = driver-coach-ipad/
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REFERENCE_DIR = os.path.join(_REPO_ROOT, "build", "reference")
if _REFERENCE_DIR not in sys.path:
    sys.path.insert(0, _REFERENCE_DIR)

# Verbatim re-export — the service NEVER redefines this logic.
from flag_resolver import (  # noqa: E402  (path set above)
    DriverProfile,
    FlagConfig,
    PriorFlagState,
    ResolvedFlags,
    resolve_flags,
)
from motion_state import (  # noqa: E402
    ARRIVING,
    DRIVING,
    PARKED,
    GeotabFix,
    MotionConfig,
    MotionResult,
    MotionState,
    step as motion_step,
)

__all__ = [
    "DriverProfile",
    "FlagConfig",
    "PriorFlagState",
    "ResolvedFlags",
    "resolve_flags",
    "GeotabFix",
    "MotionConfig",
    "MotionResult",
    "MotionState",
    "motion_step",
    "DRIVING",
    "ARRIVING",
    "PARKED",
    "REPO_ROOT",
]

REPO_ROOT = _REPO_ROOT

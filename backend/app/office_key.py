"""Shared office key for Hermes reads, the scorecard ingest, and Square OAuth.

``DC_OFFICE_KEY`` is read from the environment on each request. Nothing here
writes that variable, and the value is never logged. Comparison uses
``hmac.compare_digest``.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import HTTPException

ENV_NAME = "DC_OFFICE_KEY"
HEADER_NAME = "X-DC-Office-Key"


def configured_key() -> Optional[str]:
    raw = os.environ.get(ENV_NAME)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _matches(presented: Optional[str], expected: str) -> bool:
    candidate = presented if isinstance(presented, str) else ""
    return hmac.compare_digest(candidate, expected)


def require_office_read(presented: Optional[str]) -> None:
    """Idle-exclusions gate. Unset env keeps today's open read."""
    expected = configured_key()
    if expected is None:
        return
    if not _matches(presented, expected):
        raise HTTPException(status_code=401, detail="office key required")


def require_configured_office_key(presented: Optional[str]) -> None:
    """A configured office key is required. Unset env is 503; a mismatch is 401."""
    expected = configured_key()
    if expected is None:
        raise HTTPException(status_code=503, detail="office key is not configured")
    if not _matches(presented, expected):
        raise HTTPException(status_code=401, detail="office key required")


def require_office_ingest(presented: Optional[str]) -> None:
    """Scorecard ingest. Unset env refuses; a set env requires the header."""
    require_configured_office_key(presented)

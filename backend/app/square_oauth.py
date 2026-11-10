"""Read-only Square OAuth (authorization code + PKCE).

The application secret is never sent. A personal access token is not required.
When ``SQUARE_OAUTH_APP_ID`` is unset, every route here is disabled and token
resolution does not touch the database.

Connect links are single-use nonces. Only their SHA-256 hash is stored.
``GET /start`` checks the nonce and does not consume it. ``POST /start``
consumes it and creates the PKCE state. A later GET or POST reuses that
state while it is unused and unexpired. ``/start`` does not accept the
office key.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import NamedTuple, Optional

import psycopg2
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import office_key
from .db import connect

_log = logging.getLogger(__name__)

router = APIRouter()

APP_ID_ENV = "SQUARE_OAUTH_APP_ID"
REDIRECT_ENV = "SQUARE_OAUTH_REDIRECT_URI"
DEFAULT_REDIRECT_URI = (
    "https://driver-coach-backend-test.onrender.com/driver-coach/v1/square/oauth/callback"
)
DEFAULT_TOKEN_FILE = "~/.hermes/secrets/softeedashboard.env"
# ListPayments and SearchTeamMembers. Orders and merchant-profile reads are not called.
SQUARE_SCOPES = ("PAYMENTS_READ", "EMPLOYEES_READ")
AUTHORIZE_URL = "https://connect.squareup.com/oauth2/authorize"
TOKEN_URL = "https://connect.squareup.com/oauth2/token"
SQUARE_VERSION = "2024-01-18"
LINK_PATH = "/driver-coach/v1/square/oauth/link"
START_PATH = "/driver-coach/v1/square/oauth/start"
CALLBACK_PATH = "/driver-coach/v1/square/oauth/callback"
STATUS_PATH = "/driver-coach/v1/square/oauth/status"
SUCCESS_TEXT = "Driver Coach now has read-only Square access"
CONFIRM_TITLE = "Connect Square to Driver Coach"
ALREADY_CONNECTED = "Square is already connected"
LINK_TTL = dt.timedelta(minutes=30)
STATE_TTL = dt.timedelta(minutes=15)
# A second GET/POST can show the same Square URL for this long after the
# nonce is consumed, and never past the PKCE state's own expiry.
REPLAY_WINDOW = dt.timedelta(minutes=10)
REFRESH_WITHIN = dt.timedelta(days=1)
CLAIM_STALE = dt.timedelta(seconds=45)
CACHE_TTL_S = 30.0
COOLDOWN_S = 60.0
_SAFE_ERROR = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_MISS = object()

_cache_lock = threading.Lock()
_flight = threading.Lock()
_cache_access: Optional[str] = None
_cache_expires: Optional[dt.datetime] = None
_cache_until = 0.0
_cooldown_until = 0.0
_cooldown_access: Optional[str] = None
_cooldown_expires: Optional[dt.datetime] = None


class OAuthStorageUnavailable(Exception):
    """The OAuth tables are missing or the database cannot be opened."""


class OAuthExchangeError(Exception):
    def __init__(self, code: str):
        self.code = _public_error(code)
        super().__init__(self.code)


class StateRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class StartDecision(NamedTuple):
    kind: str
    status: int = 400
    message: str = ""
    location: str = ""
    nonce: Optional[str] = None
    fields: tuple[tuple[str, str], ...] = ()


class Decision(NamedTuple):
    kind: str
    access: Optional[str] = None
    refresh: Optional[str] = None
    expires_at: Optional[dt.datetime] = None
    redirect_uri: Optional[str] = None
    claim_id: Optional[str] = None


def oauth_enabled() -> bool:
    return configured_app_id() is not None


def configured_app_id() -> Optional[str]:
    raw = os.environ.get(APP_ID_ENV)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def redirect_uri() -> str:
    raw = os.environ.get(REDIRECT_ENV)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return DEFAULT_REDIRECT_URI


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def clear_memory() -> None:
    """Drop the process token cache. Tests and a fresh grant call this."""
    global _cache_access, _cache_expires, _cache_until
    global _cooldown_until, _cooldown_access, _cooldown_expires
    with _cache_lock:
        _cache_access = None
        _cache_expires = None
        _cache_until = 0.0
        _cooldown_until = 0.0
        _cooldown_access = None
        _cooldown_expires = None


def resolve_square_access_token(file_path: str = DEFAULT_TOKEN_FILE) -> Optional[str]:
    """OAuth token, then ``SQUARE_ACCESS_TOKEN``, then the Hermes file."""
    if oauth_enabled():
        token = _oauth_or_none()
        if token:
            return token
    return legacy_square_token(file_path)


def legacy_square_token(file_path: str = DEFAULT_TOKEN_FILE) -> Optional[str]:
    env = os.environ.get("SQUARE_ACCESS_TOKEN")
    if isinstance(env, str) and env.strip():
        return env.strip()
    return _file_token(file_path)


def refresh_after_unauthorized(
    used_token: str, file_path: str = DEFAULT_TOKEN_FILE
) -> Optional[str]:
    """Refresh when Square rejected an OAuth access token. Otherwise try the legacy token."""
    if not oauth_enabled() or not isinstance(used_token, str) or not used_token:
        return None
    _invalidate_cache()
    current = _peek_quietly()
    if current and not _secret_equal(current, used_token):
        return current
    refreshed = None
    if current and _secret_equal(current, used_token):
        try:
            refreshed = ensure_fresh_token(force=True, seen_access=used_token)
        except OAuthStorageUnavailable:
            refreshed = None
        except Exception:
            _log.warning("square oauth refresh after 401 failed")
            refreshed = None
    if isinstance(refreshed, str) and refreshed and not _secret_equal(refreshed, used_token):
        return refreshed
    legacy = legacy_square_token(file_path)
    if legacy and not _secret_equal(legacy, used_token):
        return legacy
    return None


def ensure_fresh_token(*, force: bool = False, seen_access: Optional[str] = None) -> Optional[str]:
    early = _cached_token(force)
    if early is not _MISS:
        return early
    with _flight:
        if force and seen_access:
            current = _peek_access()
            if current and not _secret_equal(current, seen_access):
                return current
        early = _cached_token(force)
        if early is not _MISS:
            return early
        return _refresh_or_read(force=force)


def issue_link() -> dict:
    nonce = secrets.token_urlsafe(32)
    digest = _hash_nonce(nonce)
    expires = _now().replace(microsecond=0) + LINK_TTL

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM square_oauth_link WHERE expires_at < now() - interval '1 day'"
            )
            cur.execute(
                "INSERT INTO square_oauth_link (token_hash, expires_at) VALUES (%s, %s)",
                (digest, expires),
            )
        conn.commit()

    _db(run)
    return {"url": _public_start_url(nonce), "expiresAt": _iso(expires)}


def _resolve_start(nonce: Optional[str], *, consume: bool) -> StartDecision:
    """Classify a /start request.

    GET passes ``consume=False`` and never writes. POST passes ``consume=True``.
    The first successful POST marks the nonce used and inserts the PKCE state
    in that same transaction. The state primary key is the nonce hash, which
    is already the link's primary key, so a later request can reload the
    verifier from ``square_oauth_state`` without storing it anywhere else.
    """
    digest = _hash_nonce(nonce)
    if digest is None:
        return StartDecision("error", 400, _link_message("invalid"))

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                """SELECT expires_at, used_at FROM square_oauth_link
                   WHERE token_hash = %s FOR UPDATE""",
                (digest,),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return StartDecision("error", 400, _link_message("invalid"))
            expires_at, used_at = row
            if used_at is None:
                if _aware(expires_at) <= _now():
                    conn.rollback()
                    return StartDecision("error", 400, _link_message("expired"))
                if not consume:
                    conn.rollback()
                    return StartDecision("confirm", 200, nonce=nonce)
                return _consume_and_redirect(conn, cur, digest)
            decision = _replay_decision(cur, digest, used_at)
            conn.rollback()
            return decision

    return _db(run)


def _consume_and_redirect(conn, cur, digest: str) -> StartDecision:
    verifier, _challenge = _pkce_pair()
    redir = redirect_uri()
    fields = _authorize_fields(digest, verifier, redir)
    location = _authorize_url(fields)
    cur.execute(
        "DELETE FROM square_oauth_state WHERE expires_at < now() - interval '1 day'"
    )
    cur.execute(
        """INSERT INTO square_oauth_state
               (state, code_verifier, redirect_uri, expires_at)
           VALUES (%s, %s, %s, %s)""",
        (digest, verifier, redir, _now() + STATE_TTL),
    )
    cur.execute(
        """UPDATE square_oauth_link SET used_at = now()
           WHERE token_hash = %s AND used_at IS NULL""",
        (digest,),
    )
    if cur.rowcount != 1:
        conn.rollback()
        return StartDecision("error", 400, _link_message("replay"))
    conn.commit()
    return StartDecision("redirect", 303, location=location)


def _replay_decision(cur, digest: str, consumed_at) -> StartDecision:
    cur.execute(
        """SELECT code_verifier, redirect_uri, expires_at, used_at
           FROM square_oauth_state WHERE state = %s FOR UPDATE""",
        (digest,),
    )
    row = cur.fetchone()
    if row is None:
        return StartDecision("error", 400, _link_message("replay"))
    verifier, redir, expires_at, state_used = row
    if state_used is not None:
        cur.execute("SELECT id FROM square_oauth_token WHERE id = 1")
        if cur.fetchone() is not None:
            return StartDecision("error", 400, ALREADY_CONNECTED)
        return StartDecision("error", 400, _link_message("replay"))
    if not isinstance(verifier, str) or not verifier:
        return StartDecision("error", 400, _link_message("replay"))
    if _aware(expires_at) <= _now():
        return StartDecision("error", 400, _state_message("expired"))
    if _now() - _aware(consumed_at) > REPLAY_WINDOW:
        return StartDecision("error", 400, _link_message("expired"))
    return StartDecision("confirm", 200, fields=_authorize_fields(digest, verifier, redir))


def _authorize_fields(state: str, verifier: str, redir: str) -> tuple[tuple[str, str], ...]:
    app_id = configured_app_id()
    if not app_id:
        raise OAuthExchangeError("oauth_disabled")
    return (
        ("client_id", app_id),
        ("scope", " ".join(SQUARE_SCOPES)),
        ("session", "false"),
        ("state", state),
        ("code_challenge", code_challenge_s256(verifier)),
        ("code_challenge_method", "S256"),
        ("redirect_uri", redir),
    )


def _authorize_url(fields: tuple[tuple[str, str], ...]) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(fields)


def consume_state(state: Optional[str]) -> tuple[str, str]:
    if not isinstance(state, str) or not state or len(state) > 200:
        raise StateRejected("invalid")

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                """SELECT code_verifier, redirect_uri, expires_at, used_at
                   FROM square_oauth_state WHERE state = %s FOR UPDATE""",
                (state,),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                raise StateRejected("invalid")
            verifier, redir, expires_at, used_at = row
            if used_at is not None:
                conn.rollback()
                raise StateRejected("replay")
            if _aware(expires_at) <= _now():
                conn.rollback()
                raise StateRejected("expired")
            cur.execute(
                """UPDATE square_oauth_state
                   SET used_at = now(), code_verifier = ''
                   WHERE state = %s""",
                (state,),
            )
        conn.commit()
        return verifier, redir

    return _db(run)


def exchange_code(code: str, verifier: str, redir: str) -> dict:
    app_id = configured_app_id()
    if not app_id:
        raise OAuthExchangeError("oauth_disabled")
    payload = {
        "client_id": app_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redir,
        "code_verifier": verifier,
    }
    return _post_json(TOKEN_URL, payload)


def save_grant(body: dict, redir: str) -> None:
    parsed = _require_token_body(body)

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO square_oauth_token (
                       id, access_token, refresh_token, expires_at, merchant_id,
                       scopes, redirect_uri, last_refresh_at, last_error,
                       refresh_claim_id, refresh_claimed_at, updated_at
                   ) VALUES (1, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, now())
                   ON CONFLICT (id) DO UPDATE SET
                       access_token = EXCLUDED.access_token,
                       refresh_token = EXCLUDED.refresh_token,
                       expires_at = EXCLUDED.expires_at,
                       merchant_id = EXCLUDED.merchant_id,
                       scopes = EXCLUDED.scopes,
                       redirect_uri = EXCLUDED.redirect_uri,
                       last_refresh_at = NULL,
                       last_error = NULL,
                       refresh_claim_id = NULL,
                       refresh_claimed_at = NULL,
                       updated_at = now()""",
                (
                    parsed["access_token"],
                    parsed["refresh_token"],
                    parsed["expires_at"],
                    parsed["merchant_id"],
                    " ".join(SQUARE_SCOPES),
                    redir,
                ),
            )
        conn.commit()

    _db(run)
    _cache_put(parsed["access_token"], parsed["expires_at"])
    _clear_cooldown()
    _log.info("square oauth connected")


def public_status() -> dict:
    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                """SELECT merchant_id, scopes, expires_at, last_refresh_at, last_error
                   FROM square_oauth_token WHERE id = 1"""
            )
            row = cur.fetchone()
        conn.rollback()
        return row

    row = _db(run)
    empty = {
        "connected": False,
        "merchantId": None,
        "scopes": [],
        "expiresAt": None,
        "lastRefreshAt": None,
        "lastError": None,
    }
    if row is None:
        return empty
    merchant, scopes, expires_at, refreshed, last_error = row
    return {
        "connected": True,
        "merchantId": merchant or None,
        "scopes": [part for part in str(scopes or "").split() if part],
        "expiresAt": _iso(expires_at) if expires_at else None,
        "lastRefreshAt": _iso(refreshed) if refreshed else None,
        "lastError": _safe_stored_error(last_error),
    }


def assert_storage() -> None:
    def run(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT token_hash FROM square_oauth_link WHERE false")
            cur.execute("SELECT state FROM square_oauth_state WHERE false")
            cur.execute("SELECT id FROM square_oauth_token WHERE false")
        conn.rollback()

    _db(run)


def _oauth_or_none() -> Optional[str]:
    try:
        return ensure_fresh_token(force=False)
    except OAuthStorageUnavailable:
        return None
    except Exception:
        _log.warning("square oauth token read failed")
        return None


def _peek_quietly() -> Optional[str]:
    try:
        return _peek_access()
    except OAuthStorageUnavailable:
        return None
    except Exception:
        _log.warning("square oauth token read failed")
        return None


def _peek_access() -> Optional[str]:
    row = _read_row()
    return row[0] if row else None


def _refresh_or_read(*, force: bool) -> Optional[str]:
    decision = _try_claim(force=force)
    if decision.kind == "none":
        return None
    if decision.kind == "use":
        _cache_put(decision.access or "", decision.expires_at)
        return decision.access
    if decision.kind == "wait":
        return _wait_for_refresh(decision.refresh or "")
    try:
        # The row lock was released inside _try_claim. Square is called only now.
        body = _refresh_request(decision.refresh or "", decision.redirect_uri or redirect_uri())
    except OAuthExchangeError as exc:
        _note_failure(decision.access, decision.expires_at)
        return _finish_error(decision.claim_id or "", exc.code, decision.access, decision.expires_at, force)
    except Exception:
        _log.warning("square oauth refresh failed")
        _note_failure(decision.access, decision.expires_at)
        return _finish_error(decision.claim_id or "", "refresh_failed", decision.access, decision.expires_at, force)
    try:
        return _finish_success(decision.claim_id or "", body)
    except Exception:
        _log.warning("square oauth refresh failed")
        _note_failure(decision.access, decision.expires_at)
        return _finish_error(decision.claim_id or "", "refresh_failed", decision.access, decision.expires_at, force)


def _try_claim(*, force: bool) -> Decision:
    claim_id = str(uuid.uuid4())

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                """SELECT access_token, refresh_token, expires_at, redirect_uri,
                          refresh_claim_id, refresh_claimed_at
                   FROM square_oauth_token WHERE id = 1 FOR UPDATE"""
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return Decision("none")
            access, refresh, expires_at, redir, claim, claimed_at = row
            expires_at = _aware(expires_at)
            if not force and not _needs_refresh(expires_at):
                conn.rollback()
                return Decision("use", access=access, expires_at=expires_at)
            if claim and _claim_is_current(claimed_at):
                conn.rollback()
                return Decision("wait", access=access, refresh=refresh, expires_at=expires_at)
            cur.execute(
                """UPDATE square_oauth_token
                   SET refresh_claim_id = %s, refresh_claimed_at = now()
                   WHERE id = 1""",
                (claim_id,),
            )
        conn.commit()
        return Decision(
            "go",
            claim_id=claim_id,
            access=access,
            refresh=refresh,
            expires_at=expires_at,
            redirect_uri=redir or redirect_uri(),
        )

    return _db(run)


def _wait_for_refresh(prior_refresh: str) -> Optional[str]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(0.05)
        row = _read_row()
        if row is None:
            return None
        access, refresh, expires_at, _redir, claim, _claimed = row
        expires_at = _aware(expires_at)
        if claim:
            continue
        if refresh != prior_refresh or not _needs_refresh(expires_at):
            if expires_at > _now():
                if not _needs_refresh(expires_at):
                    _cache_put(access, expires_at)
                return access
            return None
        if expires_at > _now():
            return access
        return None
    return None


def _finish_success(claim_id: str, body: dict) -> Optional[str]:
    parsed = _require_token_body(body)

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                """SELECT refresh_claim_id, access_token, expires_at
                   FROM square_oauth_token WHERE id = 1 FOR UPDATE"""
            )
            row = cur.fetchone()
            if row is None or row[0] != claim_id:
                conn.rollback()
                if row and row[1]:
                    return row[1], _aware(row[2]) if row[2] else None
                return None, None
            cur.execute(
                """UPDATE square_oauth_token SET
                       access_token = %s,
                       refresh_token = %s,
                       expires_at = %s,
                       merchant_id = COALESCE(%s, merchant_id),
                       last_refresh_at = now(),
                       last_error = NULL,
                       refresh_claim_id = NULL,
                       refresh_claimed_at = NULL,
                       updated_at = now()
                   WHERE id = 1""",
                (
                    parsed["access_token"],
                    parsed["refresh_token"],
                    parsed["expires_at"],
                    parsed["merchant_id"],
                ),
            )
        conn.commit()
        return parsed["access_token"], parsed["expires_at"]

    access, expires_at = _db(run)
    if access == parsed["access_token"]:
        _clear_cooldown()
        if expires_at and not _needs_refresh(expires_at):
            _cache_put(access, expires_at)
        _log.info("square oauth refresh succeeded")
    elif access and expires_at and not _needs_refresh(expires_at):
        _cache_put(access, expires_at)
    return access


def _finish_error(claim_id, code, access, expires_at, force: bool) -> Optional[str]:
    safe = _public_error(code)

    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE square_oauth_token SET
                       last_error = %s,
                       refresh_claim_id = NULL,
                       refresh_claimed_at = NULL,
                       updated_at = now()
                   WHERE id = 1 AND refresh_claim_id = %s""",
                (safe, claim_id),
            )
        conn.commit()

    _db(run)
    if force:
        return None
    if expires_at is not None and _aware(expires_at) > _now():
        return access
    return None


def _refresh_request(refresh_token: str, redir: str) -> dict:
    app_id = configured_app_id()
    if not app_id:
        raise OAuthExchangeError("oauth_disabled")
    payload = {
        "client_id": app_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": redir,
    }
    return _post_json(TOKEN_URL, payload)


def _post_json(url: str, payload: dict, timeout: float = 10) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Square-Version": SQUARE_VERSION},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310: fixed Square host
            parsed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            exc.close()
        except Exception:
            pass
        raise OAuthExchangeError(_square_error_code(raw, exc.code)) from None
    except OAuthExchangeError:
        raise
    except Exception:
        raise OAuthExchangeError("token_endpoint_unreachable") from None
    if not isinstance(parsed, dict):
        raise OAuthExchangeError("incomplete_token_response")
    return parsed


def _read_row():
    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                """SELECT access_token, refresh_token, expires_at, redirect_uri,
                          refresh_claim_id, refresh_claimed_at
                   FROM square_oauth_token WHERE id = 1"""
            )
            row = cur.fetchone()
        conn.rollback()
        return row

    return _db(run)


def _db(fn):
    try:
        with connect() as conn:
            return fn(conn)
    except psycopg2.Error as exc:
        if _missing_table(exc):
            raise OAuthStorageUnavailable() from None
        raise
    except RuntimeError as exc:
        if "DATABASE_URL" in str(exc):
            raise OAuthStorageUnavailable() from None
        raise


def _missing_table(exc: BaseException) -> bool:
    if isinstance(exc, psycopg2.errors.UndefinedTable):
        return True
    return getattr(exc, "pgcode", None) == "42P01"


def _file_token(path: str) -> Optional[str]:
    try:
        with open(os.path.expanduser(path)) as handle:
            for line in handle:
                if line.startswith("SQUARE_TOKEN="):
                    return line.partition("=")[2].strip().strip('"') or None
    except OSError:
        return None
    return None


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, code_challenge_s256(verifier)


def _hash_nonce(nonce: Optional[str]) -> Optional[str]:
    if not isinstance(nonce, str) or not nonce or len(nonce) > 512:
        return None
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _public_start_url(nonce: str) -> str:
    parts = urllib.parse.urlsplit(redirect_uri())
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, START_PATH, urllib.parse.urlencode([("t", nonce)]), "")
    )


def _require_token_body(body: dict) -> dict:
    if not isinstance(body, dict):
        raise OAuthExchangeError("incomplete_token_response")
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise OAuthExchangeError("incomplete_token_response")
    merchant = body.get("merchant_id")
    if not isinstance(merchant, str) or not merchant:
        merchant = None
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": _parse_expires(body.get("expires_at")),
        "merchant_id": merchant,
    }


def _parse_expires(value: object) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise OAuthExchangeError("missing_expiry")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise OAuthExchangeError("missing_expiry") from None
    return _aware(parsed)


def _square_error_code(raw: str, status: int) -> str:
    parsed = None
    try:
        parsed = json.loads(raw) if raw else None
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        errors = parsed.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            code = errors[0].get("code")
            if isinstance(code, str) and _SAFE_ERROR.fullmatch(code):
                return code
    fallback = f"http_{int(status)}"
    if _SAFE_ERROR.fullmatch(fallback):
        return fallback
    return "token_exchange_failed"


def _public_error(code: str) -> str:
    if isinstance(code, str) and _SAFE_ERROR.fullmatch(code):
        return code
    return "token_exchange_failed"


def _safe_stored_error(value) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    if _SAFE_ERROR.fullmatch(value):
        return value
    return "token_exchange_failed"


def _needs_refresh(expires_at: dt.datetime, now: Optional[dt.datetime] = None) -> bool:
    return _aware(expires_at) - (now or _now()) <= REFRESH_WITHIN


def _claim_is_current(claimed_at) -> bool:
    if claimed_at is None:
        return False
    return _now() - _aware(claimed_at) < CLAIM_STALE


def _cached_token(force: bool):
    if force:
        return _MISS
    with _cache_lock:
        now_m = time.monotonic()
        if (
            _cache_access
            and now_m < _cache_until
            and _cache_expires is not None
            and not _needs_refresh(_cache_expires)
        ):
            return _cache_access
        if now_m < _cooldown_until:
            if _cooldown_access and _cooldown_expires and _aware(_cooldown_expires) > _now():
                return _cooldown_access
            return None
    return _MISS


def _cache_put(access: str, expires_at: Optional[dt.datetime]) -> None:
    global _cache_access, _cache_expires, _cache_until
    if not access or expires_at is None or _needs_refresh(expires_at):
        return
    with _cache_lock:
        _cache_access = access
        _cache_expires = _aware(expires_at)
        _cache_until = time.monotonic() + CACHE_TTL_S


def _invalidate_cache() -> None:
    global _cache_until
    with _cache_lock:
        _cache_until = 0.0


def _note_failure(access: Optional[str], expires_at: Optional[dt.datetime]) -> None:
    global _cooldown_until, _cooldown_access, _cooldown_expires
    with _cache_lock:
        _cooldown_until = time.monotonic() + COOLDOWN_S
        _cooldown_access = access
        _cooldown_expires = _aware(expires_at) if expires_at is not None else None


def _clear_cooldown() -> None:
    global _cooldown_until, _cooldown_access, _cooldown_expires
    with _cache_lock:
        _cooldown_until = 0.0
        _cooldown_access = None
        _cooldown_expires = None


def _secret_equal(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    a = left.encode("utf-8")
    b = right.encode("utf-8")
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _iso(value: dt.datetime) -> str:
    return _aware(value).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _posted_nonce(raw: bytes) -> Optional[str]:
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        parsed = urllib.parse.parse_qs(text, keep_blank_values=False, separator="&")
    except ValueError:
        return None
    values = parsed.get("t")
    if not values:
        return None
    nonce = values[0]
    if not isinstance(nonce, str) or not nonce:
        return None
    return nonce


def _hidden(name: str, value: str) -> str:
    return (
        '<input type="hidden" name="'
        + html.escape(name, quote=True)
        + '" value="'
        + html.escape(value, quote=True)
        + '">'
    )


def _confirm_page(decision: StartDecision) -> HTMLResponse:
    if decision.fields:
        method = "get"
        action = AUTHORIZE_URL
        fields = "".join(_hidden(name, value) for name, value in decision.fields)
    else:
        method = "post"
        action = START_PATH
        fields = _hidden("t", decision.nonce or "")
    title = html.escape(CONFIRM_TITLE)
    body = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex">'
        "<title>" + title + "</title></head><body><h1>" + title + "</h1>"
        "<p>This requests read-only access to payments and team members.</p>"
        '<form method="' + method + '" action="' + html.escape(action, quote=True) + '" '
        'referrerpolicy="no-referrer">'
        + fields
        + '<button type="submit">Continue to Square</button></form></body></html>'
    )
    return HTMLResponse(
        body,
        status_code=200,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _start_response(decision: StartDecision):
    if decision.kind == "redirect":
        response = RedirectResponse(decision.location, status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    if decision.kind == "confirm":
        return _confirm_page(decision)
    return _page(decision.message, decision.status)


def _page(message: str, status: int) -> HTMLResponse:
    body = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        "<title>Driver Coach</title></head><body><p>"
        + html.escape(message)
        + "</p></body></html>"
    )
    return HTMLResponse(
        body,
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _link_message(reason: str) -> str:
    if reason == "expired":
        return "This Square connection link has expired. Ask for a new one."
    return "This Square connection link is invalid or has already been used."


def _state_message(reason: str) -> str:
    if reason == "expired":
        return "This Square authorization link has expired. Start again."
    return "This Square authorization link is invalid or has already been used."


def _disabled():
    raise HTTPException(status_code=404, detail="Square OAuth is disabled")


def _storage_unavailable():
    raise HTTPException(status_code=503, detail="Square OAuth storage is not available")


@router.post(LINK_PATH)
def oauth_link(
    x_dc_office_key: Optional[str] = Header(default=None, alias="X-DC-Office-Key"),
):
    if not oauth_enabled():
        _disabled()
    office_key.require_configured_office_key(x_dc_office_key)
    try:
        assert_storage()
        body = issue_link()
    except OAuthStorageUnavailable:
        _storage_unavailable()
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.get(START_PATH)
def oauth_start_get(t: Optional[str] = None):
    if not oauth_enabled():
        return _page("Square OAuth is disabled.", 404)
    try:
        assert_storage()
        decision = _resolve_start(t, consume=False)
    except OAuthStorageUnavailable:
        return _page("Square OAuth storage is not available.", 503)
    except Exception:
        _log.warning("square oauth start failed")
        return _page("Square authorization could not be started.", 500)
    return _start_response(decision)


@router.post(START_PATH)
async def oauth_start_post(request: Request):
    if not oauth_enabled():
        return _page("Square OAuth is disabled.", 404)
    try:
        nonce = _posted_nonce(await request.body())
        assert_storage()
        decision = _resolve_start(nonce, consume=True)
    except OAuthStorageUnavailable:
        return _page("Square OAuth storage is not available.", 503)
    except Exception:
        _log.warning("square oauth start failed")
        return _page("Square authorization could not be started.", 500)
    return _start_response(decision)


@router.get(CALLBACK_PATH)
def oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    if not oauth_enabled():
        return _page("Square OAuth is disabled.", 404)
    try:
        assert_storage()
    except OAuthStorageUnavailable:
        return _page("Square OAuth storage is not available.", 503)
    if error:
        return _page("Square did not grant access.", 400)
    if not code or not state:
        return _page("Square authorization could not be completed.", 400)
    try:
        verifier, redir = consume_state(state)
    except StateRejected as exc:
        return _page(_state_message(exc.reason), 400)
    except OAuthStorageUnavailable:
        return _page("Square OAuth storage is not available.", 503)
    try:
        body = exchange_code(code, verifier, redir)
        save_grant(body, redir)
    except OAuthStorageUnavailable:
        return _page("Square OAuth storage is not available.", 503)
    except OAuthExchangeError:
        return _page("Square authorization could not be completed.", 400)
    except Exception:
        _log.warning("square oauth callback failed")
        return _page("Square authorization could not be completed.", 400)
    return _page(SUCCESS_TEXT, 200)


@router.get(STATUS_PATH)
def oauth_status(
    x_dc_office_key: Optional[str] = Header(default=None, alias="X-DC-Office-Key"),
):
    if not oauth_enabled():
        _disabled()
    office_key.require_configured_office_key(x_dc_office_key)
    try:
        assert_storage()
        body = public_status()
    except OAuthStorageUnavailable:
        _storage_unavailable()
    return JSONResponse(body, headers={"Cache-Control": "no-store"})

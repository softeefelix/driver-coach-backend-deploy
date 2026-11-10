"""Square OAuth PKCE: office link, nonce, token storage, and refresh."""
import email.message
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pgserver
import psycopg2
import pytest
from fastapi.testclient import TestClient

from app import scorecard, service, square_oauth

SQL = (Path(__file__).parents[1] / "migrations" / "010_square_oauth.sql").read_text()
SCHEMA = "driver_coach_test"
EMPTY = "driver_coach_oauth_empty"
LINK = "/driver-coach/v1/square/oauth/link"
START = "/driver-coach/v1/square/oauth/start"
CALLBACK = "/driver-coach/v1/square/oauth/callback"
STATUS = "/driver-coach/v1/square/oauth/status"
APP_ID = "sq0idp-test-app"
OFFICE = "office-secret-value"
AT = "AT-secret-value"
RT = "RT-secret-value"
CODE = "AUTH-CODE-SECRET"
REDIRECT = square_oauth.DEFAULT_REDIRECT_URI
HEADER = {"X-DC-Office-Key": OFFICE}


def _rfc_challenge():
    # RFC 7636 appendix B.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert square_oauth.code_challenge_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


class _Body:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _connect_factory(dsn, schema_name):
    @contextmanager
    def connect(schema="", autocommit=False):
        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = autocommit
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema_name}"')
            yield conn
        finally:
            conn.close()

    return connect


def _sql(dsn, statement, args=()):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{SCHEMA}"')
            cur.execute(statement, args)
            rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _wipe(dsn):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{SCHEMA}"')
            cur.execute("DELETE FROM square_oauth_link")
            cur.execute("DELETE FROM square_oauth_state")
            cur.execute("DELETE FROM square_oauth_token")
        conn.commit()
    finally:
        conn.close()


def _seed(dsn, *, access, refresh, expires_at, redirect=REDIRECT):
    _sql(
        dsn,
        """INSERT INTO square_oauth_token (
               id, access_token, refresh_token, expires_at, merchant_id, scopes, redirect_uri
           ) VALUES (1, %s, %s, %s, 'MERCHANT1', 'PAYMENTS_READ EMPLOYEES_READ', %s)
           ON CONFLICT (id) DO UPDATE SET
               access_token = EXCLUDED.access_token,
               refresh_token = EXCLUDED.refresh_token,
               expires_at = EXCLUDED.expires_at,
               merchant_id = EXCLUDED.merchant_id,
               scopes = EXCLUDED.scopes,
               redirect_uri = EXCLUDED.redirect_uri,
               last_error = NULL,
               last_refresh_at = NULL,
               refresh_claim_id = NULL,
               refresh_claimed_at = NULL""",
        (access, refresh, expires_at, redirect),
    )


def _enable(monkeypatch, app_id=APP_ID):
    monkeypatch.setenv("SQUARE_OAUTH_APP_ID", app_id)
    monkeypatch.setenv("DC_OFFICE_KEY", OFFICE)


def _issued_nonce(client):
    opened = client.post(LINK, headers=HEADER)
    assert opened.status_code == 200, opened.text
    body = opened.json()
    nonce = urllib.parse.parse_qs(urllib.parse.urlsplit(body["url"]).query)["t"][0]
    return body, nonce


def _assert_absent(blob, *secrets):
    for secret in secrets:
        assert secret not in blob


def _app_log(caplog) -> str:
    """Application log lines. The test client records the request URL it was given."""
    return "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("app.")
    )


@pytest.fixture(scope="module")
def database():
    root = Path(tempfile.mkdtemp(prefix="square-oauth-", dir=os.environ.get("TMPDIR", "/tmp")))
    server = None
    try:
        server = pgserver.get_server(root / "data", cleanup_mode="delete")
        dsn = server.get_uri()
        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{SCHEMA}"')
                cur.execute(f'SET search_path TO "{SCHEMA}"')
                cur.execute(SQL)
                cur.execute(SQL)
                cur.execute(f'CREATE SCHEMA "{EMPTY}"')
        finally:
            conn.close()
        yield dsn
    finally:
        if server is not None:
            server.cleanup()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset(database, monkeypatch):
    monkeypatch.delenv("SQUARE_OAUTH_APP_ID", raising=False)
    monkeypatch.delenv("SQUARE_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("SQUARE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DC_OFFICE_KEY", raising=False)
    monkeypatch.setattr(square_oauth, "connect", _connect_factory(database, SCHEMA))
    square_oauth.clear_memory()
    scorecard.clear_sale_cache()
    _wipe(database)
    yield
    square_oauth.clear_memory()
    scorecard.clear_sale_cache()


@pytest.fixture
def client():
    return TestClient(service.app)


def test_pkce_s256_matches_the_rfc7636_vector():
    _rfc_challenge()


def test_app_id_and_application_secret_are_not_in_the_module():
    text = (Path(__file__).parents[1] / "app" / "square_oauth.py").read_text()
    assert "sq0idp-IcgFfAXRpjHszcKxCuGSFQ" not in text
    assert "client_secret" not in text


def test_migration_refuses_public_and_stays_out_of_it(database):
    conn = psycopg2.connect(database)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            with pytest.raises(psycopg2.Error):
                cur.execute(SQL)
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.square_oauth_token')")
            assert cur.fetchone()[0] is None
            cur.execute(f'SET search_path TO "{SCHEMA}"')
            cur.execute("SELECT to_regclass('square_oauth_token')")
            assert cur.fetchone()[0] is not None
    finally:
        conn.close()


def test_disabled_when_app_id_unset_leaves_the_file_token(database, monkeypatch, tmp_path, client):
    def explode(*_a, **_k):
        raise AssertionError("oauth storage used while disabled")

    monkeypatch.setattr(square_oauth, "connect", explode)
    path = tmp_path / "softeedashboard.env"
    path.write_text('SQUARE_TOKEN="file-token"\n')
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "env-token")
    assert square_oauth.resolve_square_access_token(str(path)) == "env-token"
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "   ")
    assert square_oauth.resolve_square_access_token(str(path)) == "file-token"
    monkeypatch.setenv("SQUARE_OAUTH_APP_ID", "   ")
    for method, url in (
        ("post", LINK),
        ("get", START),
        ("get", CALLBACK),
        ("get", STATUS),
    ):
        response = getattr(client, method)(url)
        assert response.status_code == 404


def test_link_and_status_require_the_office_key_header(monkeypatch, client):
    _enable(monkeypatch)
    monkeypatch.delenv("DC_OFFICE_KEY", raising=False)
    assert client.post(LINK).status_code == 503
    assert client.get(STATUS).status_code == 503

    monkeypatch.setenv("DC_OFFICE_KEY", OFFICE)
    assert client.post(LINK).status_code == 401
    assert client.post(LINK, headers={"X-DC-Office-Key": "nope"}).status_code == 401
    assert client.post(LINK, headers={"X-DC-Office-Key": "x"}).status_code == 401
    assert client.post(f"{LINK}?k={OFFICE}").status_code == 401
    assert client.get(STATUS).status_code == 401
    assert client.get(STATUS, params={"k": OFFICE}).status_code == 401

    opened = client.get(STATUS, headers=HEADER)
    assert opened.status_code == 200
    assert opened.json() == {
        "connected": False,
        "merchantId": None,
        "scopes": [],
        "expiresAt": None,
        "lastRefreshAt": None,
        "lastError": None,
    }
    assert "access_token" not in opened.text
    assert "refresh_token" not in opened.text


def test_start_never_accepts_the_office_key(monkeypatch, client):
    _enable(monkeypatch)
    refused = client.get(START, params={"k": OFFICE}, headers=HEADER)
    assert refused.status_code == 400
    assert refused.headers.get("location") is None
    assert "connect.squareup.com" not in refused.text
    assert client.get(START, params={"t": OFFICE}, headers=HEADER).status_code == 400

    _body, nonce = _issued_nonce(client)
    opened = client.get(
        START,
        params={"t": nonce, "k": OFFICE},
        headers={"X-DC-Office-Key": "wrong-key"},
        follow_redirects=False,
    )
    assert opened.status_code == 302
    location = opened.headers["location"]
    assert location.startswith("https://connect.squareup.com/oauth2/authorize?")
    assert OFFICE not in location
    assert "wrong-key" not in location
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert "k" not in query
    assert query["client_id"] == [APP_ID]


def test_link_nonce_is_hashed_and_replay_is_rejected(database, monkeypatch, client, caplog):
    _enable(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        body, nonce = _issued_nonce(client)
    assert body["url"].startswith(
        "https://driver-coach-backend-test.onrender.com/driver-coach/v1/square/oauth/start?t="
    )
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(body["url"]).query).keys() == {"t"}
    expires = datetime.fromisoformat(body["expiresAt"].replace("Z", "+00:00"))
    remaining = expires - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < remaining < timedelta(minutes=31)
    stored = _sql(database, "SELECT token_hash, used_at FROM square_oauth_link")
    assert stored[0][0] == hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    assert nonce not in stored[0][0]
    assert stored[0][1] is None
    _assert_absent(caplog.text, nonce, OFFICE)

    first = client.get(START, params={"t": nonce}, follow_redirects=False)
    assert first.status_code == 302
    again = client.get(START, params={"t": nonce}, follow_redirects=False)
    assert again.status_code == 400
    assert "already been used" in again.text
    assert again.headers.get("location") is None
    assert _sql(database, "SELECT count(*) FROM square_oauth_state")[0][0] == 1
    assert _sql(database, "SELECT count(*) FROM square_oauth_link WHERE used_at IS NOT NULL")[0][0] == 1


def test_link_nonce_expiry(database, monkeypatch, client):
    _enable(monkeypatch)
    _body, nonce = _issued_nonce(client)
    _sql(
        database,
        "UPDATE square_oauth_link SET expires_at = now() - interval '1 minute' WHERE token_hash = %s",
        (hashlib.sha256(nonce.encode("utf-8")).hexdigest(),),
    )
    expired = client.get(START, params={"t": nonce}, follow_redirects=False)
    assert expired.status_code == 400
    assert "expired" in expired.text
    assert expired.headers.get("location") is None
    assert _sql(database, "SELECT count(*) FROM square_oauth_state")[0][0] == 0


def test_pkce_callback_hides_tokens_and_rejects_state_replay(database, monkeypatch, client, caplog):
    _enable(monkeypatch)
    posts = []

    def post(url, payload, timeout=10):
        posts.append((url, dict(payload)))
        return {
            "access_token": AT,
            "refresh_token": RT,
            "expires_at": "2026-12-10T19:00:00Z",
            "merchant_id": "MERCHANT1",
        }

    monkeypatch.setattr(square_oauth, "_post_json", post)
    _body, nonce = _issued_nonce(client)
    with caplog.at_level(logging.DEBUG):
        started = client.get(START, params={"t": nonce}, follow_redirects=False)
    assert started.status_code == 302
    location = started.headers["location"]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert query["session"] == ["false"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [REDIRECT]
    assert set(query["scope"][0].split()) == {"PAYMENTS_READ", "EMPLOYEES_READ"}
    assert "WRITE" not in query["scope"][0]
    verifier = _sql(database, "SELECT code_verifier FROM square_oauth_state")[0][0]
    assert query["code_challenge"] == [square_oauth.code_challenge_s256(verifier)]
    _assert_absent(location, verifier, AT, RT, CODE)
    state = query["state"][0]

    with caplog.at_level(logging.DEBUG):
        done = client.get(CALLBACK, params={"code": CODE, "state": state})
    assert done.status_code == 200
    assert "Driver Coach now has read-only Square access" in done.text
    _assert_absent(done.text, AT, RT, CODE, verifier, OFFICE)
    _assert_absent(_app_log(caplog), AT, RT, CODE, verifier, OFFICE, nonce)
    assert posts[0][0] == "https://connect.squareup.com/oauth2/token"
    assert set(posts[0][1]) == {"client_id", "grant_type", "code", "redirect_uri", "code_verifier"}
    assert posts[0][1]["redirect_uri"] == REDIRECT
    assert posts[0][1]["code_verifier"] == verifier
    status = client.get(STATUS, headers=HEADER)
    assert status.json()["connected"] is True
    assert status.json()["merchantId"] == "MERCHANT1"
    assert status.json()["scopes"] == ["PAYMENTS_READ", "EMPLOYEES_READ"]
    assert status.json()["expiresAt"] == "2026-12-10T19:00:00Z"
    assert status.json()["lastRefreshAt"] is None
    assert status.json()["lastError"] is None
    _assert_absent(status.text, AT, RT, verifier)

    with caplog.at_level(logging.DEBUG):
        replay = client.get(CALLBACK, params={"code": "second-code", "state": state})
    assert replay.status_code == 400
    assert "already been used" in replay.text
    assert len(posts) == 1
    _assert_absent(replay.text, AT, RT, "second-code")


def test_state_expiry_does_not_exchange(database, monkeypatch, client):
    _enable(monkeypatch)
    posts = []
    monkeypatch.setattr(
        square_oauth,
        "_post_json",
        lambda url, payload, timeout=10: posts.append(payload),
    )
    _body, nonce = _issued_nonce(client)
    started = client.get(START, params={"t": nonce}, follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(started.headers["location"]).query)["state"][0]
    _sql(database, "UPDATE square_oauth_state SET expires_at = now() - interval '1 minute'")
    expired = client.get(CALLBACK, params={"code": CODE, "state": state})
    assert expired.status_code == 400
    assert "expired" in expired.text
    assert posts == []
    _assert_absent(expired.text, CODE)


def test_redirect_uri_override_is_sent_on_the_link_and_the_authorize_url(monkeypatch, client):
    _enable(monkeypatch)
    custom = "https://example.test/driver-coach/v1/square/oauth/callback"
    monkeypatch.setenv("SQUARE_OAUTH_REDIRECT_URI", custom)
    _body, nonce = _issued_nonce(client)
    assert _body["url"].startswith("https://example.test/driver-coach/v1/square/oauth/start?t=")
    started = client.get(START, params={"t": nonce}, follow_redirects=False)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(started.headers["location"]).query)
    assert query["redirect_uri"] == [custom]


def test_refresh_within_one_day_does_not_hold_the_row_lock(database, monkeypatch, client):
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    _seed(database, access="AT-old", refresh="RT-old", expires_at=now + timedelta(hours=12))
    posts = []

    def post(url, payload, timeout=10):
        posts.append(dict(payload))
        conn = psycopg2.connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{SCHEMA}"')
                cur.execute("SET lock_timeout = '500ms'")
                cur.execute(
                    "SELECT refresh_claim_id FROM square_oauth_token WHERE id = 1 FOR UPDATE NOWAIT"
                )
                claim = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        assert claim
        return {
            "access_token": "AT-new",
            "refresh_token": "RT-new",
            "expires_at": (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "merchant_id": "MERCHANT1",
        }

    monkeypatch.setattr(square_oauth, "_post_json", post)
    assert square_oauth.resolve_square_access_token() == "AT-new"
    assert len(posts) == 1
    assert set(posts[0]) == {"client_id", "grant_type", "refresh_token", "redirect_uri"}
    assert posts[0]["grant_type"] == "refresh_token"
    assert posts[0]["refresh_token"] == "RT-old"
    assert posts[0]["redirect_uri"] == REDIRECT
    stored = _sql(database, "SELECT refresh_token, refresh_claim_id FROM square_oauth_token")
    assert stored[0] == ("RT-new", None)
    status = client.get(STATUS, headers=HEADER).json()
    assert status["lastRefreshAt"]
    assert status["lastError"] is None

    square_oauth.clear_memory()
    _seed(database, access="AT-stable", refresh="RT-stable", expires_at=now + timedelta(days=10))
    before = len(posts)
    assert square_oauth.resolve_square_access_token() == "AT-stable"
    assert len(posts) == before


def test_refresh_rotation_race(database, monkeypatch, caplog):
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    _seed(database, access="AT-old", refresh="RT-old", expires_at=now + timedelta(days=20))

    class _Open:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(square_oauth, "_flight", _Open())
    arrived = threading.Event()
    real_wait = square_oauth._wait_for_refresh

    def wait(prior):
        arrived.set()
        return real_wait(prior)

    calls = []

    def post(url, payload, timeout=10):
        calls.append(payload["refresh_token"])
        assert "client_secret" not in payload
        assert arrived.wait(3)
        return {
            "access_token": "AT-rotated-value",
            "refresh_token": "RT-rotated-value",
            "expires_at": (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "merchant_id": "MERCHANT1",
        }

    monkeypatch.setattr(square_oauth, "_wait_for_refresh", wait)
    monkeypatch.setattr(square_oauth, "_post_json", post)
    errors = []
    results = []

    def work():
        try:
            results.append(square_oauth.ensure_fresh_token(force=True))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with caplog.at_level(logging.DEBUG):
        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == ["AT-rotated-value", "AT-rotated-value"]
    assert calls == ["RT-old"]
    assert _sql(database, "SELECT access_token, refresh_token, refresh_claim_id FROM square_oauth_token")[0] == (
        "AT-rotated-value",
        "RT-rotated-value",
        None,
    )
    _assert_absent(caplog.text, "AT-old", "RT-old", "AT-rotated-value", "RT-rotated-value")


def test_unauthorized_oauth_token_refreshes_once(database, monkeypatch, caplog):
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    _seed(database, access=AT, refresh=RT, expires_at=now + timedelta(days=20))
    posts = []

    def post(url, payload, timeout=10):
        posts.append(payload["grant_type"])
        return {
            "access_token": "AT-after-401",
            "refresh_token": "RT-after-401",
            "expires_at": (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "merchant_id": "MERCHANT1",
        }

    def urlopen(req, timeout=8):
        auth = req.get_header("Authorization")
        if auth == f"Bearer {AT}":
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", email.message.Message(), io.BytesIO(b"")
            )
        if "team-members" in req.full_url:
            return _Body({"team_members": [{"id": "tm-1", "given_name": "Amina", "family_name": "Cole"}]})
        return _Body({"payments": []})

    monkeypatch.setattr(square_oauth, "_post_json", post)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    scorecard.clear_sale_cache()
    with caplog.at_level(logging.DEBUG):
        payments, team = scorecard._cached_today_payments(now)
    assert payments == []
    assert team == {"tm-1": "Amina Cole"}
    assert posts == ["refresh_token"]
    _assert_absent(caplog.text, AT, RT, "AT-after-401", "RT-after-401")


def test_env_token_unauthorized_does_not_refresh(database, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "env-token")

    def refuse(url, payload, timeout=10):
        raise AssertionError("oauth refresh ran for an env token")

    def urlopen(req, timeout=8):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", email.message.Message(), io.BytesIO(b"")
        )

    monkeypatch.setattr(square_oauth, "_post_json", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    scorecard.clear_sale_cache()
    assert scorecard._cached_today_payments(datetime.now(timezone.utc)) == (None, None)


def test_oauth_token_beats_env_and_file(database, monkeypatch, tmp_path):
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    _seed(database, access="AT-db", refresh="RT-db", expires_at=now + timedelta(days=20))
    path = tmp_path / "softeedashboard.env"
    path.write_text('SQUARE_TOKEN="file-token"\n')
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "env-token")
    assert square_oauth.resolve_square_access_token(str(path)) == "AT-db"
    _sql(database, "DELETE FROM square_oauth_token")
    square_oauth.clear_memory()
    assert square_oauth.resolve_square_access_token(str(path)) == "env-token"
    monkeypatch.delenv("SQUARE_ACCESS_TOKEN", raising=False)
    assert square_oauth.resolve_square_access_token(str(path)) == "file-token"


def test_missing_table_is_503_and_token_resolution_falls_back(database, monkeypatch, tmp_path, client):
    _enable(monkeypatch)
    monkeypatch.setattr(square_oauth, "connect", _connect_factory(database, EMPTY))
    assert client.post(LINK, headers=HEADER).status_code == 503
    assert client.get(STATUS, headers=HEADER).status_code == 503
    assert client.get(START, params={"t": "anything"}).status_code == 503
    assert client.get(CALLBACK, params={"code": "x", "state": "y"}).status_code == 503
    path = tmp_path / "softeedashboard.env"
    path.write_text('SQUARE_TOKEN="file-token"\n')
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "env-token")
    assert square_oauth.resolve_square_access_token(str(path)) == "env-token"

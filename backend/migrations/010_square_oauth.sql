-- Square OAuth (PKCE) grants and one-time connect links.
-- Run with search_path set to the service's DRIVER_COACH_SCHEMA
-- (driver_coach in production, driver_coach_test for a pilot).
-- No CREATE SCHEMA, GRANT, role changes, or writes to public.
-- Access and refresh tokens live in square_oauth_token. The application
-- must never log those columns. square_oauth_link stores only a hash of
-- the one-time connect nonce.
DO $$
BEGIN
    IF current_schema() IS NULL OR current_schema() = 'public' THEN
        RAISE EXCEPTION 'a named Driver Coach schema is required';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS square_oauth_link (
    token_hash text PRIMARY KEY,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS square_oauth_state (
    state text PRIMARY KEY,
    code_verifier text NOT NULL,
    redirect_uri text NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS square_oauth_token (
    id smallint PRIMARY KEY DEFAULT 1,
    CONSTRAINT square_oauth_token_singleton CHECK (id = 1),
    access_token text NOT NULL,
    refresh_token text NOT NULL,
    expires_at timestamptz NOT NULL,
    merchant_id text,
    scopes text NOT NULL,
    redirect_uri text NOT NULL,
    last_refresh_at timestamptz,
    last_error text,
    refresh_claim_id text,
    refresh_claimed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Square offline periods. Additive only.
-- Run with search_path set to the service's DRIVER_COACH_SCHEMA
-- (driver_coach in production, driver_coach_test for a pilot).
-- No CREATE SCHEMA, GRANT, role changes, or writes to public.
-- A driver may mark Square offline more than once per day. Every period is kept.
-- The server closes an open row at SQUARE_OFFLINE_MAX_S (60 minutes).
DO $$
BEGIN
    IF current_schema() IS NULL OR current_schema() = 'public' THEN
        RAISE EXCEPTION 'a named Driver Coach schema is required';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS square_offline_period (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    truck_no integer NOT NULL,
    driver_id text,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    duration_s integer,
    allowed_s integer NOT NULL,
    end_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS square_offline_driver_started
    ON square_offline_period (driver_id, started_at DESC);
CREATE INDEX IF NOT EXISTS square_offline_session_started
    ON square_offline_period (session_id, started_at DESC);

-- Day lookup for the one-break rule. Skip when 007 has not been applied yet.
DO $$
BEGIN
    IF to_regclass('shift_break') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS shift_break_driver_started
            ON shift_break (driver_id, started_at DESC);
    END IF;
END $$;

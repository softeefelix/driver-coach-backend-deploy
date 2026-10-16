-- No-idle episodes and per-shift lunch/breaks.
-- Run with search_path set to the service's DRIVER_COACH_SCHEMA
-- (driver_coach in production, driver_coach_test for a pilot).
-- No CREATE SCHEMA, GRANT, role changes, or writes to public.
DO $$
BEGIN
    IF current_schema() IS NULL OR current_schema() = 'public' THEN
        RAISE EXCEPTION 'a named Driver Coach schema is required';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS idle_episode (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    truck_no integer NOT NULL,
    driver_id text,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    duration_s integer,
    lat double precision,
    lng double precision,
    at_stop boolean NOT NULL,
    stop_order integer,
    end_reason text,
    nudges jsonb NOT NULL DEFAULT '[]'::jsonb,
    nudge_since timestamptz,
    manager_alerted boolean NOT NULL DEFAULT false,
    manager_alerted_at timestamptz,
    manager_alert_for text
);
CREATE INDEX IF NOT EXISTS idle_episode_session_started
    ON idle_episode (session_id, started_at DESC);

CREATE TABLE IF NOT EXISTS shift_break (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    truck_no integer NOT NULL,
    driver_id text,
    break_type text NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    duration_s integer,
    allowed_s integer NOT NULL,
    end_reason text,
    overrun_s integer NOT NULL DEFAULT 0,
    flagged boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT shift_break_type_chk CHECK (break_type IN ('lunch', 'break'))
);
CREATE INDEX IF NOT EXISTS shift_break_session_started
    ON shift_break (session_id, started_at DESC);

CREATE TABLE IF NOT EXISTS shift_break_flag (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    truck_no integer NOT NULL,
    driver_id text,
    break_type text,
    break_id uuid,
    kind text NOT NULL,
    overrun_s integer,
    detail text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT shift_break_flag_kind_chk CHECK (kind IN ('over_limit', 'overrun'))
);
CREATE INDEX IF NOT EXISTS shift_break_flag_session_created
    ON shift_break_flag (session_id, created_at DESC);

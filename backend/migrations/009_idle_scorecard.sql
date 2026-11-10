-- Hermes idle scorecard totals. Additive only.
-- Run with search_path set to the service's DRIVER_COACH_SCHEMA
-- (driver_coach in production, driver_coach_test for a pilot).
-- No CREATE SCHEMA, GRANT, role changes, or writes to public.
DO $$
BEGIN
    IF current_schema() IS NULL OR current_schema() = 'public' THEN
        RAISE EXCEPTION 'a named Driver Coach schema is required';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS idle_scorecard (
    day date NOT NULL,
    driver_id text NOT NULL,
    truck_no integer,
    idle_minutes_today integer NOT NULL,
    idle_minutes_week integer NOT NULL,
    fleet_rank integer NOT NULL,
    fleet_size integer NOT NULL,
    clean_day_streak integer NOT NULL,
    as_of timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, driver_id)
);

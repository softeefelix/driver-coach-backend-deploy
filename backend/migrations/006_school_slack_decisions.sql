-- Run with search_path set to the service's already-owned DRIVER_COACH_SCHEMA
-- (driver_coach in production, driver_coach_test for the pilot).
-- No CREATE SCHEMA, GRANT, role changes, or writes to public.
DO $$
BEGIN
    IF current_schema() IS NULL OR current_schema() = 'public' THEN
        RAISE EXCEPTION 'a named Driver Coach schema is required';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS next_stop_decision (
    sequence bigserial PRIMARY KEY,
    decision_id uuid NOT NULL UNIQUE,
    session_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    inputs jsonb NOT NULL,
    state jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS next_stop_decision_session_sequence
    ON next_stop_decision (session_id, sequence DESC);

CREATE OR REPLACE FUNCTION reject_next_stop_decision_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'next_stop_decision is append-only';
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgrelid = 'next_stop_decision'::regclass
                     AND tgname = 'next_stop_decision_append_only') THEN
        CREATE TRIGGER next_stop_decision_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON next_stop_decision
        FOR EACH STATEMENT EXECUTE FUNCTION reject_next_stop_decision_mutation();
    END IF;
END $$;

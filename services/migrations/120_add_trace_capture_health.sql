-- Capture health is separate from chain integrity.
--
-- A hash chain proves that the events which were written have not been edited.
-- It cannot prove that every observed step was written: if the collector sees a
-- call, the insert fails, and later inserts succeed, the chain still verifies.
-- These columns let a run say "some steps may be missing" independently of
-- "the stored events verify".
ALTER TABLE agent_trace_runs
    ADD COLUMN IF NOT EXISTS capture_status text NOT NULL DEFAULT 'unknown'
        CHECK (capture_status IN ('ok', 'degraded', 'unknown')),
    ADD COLUMN IF NOT EXISTS capture_error text,
    ADD COLUMN IF NOT EXISTS dropped_events integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_event_at timestamptz;

-- Rows written before capture health was tracked cannot claim to be healthy.
UPDATE agent_trace_runs
SET capture_status = 'unknown'
WHERE capture_status = 'ok' AND last_event_at IS NULL;

UPDATE agent_trace_runs
SET last_event_at = COALESCE(last_event_at, ended_at, started_at)
WHERE last_event_at IS NULL;

CREATE INDEX IF NOT EXISTS agent_trace_runs_capture_idx
    ON agent_trace_runs (capture_status)
    WHERE capture_status <> 'ok';

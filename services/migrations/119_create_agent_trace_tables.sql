-- Per-turn execution tracing for the workbench chat.
--
-- One row per assistant attempt (a "run"), with an append-only, hash-chained
-- event ledger per run. The chain is scoped to the run rather than the whole
-- chat: a run is what a person opens, it can be executed concurrently with
-- other chats, and verification does not need to replay the chat's history.
CREATE TABLE IF NOT EXISTS agent_trace_runs (
    run_id text PRIMARY KEY,
    chat_id text NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id text NOT NULL,
    user_message_id text,
    assistant_message_id text,
    attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')),
    model_name text,
    error_code text,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    last_seq integer NOT NULL DEFAULT 0,
    head_digest text,
    seal_version text,
    seal text
);

CREATE INDEX IF NOT EXISTS agent_trace_runs_chat_started_idx
    ON agent_trace_runs (chat_id, started_at DESC);
CREATE INDEX IF NOT EXISTS agent_trace_runs_user_started_idx
    ON agent_trace_runs (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS agent_trace_runs_status_idx
    ON agent_trace_runs (status, started_at DESC);

CREATE TABLE IF NOT EXISTS agent_trace_events (
    run_id text NOT NULL REFERENCES agent_trace_runs(run_id) ON DELETE CASCADE,
    seq integer NOT NULL CHECK (seq > 0),
    event_id text NOT NULL,
    span_id text NOT NULL,
    parent_span_id text,
    type text NOT NULL,
    ts timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    previous_digest text,
    digest text NOT NULL,
    PRIMARY KEY (run_id, seq),
    CONSTRAINT agent_trace_events_event_id_key UNIQUE (run_id, event_id)
);

CREATE INDEX IF NOT EXISTS agent_trace_events_span_idx
    ON agent_trace_events (run_id, span_id);

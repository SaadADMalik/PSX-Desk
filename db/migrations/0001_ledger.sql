-- LLM call ledger (Slice 0). IF NOT EXISTS so databases created by the old
-- init script adopt this migration without change.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS llm_calls (
    id                bigserial PRIMARY KEY,
    created_at        timestamptz NOT NULL DEFAULT now(),
    trace_id          text,
    caller            text NOT NULL,
    lane              text NOT NULL,
    requested         text NOT NULL,
    model             text,
    outcome           text NOT NULL CHECK (outcome IN ('ok', 'cached', 'error')),
    attempts          int  NOT NULL,
    prompt_tokens     int  NOT NULL,
    completion_tokens int  NOT NULL,
    latency_ms        int  NOT NULL,
    cached            boolean NOT NULL,
    error             text
);
CREATE INDEX IF NOT EXISTS llm_calls_created_at ON llm_calls (created_at);
CREATE INDEX IF NOT EXISTS llm_calls_trace_id ON llm_calls (trace_id);

REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- The gateway may append to and read its ledger. Nothing else.
GRANT USAGE ON SCHEMA public TO gateway;
GRANT INSERT, SELECT ON llm_calls TO gateway;
GRANT USAGE ON SEQUENCE llm_calls_id_seq TO gateway;

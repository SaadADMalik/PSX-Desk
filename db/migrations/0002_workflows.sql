-- Durable workflows, transactional outbox and inbox (Slice 2).

-- One row per piece of work (a brief). `idempotency_key` is the business key:
-- the same request arriving twice, by any route, maps to the same run.
CREATE TABLE workflow_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    symbol          text NOT NULL,
    lane            text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    attempts        int  NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    -- Lease: which worker owns the run, until when, and a fencing token that
    -- changes on every claim so a stale worker's writes are rejected.
    lease_owner     text,
    lease_token     uuid,
    lease_until     timestamptz,
    trace_parent    text,
    result          jsonb,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX workflow_runs_runnable ON workflow_runs (next_attempt_at)
    WHERE status IN ('pending', 'running');

-- Checkpoints: a step that succeeded is never run again, even after a crash.
CREATE TABLE workflow_steps (
    run_id      uuid NOT NULL REFERENCES workflow_runs (id),
    step        text NOT NULL,
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    attempts    int  NOT NULL DEFAULT 0,
    result      jsonb,
    error       text,
    started_at  timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (run_id, step)
);

-- Transactional outbox: events are written in the same transaction as the state
-- change they describe, and a relay publishes them afterwards.
CREATE TABLE outbox (
    id           bigserial PRIMARY KEY,
    event_id     uuid NOT NULL UNIQUE,
    stream       text NOT NULL,
    event        jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- Inbox: which events a consumer has already handled (at-least-once delivery).
CREATE TABLE processed_events (
    consumer     text NOT NULL,
    event_id     uuid NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);

-- The orchestrator can create and update its own workflow state. No DELETE anywhere,
-- and no access to the LLM ledger.
GRANT USAGE ON SCHEMA public TO orchestrator;
GRANT SELECT, INSERT, UPDATE ON workflow_runs, workflow_steps, outbox TO orchestrator;
GRANT SELECT, INSERT ON processed_events TO orchestrator;
GRANT USAGE ON SEQUENCE outbox_id_seq TO orchestrator;

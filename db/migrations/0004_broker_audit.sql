-- Slice 4: the paper broker and the audit log.
--
-- The broker role owns trading state. Nobody else may write it: the orchestrator,
-- the agents and the market-data service have no grants on these tables at all.

-- ---------------------------------------------------------------- trading

CREATE TABLE proposals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_order_id text NOT NULL UNIQUE,          -- idempotency: one proposal per key
    run_id          text,                          -- the workflow run that proposed it
    proposed_by     text NOT NULL,                 -- proven caller identity
    symbol          text NOT NULL,
    side            text NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity        int  NOT NULL CHECK (quantity > 0),
    ref_price       numeric(14, 4) NOT NULL CHECK (ref_price > 0),  -- quote when proposed
    rationale       text NOT NULL,
    brief           jsonb NOT NULL,                -- what the human sees when deciding
    evidence_ids    text[] NOT NULL DEFAULT '{}',
    checks          jsonb NOT NULL,                -- pre-trade checks when proposed
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'rejected', 'expired', 'filled', 'failed')),
    decided_by      text,
    decided_at      timestamptz,
    decision_note   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL
);
CREATE INDEX proposals_pending ON proposals (created_at) WHERE status = 'pending';

CREATE TABLE fills (
    id          bigserial PRIMARY KEY,
    proposal_id uuid NOT NULL UNIQUE REFERENCES proposals (id),  -- at most one fill each
    symbol      text NOT NULL,
    side        text NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity    int  NOT NULL CHECK (quantity > 0),
    price       numeric(14, 4) NOT NULL CHECK (price > 0),
    fees        numeric(14, 2) NOT NULL CHECK (fees >= 0),
    filled_at   timestamptz NOT NULL DEFAULT now()
);

-- Cash is a ledger, not a balance column: the balance is the sum, and every change
-- has a reason and (for trades) the fill that caused it.
CREATE TABLE cash_ledger (
    id      bigserial PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    amount  numeric(16, 2) NOT NULL,
    reason  text NOT NULL,
    fill_id bigint REFERENCES fills (id)
);
INSERT INTO cash_ledger (amount, reason) VALUES (1000000, 'initial paper capital (PKR)');

-- ---------------------------------------------------------------- audit log
--
-- Append-only and tamper-evident:
--   * no role has UPDATE or DELETE, and a trigger refuses them even for the owner;
--   * writers get EXECUTE on audit_append() only, never INSERT on the table, so the
--     hash chain cannot be bypassed;
--   * each row's hash covers the previous row's hash, so editing or deleting any
--     row (by someone who disables the trigger) breaks every hash after it.
--     audit_verify() finds the first broken row.

CREATE TABLE audit_log (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL,
    actor     text NOT NULL,     -- who: a proven service identity or a named human
    action    text NOT NULL,     -- e.g. proposal.created, proposal.approved, order.filled
    subject   text NOT NULL,     -- what it was done to, e.g. proposal:<uuid>
    details   jsonb NOT NULL DEFAULT '{}',
    prev_hash text NOT NULL,
    hash      text NOT NULL
);

CREATE FUNCTION audit_hash(prev text, at timestamptz, actor text, action text,
                           subject text, details jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    -- Epoch microseconds, not at::text: text output depends on the session time zone.
    SELECT encode(sha256(convert_to(concat_ws('|', prev,
        (extract(epoch FROM at) * 1000000)::bigint::text,
        actor, action, subject, details::text), 'UTF8')), 'hex')
$$;

CREATE FUNCTION audit_append(p_actor text, p_action text, p_subject text, p_details jsonb)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    prev text;
    ts timestamptz := clock_timestamp();
    new_id bigint;
BEGIN
    -- One writer at a time, or two rows could chain from the same predecessor.
    PERFORM pg_advisory_xact_lock(hashtext('audit_log'));
    SELECT hash INTO prev FROM audit_log ORDER BY id DESC LIMIT 1;
    prev := coalesce(prev, repeat('0', 64));
    INSERT INTO audit_log (at, actor, action, subject, details, prev_hash, hash)
    VALUES (ts, p_actor, p_action, p_subject, coalesce(p_details, '{}'), prev,
            audit_hash(prev, ts, p_actor, p_action, p_subject, coalesce(p_details, '{}')))
    RETURNING id INTO new_id;
    RETURN new_id;
END $$;

CREATE FUNCTION audit_verify() RETURNS TABLE (checked bigint, first_bad_id bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    r audit_log%ROWTYPE;
    expect_prev text := repeat('0', 64);
    n bigint := 0;
BEGIN
    FOR r IN SELECT * FROM audit_log ORDER BY id LOOP
        n := n + 1;
        IF r.prev_hash <> expect_prev OR
           r.hash <> audit_hash(r.prev_hash, r.at, r.actor, r.action, r.subject, r.details) THEN
            RETURN QUERY SELECT n, r.id;
            RETURN;
        END IF;
        expect_prev := r.hash;
    END LOOP;
    RETURN QUERY SELECT n, NULL::bigint;
END $$;

CREATE FUNCTION audit_forbid() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END $$;
CREATE TRIGGER audit_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_forbid();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_forbid();

-- ---------------------------------------------------------------- grants

-- Functions are executable by PUBLIC by default; take that away first.
REVOKE ALL ON FUNCTION audit_append(text, text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION audit_verify() FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO broker;
GRANT SELECT, INSERT ON proposals, fills, cash_ledger TO broker;
GRANT UPDATE (status, decided_by, decided_at, decision_note) ON proposals TO broker;
GRANT USAGE ON SEQUENCE fills_id_seq, cash_ledger_id_seq TO broker;
GRANT SELECT ON audit_log TO broker;
GRANT EXECUTE ON FUNCTION audit_append(text, text, text, jsonb) TO broker;
GRANT EXECUTE ON FUNCTION audit_verify() TO broker;

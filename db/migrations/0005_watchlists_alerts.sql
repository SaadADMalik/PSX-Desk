-- Account settings: each user's watchlist and their price alerts.
--
-- Owned by the broker role, the account service: alerts watch positions, and a
-- position's cost and size live here. Every table is keyed by account, so each
-- user has their own watchlist and alerts; research stays shared per stock.

-- ---------------------------------------------------------------- watchlists

CREATE TABLE watchlists (
    account  text NOT NULL,
    symbol   text NOT NULL,
    seq      bigserial NOT NULL,   -- the order stocks were added (several share a now())
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, symbol)
);

-- An account that has had a watchlist set up. Seeding the defaults happens once, so
-- a user who removes every stock keeps an empty list instead of getting it back.
CREATE TABLE watchlist_accounts (
    account   text PRIMARY KEY,
    seeded_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- price alerts
--
-- A rule fires when the price CROSSES its line, not on every check below it:
--   armed ──(drop reaches the threshold)──► triggered ──(recovers past 80% of it)──► armed
-- Recovery must clear a buffer (hysteresis), so a price wobbling around the line does
-- not alert again and again. A rule ends when its window passes or the user cancels.

CREATE TABLE alert_rules (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account         text NOT NULL,
    symbol          text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('pct', 'pkr')),  -- % drop, or Rs lost
    threshold       numeric(16, 4) NOT NULL CHECK (threshold > 0),
    ref_price       numeric(14, 4) NOT NULL CHECK (ref_price > 0),  -- what the drop is from
    ref_basis       text NOT NULL CHECK (ref_basis IN ('avg_cost', 'price_when_set')),
    starts_at       timestamptz NOT NULL DEFAULT now(),
    ends_at         timestamptz,                                    -- NULL: no end
    state           text NOT NULL DEFAULT 'armed' CHECK (state IN ('armed', 'triggered')),
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'expired', 'cancelled', 'ended')),
    last_price      numeric(14, 4),
    last_checked_at timestamptz,
    times_triggered int NOT NULL DEFAULT 0,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (kind <> 'pct' OR threshold < 100),
    CHECK (ends_at IS NULL OR ends_at > starts_at)
);
CREATE INDEX alert_rules_active ON alert_rules (symbol) WHERE status = 'active';
CREATE INDEX alert_rules_account ON alert_rules (account, created_at DESC);

CREATE TABLE alert_events (
    id        bigserial PRIMARY KEY,
    rule_id   uuid NOT NULL REFERENCES alert_rules (id),
    account   text NOT NULL,
    symbol    text NOT NULL,
    kind      text NOT NULL CHECK (kind IN ('triggered', 'rearmed')),
    price     numeric(14, 4) NOT NULL,
    drop_pct  numeric(8, 2) NOT NULL,
    loss_pkr  numeric(16, 2),
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX alert_events_account ON alert_events (account, id DESC);

-- ---------------------------------------------------------------- grants

GRANT SELECT, INSERT, DELETE ON watchlists TO broker;
GRANT SELECT, INSERT ON watchlist_accounts TO broker;
GRANT SELECT, INSERT ON alert_rules TO broker;
GRANT UPDATE (state, status, last_price, last_checked_at, times_triggered) ON alert_rules
    TO broker;
GRANT SELECT, INSERT ON alert_events TO broker;
GRANT USAGE ON SEQUENCE alert_events_id_seq, watchlists_seq_seq TO broker;

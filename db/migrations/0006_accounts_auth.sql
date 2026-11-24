-- Many users, each with their own demo account.
--
--   * accounts, and an `account` column on every trading table (broker role). What
--     existed before belongs to the operator account.
--   * users and sessions (dashboard role): sign-up, password hashes and refresh tokens.
--     The broker never sees a password; the dashboard never sees a trade it could
--     change. Each service can touch only its own half.

-- ---------------------------------------------------------------- demo accounts

CREATE TABLE accounts (
    account         text PRIMARY KEY CHECK (account ~ '^[a-z0-9_.@-]{1,40}$'),
    initial_capital numeric(16, 2) NOT NULL CHECK (initial_capital > 0),
    opened_at       timestamptz NOT NULL DEFAULT now()
);

-- Everything traded so far was the operator's.
INSERT INTO accounts (account, initial_capital)
SELECT 'operator', coalesce((SELECT amount FROM cash_ledger ORDER BY id LIMIT 1), 1000000);

ALTER TABLE proposals   ADD COLUMN account text NOT NULL DEFAULT 'operator'
                        REFERENCES accounts (account);
ALTER TABLE fills       ADD COLUMN account text NOT NULL DEFAULT 'operator'
                        REFERENCES accounts (account);
ALTER TABLE cash_ledger ADD COLUMN account text NOT NULL DEFAULT 'operator'
                        REFERENCES accounts (account);
ALTER TABLE proposals   ALTER COLUMN account DROP DEFAULT;
ALTER TABLE fills       ALTER COLUMN account DROP DEFAULT;
ALTER TABLE cash_ledger ALTER COLUMN account DROP DEFAULT;

CREATE INDEX proposals_account ON proposals (account, created_at DESC);
CREATE INDEX fills_account ON fills (account, id);
CREATE INDEX cash_ledger_account ON cash_ledger (account);
CREATE INDEX watchlists_symbol ON watchlists (symbol);

GRANT SELECT, INSERT ON accounts TO broker;

-- ---------------------------------------------------------------- users (dashboard)

CREATE TABLE users (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email             text NOT NULL UNIQUE CHECK (email = lower(email) AND email LIKE '%_@_%'),
    name              text NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    password_hash     text NOT NULL,                       -- argon2id
    account           text NOT NULL UNIQUE CHECK (account ~ '^[a-z0-9_.@-]{1,40}$'),
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_login_at     timestamptz,
    email_verified_at timestamptz,                         -- set once email is verified
    disabled_at       timestamptz
);

-- Refresh tokens. Only a hash is stored. Each use replaces the token with a new one
-- in the same family; presenting a token that was already used means it was copied,
-- so the whole family (that sign-in, on every device it reached) is revoked.
CREATE TABLE sessions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users (id),
    family      uuid NOT NULL,
    token_hash  bytea NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    revoked_at  timestamptz,
    user_agent  text
);
CREATE INDEX sessions_family ON sessions (family);
CREATE INDEX sessions_user ON sessions (user_id);

GRANT USAGE ON SCHEMA public TO dashboard;
GRANT SELECT, INSERT ON users TO dashboard;
GRANT UPDATE (last_login_at, password_hash, email_verified_at, name) ON users TO dashboard;
GRANT SELECT, INSERT ON sessions TO dashboard;
GRANT UPDATE (used_at, revoked_at) ON sessions TO dashboard;

-- Row-level security on every table that holds an account's data.
--
-- The broker already filters every query by account. This makes Postgres enforce it
-- too, so a query that forgets its WHERE clause, or a bug that passes the wrong
-- account, cannot read or change another account's rows.
--
--   the broker, per transaction:  SELECT set_config('desk.account', '<account>', true)
--     ─► that account's rows only, for reads AND writes (USING + WITH CHECK)
--   system reads (who follows a stock, alert checks, the activity feed):
--                                 SELECT set_config('desk.read_all', 'on', true)
--     ─► SELECT across accounts; no write policy matches, so still nothing written
--        except to the one account set in desk.account
--   neither set (a forgotten context) ─► no rows at all: fails closed
--
-- `true` = transaction-local: the setting ends with the transaction, so a pooled
-- connection never carries one request's account into the next.
--
-- Applies to the broker role. The admin role that owns the tables and the SECURITY
-- DEFINER audit functions are not subject to it (audit_append must chain every row).

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['accounts', 'proposals', 'fills', 'cash_ledger', 'watchlists',
                             'watchlist_accounts', 'alert_rules', 'alert_events'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY own_account ON %I FOR ALL TO broker '
            'USING (account = current_setting(''desk.account'', true)) '
            'WITH CHECK (account = current_setting(''desk.account'', true))', t);
        EXECUTE format(
            'CREATE POLICY system_read ON %I FOR SELECT TO broker '
            'USING (current_setting(''desk.read_all'', true) = ''on'')', t);
    END LOOP;
END $$;

-- The audit log: an account sees its own entries; system reads see all. Rows are only
-- ever added through audit_append (SECURITY DEFINER), so no write policy is needed.
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY own_account ON audit_log FOR SELECT TO broker
    USING (details->>'account' = current_setting('desk.account', true));
CREATE POLICY system_read ON audit_log FOR SELECT TO broker
    USING (current_setting('desk.read_all', true) = 'on');

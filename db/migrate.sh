#!/bin/sh
# Versioned migrations, applied as the admin role by the one-shot `migrate` service.
#
#   * roles.psql runs every time (idempotent; sets passwords from the environment)
#   * NNNN_name.sql files run once each, in order. Each migration and its record in
#     schema_migrations commit in ONE transaction: a failed migration leaves no trace
#     and is retried on the next run.
set -eu

export PGHOST="${PGHOST:-postgres}" PGUSER="$POSTGRES_USER" PGDATABASE="$POSTGRES_DB" \
       PGPASSWORD="$POSTGRES_PASSWORD"

until pg_isready -q; do sleep 1; done

psql -v ON_ERROR_STOP=1 -q -c \
  "CREATE TABLE IF NOT EXISTS schema_migrations (
     version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"

psql -v ON_ERROR_STOP=1 -q \
     -v gateway_pw="$GATEWAY_DB_PASSWORD" \
     -v orchestrator_pw="$ORCHESTRATOR_DB_PASSWORD" \
     -v market_data_pw="$MARKET_DATA_DB_PASSWORD" \
     -v memory_pw="$MEMORY_DB_PASSWORD" \
     -v broker_pw="$BROKER_DB_PASSWORD" \
     -v dashboard_pw="$DASHBOARD_DB_PASSWORD" \
     -f /migrations/roles.psql

for f in /migrations/[0-9]*.sql; do
  v=$(basename "$f" .sql)
  if [ "$(psql -tAc "SELECT 1 FROM schema_migrations WHERE version = '$v'")" = "1" ]; then
    echo "migrate: $v already applied"
    continue
  fi
  echo "migrate: applying $v"
  psql -v ON_ERROR_STOP=1 -q --single-transaction \
       -f "$f" -c "INSERT INTO schema_migrations (version) VALUES ('$v')"
done
echo "migrate: done"

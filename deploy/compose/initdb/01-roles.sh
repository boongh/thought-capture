#!/usr/bin/env bash
# Provision the least-privilege application role at cluster creation.
#
# Runs once, as the superuser, before any migration. Table privileges are NOT
# granted here - migration 0001 owns those, so that the append-only grants stay
# versioned alongside the schema they protect (docs/DESIGN.md 6.2).
set -euo pipefail

if [[ -z "${TC_APP_DB_PASSWORD:-}" ]]; then
  echo "TC_APP_DB_PASSWORD is not set; refusing to create a passwordless login role" >&2
  exit 1
fi

# The password is passed as a psql variable and quoted with :'...' so it is
# never concatenated into the SQL text. \gexec makes role creation idempotent
# without a dollar-quoted DO block, inside which psql would not interpolate.
psql -v ON_ERROR_STOP=1 \
     -v pw="$TC_APP_DB_PASSWORD" \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" <<-'SQL'
	SELECT 'CREATE ROLE tc_app LOGIN'
	WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tc_app');
	\gexec

	ALTER ROLE tc_app LOGIN PASSWORD :'pw';

	-- The application role must never create objects of its own.
	REVOKE CREATE ON SCHEMA public FROM tc_app;
SQL

echo "role tc_app provisioned"

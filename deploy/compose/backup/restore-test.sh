#!/usr/bin/env bash
# Runs inside the `restore-test` one-shot job (deploy/compose/
# backup.restore-test.docker-compose.yml), against `postgres-scratch` - a
# throwaway Postgres that exists only for the lifetime of one restore-test
# run, under project name `thought-capture-restore-test`
# (deploy/compose/backup.restore-test.docker-compose.yml's own `name:`
# field) - never the real `thought-capture` project. This is deliberate
# structural isolation, not just naming: even a completely unscoped
# `docker compose down -v` run against this file's project could not reach
# the real dev stack's volumes, because they simply do not exist in this
# project. See docs/incidents/0001-docker-compose-down-deleted-the-real-
# dev-stack.md for why that distinction is the entire point.
#
# Validates, against the most recent backup.sh manifest found in the
# bind-mounted /backups directory (read-only here):
#   1. pg_restore succeeds inside a single transaction - any constraint
#      (including every foreign key) that cannot be satisfied fails the
#      whole restore, not a partial one silently missing rows.
#   2. Every table's restored row count matches what backup.sh recorded at
#      dump time (schema-generic - reads the same recorded list, not a
#      hardcoded one).
#   3. The dump file's own sha256 still matches what backup.sh recorded,
#      catching corruption of the backup at rest, not just a restore bug.
#   4. Every attachment file's sha256 still matches backup.sh's manifest,
#      the "blob hashes" docs/DESIGN.md 14.3 names for the eventual
#      quarterly drill.
#   5. Every row the RESTORED database's own `blobs` table names - not the
#      manifest - has a matching file in the backup with the right sha256
#      and size. This is deliberately independent of step 4: the manifest
#      is generated FROM whatever backup.sh actually copied, so a blob the
#      copy step silently omitted (a bug, a permissions error, an attachment
#      written between the copy and the manifest scan) would simply not
#      appear in the manifest either - step 4 would have nothing to
#      complain about, and restore-test would pass while the restored
#      `blobs`/`thought_attachments` rows point at a file that does not
#      exist anywhere in the backup. Querying the database itself is the
#      only source of truth for "what SHOULD be here," matching how the row
#      counts in step 2 already use the database's own bookkeeping.
#   6. The restored `tc_app` role's table/column grant catalog matches what
#      backup.sh recorded at dump time, AND `tc_app` can actually use those
#      grants (a live SELECT succeeds; a live INSERT on an identity table
#      and a live UPDATE on `thoughts` are both refused). A restore that
#      produces a working schema but a privilege-less or over-granted
#      `tc_app` is still a failed recovery - the catalog diff alone would
#      not catch a role that cannot log in or was granted the wrong thing
#      in a way that happens to match a plausible-looking JSON diff.
set -euo pipefail

backup_root="/backups"

if [[ ! -f "$backup_root/latest.txt" ]]; then
  echo "FAIL: no backup found at $backup_root/latest.txt - run the 'backup' service first" >&2
  exit 1
fi

manifest_json="$backup_root/$(cat "$backup_root/latest.txt")"
if [[ ! -f "$manifest_json" ]]; then
  echo "FAIL: $backup_root/latest.txt points at a missing manifest: $manifest_json" >&2
  exit 1
fi

json_field() {
  # Minimal, dependency-free extraction (no jq in postgres:18.6-trixie) -
  # backup.sh's own manifest.json is written by this same project in a
  # fixed, single-line-per-field shape, so a targeted grep/sed is exact
  # here without needing a general JSON parser.
  grep -oE "\"$1\": *\"?[^\",}]*\"?" "$manifest_json" | head -1 | sed -E 's/^"[^"]+": *"?([^"]*)"?$/\1/'
}

dump_file="$backup_root/postgres/$(json_field postgres_dump_file)"
expected_dump_sha256="$(json_field postgres_dump_sha256)"
row_counts_json="$backup_root/postgres/$(json_field postgres_row_counts_file)"
grants_json_field="$(json_field postgres_grants_file)"
attachments_manifest="$backup_root/$(json_field attachments_manifest_file)"

echo "--- verifying dump file integrity at rest"
actual_dump_sha256="$(sha256sum "$dump_file" | cut -d' ' -f1)"
if [[ "$actual_dump_sha256" != "$expected_dump_sha256" ]]; then
  echo "FAIL: $dump_file sha256 mismatch (expected $expected_dump_sha256, got $actual_dump_sha256) - the backup at rest is corrupt, restore would not be trustworthy" >&2
  exit 1
fi
echo "OK: dump file sha256 matches ($actual_dump_sha256)"

echo "--- restoring into postgres-scratch (single transaction: any constraint violation fails the whole restore)"
# Deliberately NOT --no-privileges any more (review finding): the dump now
# carries the ACLs backup.sh recorded (GRANT statements from migrations
# 0001-0008), and postgres-scratch provisions tc_app itself via
# initdb/01-roles.sh (deploy/compose/backup.restore-test.docker-compose.yml
# mounts it) before this restore runs, so those GRANT ... TO tc_app
# statements have a role to land on.
PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
  --host=postgres-scratch --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  --no-owner --single-transaction "$dump_file"
echo "OK: pg_restore succeeded (schema and every foreign key/constraint accepted)"

echo "--- comparing restored row counts to backup-time counts"
mismatch=0
while IFS= read -r pair; do
  [[ -z "$pair" ]] && continue
  table="${pair%%\":*}"
  table="${table#\"}"
  count="${pair##*:}"
  restored_count="$(PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 --quiet --tuples-only --no-align \
    --host=postgres-scratch --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
    -c "SELECT count(*) FROM \"$table\"")"
  if [[ "$restored_count" != "$count" ]]; then
    echo "FAIL: table \"$table\" row count mismatch (backed up $count, restored $restored_count)" >&2
    mismatch=1
  fi
done < <(grep -o '"[^"]*":[0-9]*' "$row_counts_json")

if [[ "$mismatch" -ne 0 ]]; then
  exit 1
fi
echo "OK: every table's restored row count matches the backup manifest"

echo "--- re-verifying attachment blob hashes"
if [[ -s "$attachments_manifest" ]]; then
  if ! (cd /backups/attachments && sha256sum --check --quiet "$attachments_manifest"); then
    echo "FAIL: one or more attachment files no longer match their backed-up sha256" >&2
    exit 1
  fi
  echo "OK: all attachment blob hashes match ($(wc -l <"$attachments_manifest" | tr -d ' ') files)"
else
  echo "OK: no attachments were present at backup time, nothing to verify"
fi

echo "--- verifying every database-referenced blob exists in the backup and matches"
blob_mismatch=0
blob_count=0
while IFS='|' read -r blob_sha256 size_bytes storage_key; do
  [[ -z "$blob_sha256" ]] && continue
  blob_count=$((blob_count + 1))
  blob_file="/backups/attachments/$storage_key"
  if [[ ! -f "$blob_file" ]]; then
    echo "FAIL: the restored database's blobs table references sha256=$blob_sha256 (storage_key=$storage_key), but no such file exists in the backup - the attachment copy silently omitted a blob the database still points at, which the manifest check above cannot catch (it only re-checks what WAS copied)" >&2
    blob_mismatch=1
    continue
  fi
  actual_size="$(stat -c%s "$blob_file")"
  if [[ "$actual_size" != "$size_bytes" ]]; then
    echo "FAIL: blob $blob_sha256 size mismatch (database says $size_bytes bytes, backup file $blob_file is $actual_size bytes)" >&2
    blob_mismatch=1
    continue
  fi
  actual_sha256="$(sha256sum "$blob_file" | cut -d' ' -f1)"
  if [[ "$actual_sha256" != "$blob_sha256" ]]; then
    echo "FAIL: file at $blob_file hashes to $actual_sha256, but the database's blobs row for it says $blob_sha256" >&2
    blob_mismatch=1
  fi
done < <(PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 --quiet --tuples-only --no-align -F'|' \
  --host=postgres-scratch --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  -c "SELECT sha256, size_bytes, storage_key FROM blobs ORDER BY sha256")

if [[ "$blob_mismatch" -ne 0 ]]; then
  exit 1
fi
echo "OK: every database-referenced blob ($blob_count) exists in the backup and matches its recorded hash/size"

echo "--- comparing tc_app's restored grant catalog to the backup-time catalog"
# A backup written before this check existed carries no grants file. That
# must be reported as "cannot validate", never silently treated as "nothing
# to compare" and passed - a check that quietly downgrades to a pass on
# missing data is how this class of gap reappears (review finding).
if [[ -z "$grants_json_field" ]]; then
  echo "FAIL: the backup manifest has no postgres_grants_file - this backup predates grant-catalog recording and tc_app's restored privileges cannot be validated" >&2
  exit 1
fi
grants_json="$backup_root/postgres/$grants_json_field"
if [[ ! -f "$grants_json" ]]; then
  echo "FAIL: manifest names postgres_grants_file=$grants_json_field but that file does not exist at $grants_json" >&2
  exit 1
fi
expected_grants="$(cat "$grants_json")"
restored_grants="$(PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 --quiet --tuples-only --no-align \
  --host=postgres-scratch --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" -c "
    SELECT json_build_object(
      'table_grants', (
        SELECT coalesce(json_agg(row_to_json(t)), '[]'::json)
        FROM (SELECT table_schema, table_name, privilege_type
              FROM information_schema.role_table_grants
              WHERE grantee = 'tc_app'
              ORDER BY table_schema, table_name, privilege_type) t
      ),
      'column_grants', (
        SELECT coalesce(json_agg(row_to_json(c)), '[]'::json)
        FROM (SELECT table_schema, table_name, column_name, privilege_type
              FROM information_schema.column_privileges
              WHERE grantee = 'tc_app'
              ORDER BY table_schema, table_name, column_name, privilege_type) c
      )
    )::text;
  ")"
if [[ "$restored_grants" != "$expected_grants" ]]; then
  echo "FAIL: tc_app's restored grant catalog does not match the one recorded at backup time" >&2
  printf 'expected: %s\n' "$expected_grants" >&2
  printf 'restored: %s\n' "$restored_grants" >&2
  exit 1
fi
echo "OK: tc_app's restored grant catalog matches the backup-time catalog"

echo "--- verifying tc_app's restored privileges are actually usable (live smoke test)"
if [[ -z "${TC_APP_DB_PASSWORD:-}" ]]; then
  echo "FAIL: TC_APP_DB_PASSWORD is not set - cannot connect as tc_app to prove its restored privileges work" >&2
  exit 1
fi

if ! PGPASSWORD="$TC_APP_DB_PASSWORD" psql -v ON_ERROR_STOP=1 --quiet \
  --host=postgres-scratch --username=tc_app --dbname="$POSTGRES_DB" \
  -c "SELECT count(*) FROM thoughts" >/dev/null; then
  echo "FAIL: tc_app could not SELECT from thoughts after restore" >&2
  exit 1
fi
echo "OK: tc_app can SELECT from thoughts"

# tc_app must never be able to mint identities (migration 0003: identity
# tables are read-only to it). psql's own exit code, not output parsing,
# decides pass/fail; the error text is only cross-checked to make sure the
# refusal happened for the intended reason (insufficient privilege), not
# some unrelated failure that would make this assertion pass for the wrong
# reason.
set +e
insert_output="$(PGPASSWORD="$TC_APP_DB_PASSWORD" psql --host=postgres-scratch --username=tc_app --dbname="$POSTGRES_DB" \
  -c "INSERT INTO users (id, display_name, created_at) VALUES (gen_random_uuid(), 'restore-test smoke insert', now())" 2>&1)"
insert_exit=$?
set -e
if [[ "$insert_exit" -eq 0 ]]; then
  echo "FAIL: tc_app was able to INSERT into users after restore - it must be read-only there" >&2
  exit 1
fi
if [[ "$insert_output" != *"permission denied"* ]]; then
  echo "FAIL: tc_app's INSERT into users failed for an unexpected reason (expected permission denied): $insert_output" >&2
  exit 1
fi
echo "OK: tc_app is correctly refused INSERT on users"

# tc_app has SELECT, INSERT on thoughts but no UPDATE at all (migration
# 0001/0003) - a captured thought is append-only, never editable by the
# capture role itself.
set +e
update_output="$(PGPASSWORD="$TC_APP_DB_PASSWORD" psql --host=postgres-scratch --username=tc_app --dbname="$POSTGRES_DB" \
  -c "UPDATE thoughts SET body = 'restore-test smoke tamper' WHERE id = -1" 2>&1)"
update_exit=$?
set -e
if [[ "$update_exit" -eq 0 ]]; then
  echo "FAIL: tc_app was able to UPDATE thoughts.body after restore - a thought must be append-only there" >&2
  exit 1
fi
if [[ "$update_output" != *"permission denied"* ]]; then
  echo "FAIL: tc_app's UPDATE of thoughts.body failed for an unexpected reason (expected permission denied): $update_output" >&2
  exit 1
fi
echo "OK: tc_app is correctly refused UPDATE on thoughts.body"

echo "OK: restore-test complete for $(basename "$manifest_json")"

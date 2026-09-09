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
attachments_manifest="$backup_root/$(json_field attachments_manifest_file)"

echo "--- verifying dump file integrity at rest"
actual_dump_sha256="$(sha256sum "$dump_file" | cut -d' ' -f1)"
if [[ "$actual_dump_sha256" != "$expected_dump_sha256" ]]; then
  echo "FAIL: $dump_file sha256 mismatch (expected $expected_dump_sha256, got $actual_dump_sha256) - the backup at rest is corrupt, restore would not be trustworthy" >&2
  exit 1
fi
echo "OK: dump file sha256 matches ($actual_dump_sha256)"

echo "--- restoring into postgres-scratch (single transaction: any constraint violation fails the whole restore)"
PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
  --host=postgres-scratch --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  --no-owner --no-privileges --single-transaction "$dump_file"
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

echo "OK: restore-test complete for $(basename "$manifest_json")"

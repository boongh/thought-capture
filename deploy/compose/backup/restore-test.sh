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

echo "OK: restore-test complete for $(basename "$manifest_json")"

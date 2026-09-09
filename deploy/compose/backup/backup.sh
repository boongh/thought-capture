#!/usr/bin/env bash
# Runs inside the `backup` one-shot compose service (postgres:18.6-trixie's
# own image, so pg_dump's version always matches the server it dumps).
#
# Writes to /backups, a bind-mounted HOST directory (deploy/compose/docker-
# compose.yml's `backup` service - not a Docker named volume like
# postgres-data/attachments). That is the point: a volume-scoped teardown
# (docs/incidents/0001-docker-compose-down-deleted-the-real-dev-stack.md)
# cannot touch data that was never inside a Docker volume to begin with.
#
# docs/DESIGN.md 14.3 names this project's full backup shape (nightly pg_dump,
# nightly attachment manifest/incremental copy, weekly encrypted off-site,
# monthly portable export, quarterly restore drill with retention tiers).
# This script and restore-test.sh build the mechanics the owner asked to
# bring in now - local dump, attachment manifest/copy, restrictive
# permissions, scratch restore validation. Scheduling (nightly/quarterly
# cron), off-site replication, retention tiers, and encryption key custody
# are deliberately NOT here - docs/DESIGN.md 19 leaves the off-site
# provider and retention tiers as separate, still-open owner decisions, and
# building them without that decision would mean guessing at it.
set -euo pipefail

backup_root="/backups"
attachments_source="/data/attachments"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

postgres_dir="$backup_root/postgres"
attachments_dir="$backup_root/attachments"
mkdir -p "$postgres_dir" "$attachments_dir"

echo "--- pg_dump (custom format, snapshot at $timestamp)"

# Custom format (-Fc) dumps through one transaction at one MVCC snapshot, so
# the output already reflects a single consistent point in time even though
# the source database keeps accepting writes throughout. "Atomic" here means
# the OUTPUT FILE itself never exists half-written where a reader could see
# it: pg_dump writes to a temp name, then `mv` renames it into place in one
# filesystem operation. A crash or kill mid-dump leaves an orphaned .tmp file
# and no partial `thought_capture-*.dump`, never a truncated one masquerading
# as complete.
dump_tmp="$postgres_dir/.tmp-$timestamp.dump"
dump_final="$postgres_dir/thought_capture-$timestamp.dump"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
  --host=postgres --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  --format=custom --no-owner --no-privileges --file="$dump_tmp"
chmod 600 "$dump_tmp"
mv "$dump_tmp" "$dump_final"
dump_sha256="$(sha256sum "$dump_final" | cut -d' ' -f1)"

echo "--- recording per-table row counts (schema-generic, not a hardcoded table list)"
# Queries information_schema rather than naming tables, so this does not go
# stale as future migrations add tables - restore-test.sh compares against
# whatever this actually recorded, not a list maintained by hand in two
# places.
row_counts_json="$postgres_dir/thought_capture-$timestamp.row-counts.json"
PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 --quiet --tuples-only --no-align \
  --host=postgres --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name" \
  >"$postgres_dir/.tmp-tables-$timestamp.txt"

{
  printf '{'
  first=true
  while IFS= read -r table; do
    [[ -z "$table" ]] && continue
    count="$(PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 --quiet --tuples-only --no-align \
      --host=postgres --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
      -c "SELECT count(*) FROM \"$table\"")"
    if [[ "$first" == true ]]; then
      first=false
    else
      printf ','
    fi
    printf '"%s":%s' "$table" "$count"
  done <"$postgres_dir/.tmp-tables-$timestamp.txt"
  printf '}'
} >"$row_counts_json"
rm -f "$postgres_dir/.tmp-tables-$timestamp.txt"
chmod 600 "$row_counts_json"

echo "--- attachments: incremental copy + sha256 manifest"
# `cp -au`: only copies a file when it is missing from the destination or
# newer than the destination's copy - an incremental copy without depending
# on rsync, which the postgres:18.6-trixie image does not ship.
mkdir -p "$attachments_dir"
if [[ -d "$attachments_source" ]] && [[ -n "$(ls -A "$attachments_source" 2>/dev/null)" ]]; then
  cp -au "$attachments_source"/. "$attachments_dir"/
fi
manifest_file="$attachments_dir/../attachments-$timestamp.sha256"
if [[ -n "$(find "$attachments_dir" -type f -print -quit 2>/dev/null)" ]]; then
  (cd "$attachments_dir" && find . -type f -print0 | sort -z | xargs -0 sha256sum) >"$manifest_file"
else
  : >"$manifest_file"
fi
chmod 600 "$manifest_file"

echo "--- writing backup manifest and restrictive permissions"
manifest_json="$backup_root/thought_capture-$timestamp.manifest.json"
attachment_file_count="$(wc -l <"$manifest_file" | tr -d ' ')"
cat >"$manifest_json" <<JSON
{
  "timestamp": "$timestamp",
  "postgres_dump_file": "$(basename "$dump_final")",
  "postgres_dump_sha256": "$dump_sha256",
  "postgres_row_counts_file": "$(basename "$row_counts_json")",
  "attachments_manifest_file": "$(basename "$manifest_file")",
  "attachments_file_count": $attachment_file_count
}
JSON
chmod 600 "$manifest_json"
printf '%s\n' "$(basename "$manifest_json")" >"$backup_root/latest.txt"
chmod 600 "$backup_root/latest.txt"

# Best-effort: a Windows bind mount (Docker Desktop/WSL2) does not enforce
# POSIX permission bits the way a native Linux host filesystem does, so this
# is defense-in-depth for Linux deployment (docs/DESIGN.md 12.2's "encrypt
# host disks where available" neighbourhood), not something this script can
# guarantee cross-platform. Never fails the backup over a chmod that the
# underlying filesystem silently ignored.
chmod 700 "$backup_root" "$postgres_dir" "$attachments_dir" 2>/dev/null || true
find "$backup_root" -maxdepth 2 -type f -exec chmod 600 {} + 2>/dev/null || true

echo "OK: backup complete -> $(basename "$manifest_json")"

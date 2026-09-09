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

# Run as the image's own built-in non-root `postgres` user (uid 999), not
# root (review finding) - matching this project's own convention (every
# first-party image ends in `USER tc`; docs/DESIGN.md 12.4/12.2's "reduce
# blast radius"). The stock postgres:18.6-trixie image (used directly here,
# not one of this project's own built images) still starts as root by
# default because deploy/compose/docker-compose.yml's `backup` service
# overrides `entrypoint:` to run this script directly - bypassing the
# official image's own entrypoint script, which does this exact same
# root -> postgres drop internally via `gosu`, but only around the
# long-running SERVER process, never a one-shot script run this way.
#
# Root does exactly one thing before handing off: fix ownership of the
# bind-mounted TC_BACKUP_ROOT host directory, which Compose/Docker creates
# fresh and root-owned the first time it is mounted if it does not already
# exist - a non-root process could never write there otherwise. Only the
# mount point itself needs this, not a recursive `chown`: every file and
# subdirectory this script goes on to create will already be owned by
# `postgres`, since that is the uid that creates them, from here on.
# `gosu`, not `su`/`sudo`: the same tool the official image's own
# entrypoint already uses for this exact purpose, already present in this
# image - re-execs this same script as `postgres` and never returns.
# `gosu postgres /bin/bash "$0"`, not `gosu postgres "$0"` (review finding):
# this script is bind-mounted read-only (deploy/compose/docker-compose.yml),
# and a Windows host (Docker Desktop/WSL2, see the chmod comment near the
# bottom of this file) does not reliably preserve the executable bit across
# that mount even when it is set in the repo - `gosu postgres "$0"` asks the
# kernel to exec the file directly and fails with "Permission denied" the
# moment that bit is missing. Naming `/bin/bash` explicitly runs this
# script's own text through the interpreter instead of relying on the
# filesystem's executable bit or its shebang line.
if [[ "$(id -u)" -eq 0 ]]; then
  mkdir -p "$backup_root"
  chown postgres:postgres "$backup_root"

  # Copy attachments while still root (review finding): the application
  # container writes attachments as uid 10001 via `tempfile.mkstemp`
  # (packages/infrastructure/src/tc_infrastructure/storage/blob_store.py),
  # which creates files mode 0600 owned by that uid - unreadable to the
  # `postgres` user (uid 999) this script drops to below. Root can read any
  # file regardless of owner, so this narrowly-scoped step runs once here,
  # before the privilege drop, and only ever hands the postgres user the
  # resulting copies (chowned immediately after), never broader access to
  # the source attachments tree itself.
  attachments_dir="$backup_root/attachments"
  mkdir -p "$attachments_dir"
  if [[ -d "$attachments_source" ]] && [[ -n "$(ls -A "$attachments_source" 2>/dev/null)" ]]; then
    cp -au "$attachments_source"/. "$attachments_dir"/
  fi
  chown -R postgres:postgres "$attachments_dir"

  exec gosu postgres /bin/bash "$0" "$@"
fi

mkdir -p "$backup_root"

# Two concurrent invocations (a scheduled run overlapping a manual one, or
# two manual runs) each write their own uniquely-timestamped dump/manifest/
# row-counts files, so those never collide - but both would write the SAME
# `latest.txt`, and interleaved writes to one file from two processes can
# corrupt it (a reader could see a mix of both runs' bytes). `flock` here
# serializes the two runs entirely rather than only guarding the final
# `latest.txt` write: a second run waits for the first to finish instead of
# running pg_dump against the source concurrently for no benefit. Bounded
# wait, not indefinite - a genuinely stuck prior run should surface as a
# clear failure here, not an invisible hang.
lock_file="$backup_root/.backup.lock"
exec 200>"$lock_file"
if ! flock -w 300 200; then
  echo "FAIL: could not acquire the backup lock ($lock_file) within 300s - another backup may be stuck" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

postgres_dir="$backup_root/postgres"
attachments_dir="$backup_root/attachments"
mkdir -p "$postgres_dir" "$attachments_dir"

# ---------------------------------------------------------------------------
# One long-lived session, held open across pg_dump and the row-count/grant
# queries below, exists to close a real race (review finding): pg_dump takes
# its own MVCC snapshot the instant it starts, but the OLD code then counted
# rows through SEPARATE, later connections - each with its own, later
# snapshot. A capture committed in the gap between them made the recorded
# count higher than what the dump actually held, and restore-test.sh would
# fail a perfectly valid backup. Exporting this session's snapshot with
# pg_export_snapshot() and handing its id to `pg_dump --snapshot=<id>` makes
# pg_dump read through THIS session's transaction instead of opening its own -
# so the dump, the row counts, and the tc_app grant catalog recorded below
# all observe the exact same consistent point in time, no matter what commits
# afterward. If this session dies or the export fails, the backup must fail
# outright - falling back to post-hoc counting would silently reintroduce the
# race this exists to close.
# ---------------------------------------------------------------------------
echo "--- exporting a snapshot for a consistent dump + row-count/grant capture"

coproc PSQL {
  PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 --no-align --tuples-only --quiet \
    --host=postgres --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"
}

snapshot_sentinel="__TC_BACKUP_SENTINEL__"

# Sends one SQL statement to the still-open coprocess session and stores its
# tuples-only, unaligned output into the caller-named variable $1. The
# `\echo` sentinel after every statement is how the reader knows where that
# statement's output ends - psql's own output stream has no other reliable
# end-of-result marker in this mode. Returns non-zero on EOF before the
# sentinel, which only happens if the session itself died (e.g. ON_ERROR_STOP
# aborted it on a failed statement) - never a valid empty result.
#
# Deliberately writes into a named variable (`printf -v`) rather than
# printing to stdout for the caller to capture via `$(psql_query ...)`:
# command substitution and pipelines both fork a SUBSHELL, and confirmed
# directly while testing this - a bash coprocess's file descriptors go bad
# ("Bad file descriptor") the moment a subshell forked from a call already
# past the coproc's own statement tries to use them again. Every use of
# ${PSQL[0]}/${PSQL[1]} in this script must therefore happen in the main
# shell, never inside `$(...)` or a `| pipe`.
psql_query() {
  local __psql_query_outvar="$1"
  local sql="$2"
  printf '%s\n' "$sql" >&"${PSQL[1]}"
  # `"\echo $snapshot_sentinel"` (a single literal backslash inside a
  # double-quoted string), NOT a `\\echo` sequence embedded in printf's own
  # FORMAT string - confirmed directly while testing this: printf's format
  # string interprets ITS OWN backslash escapes, and `\\e` there decodes to
  # ESC (0x1B) + "cho" rather than a literal `\echo`, which silently sent
  # `cho <sentinel>` to psql as SQL instead of the intended meta-command and
  # hung this function waiting for a sentinel line that would never arrive.
  # Passing the already-correct text through `%s` sidesteps format-string
  # escape processing entirely.
  printf '%s\n' "\echo $snapshot_sentinel" >&"${PSQL[1]}"
  local line
  local result=""
  while IFS= read -r line <&"${PSQL[0]}"; do
    if [[ "$line" == "$snapshot_sentinel" ]]; then
      printf -v "$__psql_query_outvar" '%s' "$result"
      return 0
    fi
    result+="$line"$'\n'
  done
  return 1
}

psql_session_failed=0
close_psql_session() {
  # ${PSQL[1]} holds a plain fd number - "N>&-" closes the fd whose number
  # is that word, which bash expands before parsing the redirection; the
  # `{name}` brace form (no `$`) is a different thing (auto-assigning a new
  # fd to a variable) and is not what closes an already-known fd number.
  eval "exec ${PSQL[1]}>&-" 2>/dev/null || true
  if [[ -n "${PSQL_PID:-}" ]] && kill -0 "$PSQL_PID" 2>/dev/null; then
    wait "$PSQL_PID" 2>/dev/null || psql_session_failed=1
  fi
}
trap close_psql_session EXIT

_discard=""
if ! psql_query _discard "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;"; then
  echo "FAIL: could not start the snapshot-export transaction" >&2
  exit 1
fi

snapshot_id_raw=""
if ! psql_query snapshot_id_raw "SELECT pg_export_snapshot();"; then
  echo "FAIL: pg_export_snapshot() failed - refusing to fall back to a non-consistent backup" >&2
  exit 1
fi
snapshot_id="$(printf '%s' "$snapshot_id_raw" | tr -d '[:space:]')"
if [[ -z "$snapshot_id" ]]; then
  echo "FAIL: pg_export_snapshot() returned no snapshot id" >&2
  exit 1
fi
echo "snapshot exported: $snapshot_id"

echo "--- pg_dump (custom format, at snapshot $snapshot_id)"

# Custom format (-Fc) reads through the imported snapshot above, so the
# output reflects that exact consistent point in time even though the source
# database keeps accepting writes throughout. "Atomic" here means the OUTPUT
# FILE itself never exists half-written where a reader could see it: pg_dump
# writes to a temp name, then `mv` renames it into place in one filesystem
# operation. A crash or kill mid-dump leaves an orphaned .tmp file and no
# partial `thought_capture-*.dump`, never a truncated one masquerading as
# complete.
#
# `--no-owner` (the restoring role owns the objects, which is what a
# recovery host wants) but deliberately NOT `--no-privileges` any more
# (review finding): stripping privileges from the dump meant a restored
# `tc_app` had a login but zero table grants - `alembic_version` restores at
# head, so `alembic upgrade head` after a restore is a no-op and never
# replays the GRANT statements migrations 0001-0008 issued. Letting the dump
# carry whatever ACLs the migrations actually produced keeps one source of
# truth instead of a second, hand-maintained copy that could drift silently.
dump_tmp="$postgres_dir/.tmp-$timestamp.dump"
dump_final="$postgres_dir/thought_capture-$timestamp.dump"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
  --host=postgres --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  --format=custom --no-owner --snapshot="$snapshot_id" --file="$dump_tmp"
chmod 600 "$dump_tmp"
mv "$dump_tmp" "$dump_final"
dump_sha256="$(sha256sum "$dump_final" | cut -d' ' -f1)"

echo "--- recording per-table row counts (schema-generic, not a hardcoded table list; same snapshot as the dump)"
# One statement, in the still-open snapshot-holding session: Postgres builds
# and escapes the JSON itself (json_object_agg), so this is correct by
# construction and does not go stale as future migrations add tables -
# restore-test.sh compares against whatever this actually recorded, not a
# list maintained by hand in two places.
row_counts_json_text=""
if ! psql_query row_counts_json_text "
  SELECT coalesce(json_object_agg(table_name, row_count)::text, '{}')
  FROM (SELECT c.relname AS table_name,
               (xpath('/row/cnt/text()',
                      query_to_xml(format('SELECT count(*) AS cnt FROM %I.%I', n.nspname, c.relname),
                                   false, true, '')))[1]::text::bigint AS row_count
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY c.relname) s;
"; then
  echo "FAIL: could not record row counts from the snapshot session" >&2
  exit 1
fi
row_counts_json="$postgres_dir/thought_capture-$timestamp.row-counts.json"
printf '%s\n' "$row_counts_json_text" >"$row_counts_json"
chmod 600 "$row_counts_json"

echo "--- recording tc_app's grant catalog (same snapshot as the dump)"
# A restore that produces a working schema but an unusable application role
# is still a failed recovery (review finding). Recorded from the SAME
# snapshot session as the dump and row counts above, so restore-test.sh's
# comparison is never checking against a catalog read at a different moment
# than what was actually dumped.
grants_json_text=""
if ! psql_query grants_json_text "
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
"; then
  echo "FAIL: could not record tc_app's grant catalog from the snapshot session" >&2
  exit 1
fi
grants_json="$postgres_dir/thought_capture-$timestamp.grants.json"
printf '%s\n' "$grants_json_text" >"$grants_json"
chmod 600 "$grants_json"

_discard=""
if ! psql_query _discard "COMMIT;"; then
  echo "FAIL: could not commit the snapshot-export session cleanly" >&2
  exit 1
fi
close_psql_session
trap - EXIT
if [[ "$psql_session_failed" -ne 0 ]]; then
  echo "FAIL: the snapshot-export session exited with an error" >&2
  exit 1
fi

echo "--- attachments: sha256 manifest"
# The incremental copy itself already happened above, while still root
# (review finding: attachments are owned by uid 10001, the application
# container's user, and are unreadable to the `postgres` user this section
# now runs as - only root could read them). Only the manifest is written
# here, over whatever `$attachments_dir` holds as of that root-owned copy.
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
  "postgres_grants_file": "$(basename "$grants_json")",
  "attachments_manifest_file": "$(basename "$manifest_file")",
  "attachments_file_count": $attachment_file_count
}
JSON
chmod 600 "$manifest_json"

# Same atomic write-then-rename pattern as the dump file above, and for the
# same reason: `latest.txt` is the ONE file every run overwrites in place
# (dump/manifest/row-counts are each uniquely timestamped, so they never
# collide even without the lock above). A reader (restore-test.sh, or an
# operator) must never be able to observe a half-written pointer - the
# flock above already prevents two backup.sh runs from doing this to each
# other, but a direct write here would still leave a window where a reader
# with no lock of its own could see a torn file if it happened to read at
# the exact wrong instant.
latest_tmp="$backup_root/.tmp-latest.txt"
printf '%s\n' "$(basename "$manifest_json")" >"$latest_tmp"
chmod 600 "$latest_tmp"
mv "$latest_tmp" "$backup_root/latest.txt"

# Best-effort: a Windows bind mount (Docker Desktop/WSL2) does not enforce
# POSIX permission bits the way a native Linux host filesystem does, so this
# is defense-in-depth for Linux deployment (docs/DESIGN.md 12.2's "encrypt
# host disks where available" neighbourhood), not something this script can
# guarantee cross-platform. Never fails the backup over a chmod that the
# underlying filesystem silently ignored.
chmod 700 "$backup_root" "$postgres_dir" "$attachments_dir" 2>/dev/null || true
find "$backup_root" -maxdepth 2 -type f -exec chmod 600 {} + 2>/dev/null || true

echo "OK: backup complete -> $(basename "$manifest_json")"

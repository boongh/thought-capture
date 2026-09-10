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

# Two phases, two users, one process tree.
#
# The DB phase runs as the image's own built-in non-root `postgres` user
# (uid 999), not root (review finding) - matching this project's own
# convention (every first-party image ends in `USER tc`; docs/DESIGN.md
# 12.4/12.2's "reduce blast radius"). The stock postgres:18.6-trixie image
# (used directly here, not one of this project's own built images) still
# starts as root by default because deploy/compose/docker-compose.yml's
# `backup` service overrides `entrypoint:` to run this script directly -
# bypassing the official image's own entrypoint script, which does this exact
# same root -> postgres drop internally via `gosu`, but only around the
# long-running SERVER process, never a one-shot script run this way.
#
# The attachment copy has to stay root's job: the application container writes
# attachments as uid 10001 via `tempfile.mkstemp`
# (packages/infrastructure/src/tc_infrastructure/storage/blob_store.py), which
# creates files mode 0600 owned by that uid - unreadable to uid 999. Only root
# can read them, and only the postgres user is ever handed the resulting
# copies, never broader access to the source attachments tree itself.
#
# ORDERING (review finding F3) - this is the part that changed, and the
# comment that used to sit here said the opposite, so read this rather than
# remembering the old shape. The copy must start AFTER the DB phase has
# exported its MVCC snapshot, not before:
#
#   - `BlobStore.put` fsyncs the blob file and only then commits the `blobs`
#     row, so every row visible in the snapshot already has its file on disk
#     at the snapshot instant;
#   - therefore a copy taken after the snapshot is a superset of what the dump
#     references, and files written afterwards are harmless extras;
#   - the old order (copy, then snapshot) had the opposite property: a blob
#     written and committed in the window between them was in the dump but not
#     in the backup directory. restore-test.sh's "every `blobs` row has a
#     backing file" step catches that on the restore side, but the backup
#     itself reported success.
#
# So root does NOT `exec` into the DB phase any more; it forks it and keeps
# running, and the two synchronise over two FIFOs in a private directory:
# the child signals "snapshot exported", root then copies (concurrently with
# pg_dump, so no added wall-clock cost) and signals "copy complete", and the
# child blocks on that before building the attachment manifest - so the
# manifest still describes exactly what was copied. Root `wait`s on the child
# and propagates its exit status.
#
# Rejected alternative, recorded so it is not rediscovered: drop to uid 10001
# for the whole script (`gosu 10001:10001`, numeric, no passwd entry needed).
# That uid can read the attachments AND run pg_dump/psql as clients, removing
# the root phase and this handshake entirely - genuinely simpler. It is
# rejected because the ownership of /backups is load-bearing elsewhere:
# backup.restore-test.docker-compose.yml's `restore-test` service runs as
# `user: postgres` and its comments encode uid 999, and
# scripts/check-backup-restore.sh's permission assertion checks for uid 999
# explicitly. Changing that is a separate, deliberate change, not a side
# effect of an ordering fix.
#
# `gosu`, not `su`/`sudo`: the same tool the official image's own entrypoint
# already uses for this exact purpose, already present in this image.
# `gosu postgres /bin/bash "$0"`, not `gosu postgres "$0"` (review finding):
# this script is bind-mounted read-only (deploy/compose/docker-compose.yml),
# and a Windows host (Docker Desktop/WSL2, see the chmod comment near the
# bottom of this file) does not reliably preserve the executable bit across
# that mount even when it is set in the repo - `gosu postgres "$0"` asks the
# kernel to exec the file directly and fails with "Permission denied" the
# moment that bit is missing. Naming `/bin/bash` explicitly runs this
# script's own text through the interpreter instead of relying on the
# filesystem's executable bit or its shebang line.
db_phase=0
if [[ "${1:-}" == "--db-phase" ]]; then
  db_phase=1
  shift
fi

if [[ "$(id -u)" -eq 0 ]] && [[ "$db_phase" -eq 0 ]]; then
  mkdir -p "$backup_root"
  chown postgres:postgres "$backup_root"

  attachments_dir="$backup_root/attachments"
  mkdir -p "$attachments_dir"
  chown postgres:postgres "$attachments_dir"

  # The lock now lives in the ROOT phase (review finding F3): it has to cover
  # the attachment copy as well as the DB phase, and the copy happens here.
  # Same bounded wait and same reasoning as before - a genuinely stuck prior
  # run should surface as a clear failure, not an invisible hang. The child
  # inherits this descriptor and so runs inside the same lock.
  lock_file="$backup_root/.backup.lock"
  exec 200>"$lock_file"
  if ! flock -w 300 200; then
    echo "FAIL: could not acquire the backup lock ($lock_file) within 300s - another backup may be stuck" >&2
    exit 1
  fi

  # Private, root-owned handshake directory; mode 711 so the child (uid 999)
  # can traverse into it without being able to list it, and the two FIFOs are
  # handed to that uid individually. Nothing but two one-line signals ever
  # crosses it.
  handshake_dir="$(mktemp -d)"
  chmod 711 "$handshake_dir"
  snapshot_ready_fifo="$handshake_dir/snapshot-ready"
  copy_complete_fifo="$handshake_dir/copy-complete"
  mkfifo -m 600 "$snapshot_ready_fifo" "$copy_complete_fifo"
  chown postgres:postgres "$snapshot_ready_fifo" "$copy_complete_fifo"

  # Opened read-WRITE (`<>`), not read-only. Opening a FIFO read-only blocks
  # until a writer appears, and that open happens BEFORE `read -t` starts
  # counting - so a `read -t` on a read-only open is not bounded at all.
  # Holding both ends here makes every open non-blocking and makes the
  # timeouts below real.
  exec 3<>"$snapshot_ready_fifo"
  exec 4<>"$copy_complete_fifo"

  root_cleanup() {
    rm -rf "$handshake_dir"
  }
  trap root_cleanup EXIT

  TC_BACKUP_HANDSHAKE_DIR="$handshake_dir" gosu postgres /bin/bash "$0" --db-phase "$@" &
  db_phase_pid=$!

  # Bounded wait, in slices, so a child that dies before signalling fails the
  # backup promptly instead of holding root for the full budget. `flock -w
  # 300` above already sets the precedent for that budget.
  snapshot_signal=""
  waited=0
  while [[ "$waited" -lt 300 ]]; do
    if read -r -t 5 -u 3 snapshot_signal; then
      break
    fi
    if ! kill -0 "$db_phase_pid" 2>/dev/null; then
      wait "$db_phase_pid" 2>/dev/null || true
      echo "FAIL: the database phase exited before exporting a snapshot - refusing to produce a backup whose attachment copy has no snapshot to be ordered against" >&2
      exit 1
    fi
    waited=$((waited + 5))
  done
  if [[ -z "$snapshot_signal" ]]; then
    kill "$db_phase_pid" 2>/dev/null || true
    wait "$db_phase_pid" 2>/dev/null || true
    echo "FAIL: the database phase did not signal 'snapshot exported' within 300s" >&2
    exit 1
  fi

  echo "--- attachments: copying (started after the snapshot instant, so the copy is a superset of what the dump references)"
  if [[ -d "$attachments_source" ]] && [[ -n "$(ls -A "$attachments_source" 2>/dev/null)" ]]; then
    # `.incoming-*` excluded deliberately: those are `BlobStore.put`'s
    # in-flight temp files (mkstemp with that prefix, directly under the store
    # root). They are never referenced by a `blobs` row, and copying one
    # mid-write would put a torn file into the manifest.
    #
    # A filtered file list fed to `cp -au --parents` rather than a plain
    # `cp -au source/. dest/`: `cp` has no exclude option, and `-au` is worth
    # keeping - `-u` makes repeat runs incremental (docs/DESIGN.md 14.3's
    # "attachment manifest/incremental copy") instead of rewriting every blob
    # every night.
    (
      cd "$attachments_source" || exit 1
      find . -type f ! -name '.incoming-*' -print0
    ) | (
      cd "$attachments_source" || exit 1
      xargs -0 --no-run-if-empty cp -au --parents -t "$attachments_dir"
    )
  fi
  chown -R postgres:postgres "$attachments_dir"

  printf 'copy-complete\n' >&4

  # `|| db_phase_status=$?` rather than a bare `wait`: under `set -e` a bare
  # failing `wait` aborts here, which happens to exit with the same status but
  # skips this line, so the intent stops being visible to the next reader.
  db_phase_status=0
  wait "$db_phase_pid" || db_phase_status=$?
  exit "$db_phase_status"
fi

mkdir -p "$backup_root"

# The backup lock is held by the ROOT phase above (review finding F3 moved it
# there so that it also covers the attachment copy); this phase inherits fd 200
# and so runs inside the same lock. Two concurrent invocations each write their
# own uniquely-timestamped dump/manifest/row-counts files, so those never
# collide - but both would write the SAME `latest.txt`, and interleaved writes
# to one file from two processes can corrupt it.
#
# Refuses to run without the handshake directory rather than falling back to
# some copy-less mode: this phase is meaningless on its own, and a direct
# invocation would silently produce a dump with no attachment copy behind it.
if [[ -z "${TC_BACKUP_HANDSHAKE_DIR:-}" ]]; then
  echo "FAIL: the database phase was started without a handshake directory - it must be forked by this script's own root phase, never invoked directly" >&2
  exit 1
fi
snapshot_ready_fifo="$TC_BACKUP_HANDSHAKE_DIR/snapshot-ready"
copy_complete_fifo="$TC_BACKUP_HANDSHAKE_DIR/copy-complete"

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

# Releases root's attachment copy (review finding F3). Everything the dump can
# reference is fixed by this snapshot, so a copy started from here on is
# guaranteed to be a superset of it.
printf 'snapshot-ready\n' >"$snapshot_ready_fifo"

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

# Block until root's copy has finished (review finding F3), so the manifest
# below describes exactly what was copied rather than a half-finished tree.
# Read-write open for the same reason root uses one: a read-only open on a
# FIFO blocks before `read -t` ever starts counting, which would make this
# wait unbounded in exactly the case it exists to survive.
echo "--- attachments: waiting for the root-phase copy to complete"
exec 5<>"$copy_complete_fifo"
if ! read -r -t 600 -u 5 _copy_signal; then
  echo "FAIL: the root phase did not signal 'copy complete' within 600s - refusing to write a manifest that may not describe the copied tree" >&2
  exit 1
fi

echo "--- attachments: sha256 manifest"
# The copy itself is root's job and happened above, AFTER this phase exported
# its snapshot (review finding F3): attachments are owned by uid 10001, the
# application container's user, and are unreadable to the `postgres` user this
# section runs as - only root could read them. Only the manifest is written
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

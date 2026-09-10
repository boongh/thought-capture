#!/usr/bin/env bash
# Proves scripts/backup.sh's dump can actually be restored (docs/incidents/
# 0001-docker-compose-down-deleted-the-real-dev-stack.md), as part of
# scripts/check.sh - not merely that pg_dump exits 0.
#
# Deliberately does NOT reuse scripts/backup.sh/restore-test.sh directly:
# those intentionally target the real `thought-capture` project (no -p) so a
# real invocation backs up real data. A routine `check.sh` run must never
# start, reuse, or touch the developer's real dev stack, and must never
# collide with the port scripts/check.sh's own "integration tests" step may
# already be using - so this brings up its own throwaway, uniquely-named,
# uniquely-ported project instead, with a synthetic env (same pattern
# scripts/check.sh's own "compose config sanity" step already uses), and
# tears it down unconditionally on exit.
#
# TC_BACKUP_ROOT isolation (review finding): this check used to leave
# TC_BACKUP_ROOT unset in its synthetic env file, which meant Compose
# resolved the `backup`/`restore-test` services' bind mount from the
# operator's ambient environment or the real default
# (deploy/compose/backups) - so a routine `check.sh` run overwrote the
# operator's actual `latest.txt` with a synthetic backup, and its negative
# test round deliberately left that pointer referencing a corrupt one. This
# script now creates its OWN per-run directory (see the comment further
# down for exactly where and why), injects it into both the container side
# (via the synthetic env file) and the host side (this script reads its own
# variable, never a `${TC_BACKUP_ROOT:-...}` fallback onto the real root),
# and deletes it on exit.
#
# PLATFORM TRAP - POSIX permission bits (review finding F1; the second
# platform trap in this file, alongside the `mktemp` path-translation one
# documented below). deploy/compose/backup/backup.sh deliberately finishes by
# chowning its output to `postgres` (uid 999) and chmod 700-ing the backup
# root. A Linux bind mount enforces those bits for real, so the INVOKING user
# (uid 1001 on a GitHub Actions runner) cannot stat, read, or delete anything
# inside that tree; a Docker Desktop for Windows bind mount silently ignores
# the same chown/chmod, so an identical host-side check passes locally. That
# divergence is why this script's original host-side assertions were green on
# Windows and red in CI with a misleading "backup did not copy the seeded
# attachment" - the file WAS copied; the runner simply could not see it.
# Consequently: any host-side file assertion against the backup root here is
# platform-divergent by construction. Read the backup root through a CONTAINER
# instead (the `restore-test` service already bind-mounts it read-only,
# already runs as the uid that owns it, and Compose already resolves the path
# correctly on both platforms), and hand ownership back through a container
# before deleting it.
set -euo pipefail

script_directory="${BASH_SOURCE[0]%/*}"
if [[ "$script_directory" == "${BASH_SOURCE[0]}" ]]; then
  script_directory="."
fi
cd "$script_directory/.."

backup_project="thought-capture-check-backup"
restore_project="thought-capture-check-restore-test"
compose_file="deploy/compose/docker-compose.yml"
restore_compose_file="deploy/compose/backup.restore-test.docker-compose.yml"

# Per-run, isolated backup root. Deliberately created UNDER deploy/compose/
# (a `mktemp -d` TEMPLATE, not the system temp directory) rather than an
# absolute path from the system temp directory: confirmed directly on this
# platform (Docker Desktop on Windows, driven from Git Bash) - Git Bash's own
# `mktemp -d` returns an MSYS-style path (e.g. "/tmp/tmp.XXXX") that Git Bash
# itself resolves correctly for THIS SCRIPT's own file checks, but which is
# never translated when it is merely file CONTENT that `docker compose` (a
# native, non-MSYS executable) reads out of the synthetic env file below -
# Docker Desktop's WSL2 backend resolved that string as a path INSIDE its own
# Linux VM instead of the intended host directory, so backup.sh wrote
# everything successfully while every host-side assertion below found an
# empty directory. A path under deploy/compose/ resolves identically to how
# the real default (`./backups`, also relative to deploy/compose/) already
# works - the exact mechanism already proven to bind-mount correctly here -
# so this sidesteps the translation gap entirely instead of trying to get a
# system-temp absolute path translated correctly on every platform.
#
# A marker file inside it is what `cleanup` below checks before ever calling
# `rm -rf` on it, so a future edit that changes how this variable is
# computed can never silently turn cleanup into an rm -rf of an operator
# path.
backup_root_host="$(mktemp -d "deploy/compose/.tc-check-backup-restore-XXXXXXXX")"
: >"$backup_root_host/.tc-check-backup-restore-marker"
# The value Compose itself resolves (relative to $compose_file's own
# directory, deploy/compose/ - the project directory Compose uses for a
# relative `${TC_BACKUP_ROOT:-...}` bind-mount source), not the repo-root-
# relative $backup_root_host path used for this script's OWN file checks.
backup_root_compose_value="./$(basename "$backup_root_host")"

# Positive guard (review finding, step 4): assert the isolated root can
# never collide with the real default backup root (deploy/compose/backups).
# A freshly generated `mktemp` name should never produce this, but the check
# exists so a change to either side fails loudly instead of silently
# checking against the real root again.
real_backup_root_default="$(dirname "$compose_file")/backups"
if [[ "$backup_root_host" == "$real_backup_root_default" ]]; then
  printf 'FAIL: the isolated backup root resolved to the real default backup root (%s) - refusing to run\n' \
    "$real_backup_root_default" >&2
  exit 1
fi

env_file="$(mktemp)"
cat >"$env_file" <<ENV
POSTGRES_PASSWORD=check-only-not-a-real-secret
TC_APP_DB_PASSWORD=check-only-not-a-real-secret
POSTGRES_PORT=15498
TC_DISCORD_OWNER_USER_ID=100000000000000001
TC_BACKUP_ROOT=$backup_root_compose_value
ENV

cleanup() {
  # Ownership handback FIRST (review finding F1). backup.sh leaves this tree
  # owned by uid 999, mode 0700; on Linux the invoking user genuinely cannot
  # delete inside it, so every run used to leave a
  # deploy/compose/.tc-check-backup-restore-* directory behind - and an `rm`
  # that fails inside an EXIT trap can mask the real exit code too. The
  # `backup` service is the one that mounts the backup root read-write;
  # `restore-test` mounts it :ro and so cannot chown it. Numeric ids, not
  # names: the invoking uid has no passwd entry inside the container.
  if [[ "$backup_has_run" -eq 1 ]]; then
    docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
      run --rm --no-deps -T --user 0:0 --entrypoint /bin/bash backup \
      -c "chown -R $(id -u):$(id -g) /backups && chmod -R u+rwX /backups" \
      >/dev/null 2>&1 || true
  fi
  scripts/compose-teardown.sh --env-file "$env_file" -p "$restore_project" \
    -f "$restore_compose_file" down -v --remove-orphans >/dev/null 2>&1 || true
  # --profile core --profile backup: `docker compose down` only acts on
  # services whose profile is active in the SAME invocation - every service
  # in docker-compose.yml is profile-gated (none are in the default,
  # profile-less scope), so a `down` without matching --profile flags is a
  # silent no-op that leaves every container running. Confirmed directly
  # while building this: the first version of this line omitted --profile,
  # and "down" exited 0 having removed nothing.
  scripts/compose-teardown.sh --env-file "$env_file" -p "$backup_project" \
    -f "$compose_file" --profile core --profile backup down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$env_file"
  # Only ever remove a directory THIS run created: guarded on both a
  # non-empty variable and the marker file this run itself wrote above, so
  # a future edit that changes backup_root_host's value earlier in the
  # script can never turn this into an rm -rf of an unrelated path.
  if [[ -n "${backup_root_host:-}" && -f "$backup_root_host/.tc-check-backup-restore-marker" ]]; then
    rm -rf "$backup_root_host"
  fi
}
# Set to 1 the moment the first `backup` run has written into the isolated
# backup root; `cleanup` reads it to decide whether an ownership handback is
# needed at all (before that there is nothing to hand back, and the `run`
# would pointlessly create this project's network on the way out of an early
# failure).
backup_has_run=0
trap cleanup EXIT

# Tear down first too, in case a prior run (e.g. a killed CI job) left either
# throwaway project behind - the exact class of bug found and fixed in
# scripts/restore-test.sh while building this.
scripts/compose-teardown.sh --env-file "$env_file" -p "$restore_project" \
  -f "$restore_compose_file" down -v --remove-orphans >/dev/null 2>&1 || true
scripts/compose-teardown.sh --env-file "$env_file" -p "$backup_project" \
  -f "$compose_file" --profile core --profile backup down -v --remove-orphans >/dev/null 2>&1 || true

printf -- '--- bringing up a throwaway postgres (project: %s)\n' "$backup_project"
docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
  --profile core --profile backup up -d --build postgres

printf -- '--- running migrations (schema + workspace seed)\n'
docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
  --profile core up --build migrate

# Seeds one synthetic file (CLAUDE.md: "use synthetic memory content in
# fixtures") into the attachments volume before running backup, so this
# check exercises the real attachment-copy/hash-manifest code path -
# without this, every run before it was seeded had zero attachments, and
# backup.sh's "no attachments were present" branch (an empty manifest,
# nothing copied) was the ONLY path this check ever proved worked. A review
# of this PR correctly found that gap: an empty backup passing proves
# nothing about whether a real one, with real attachments, actually works.
attachment_content="synthetic attachment for check-backup-restore.sh - $(date -u +%Y%m%dT%H%M%SZ)"
attachment_sha256="$(printf '%s' "$attachment_content" | sha256sum | cut -d' ' -f1)"
docker run --rm -v "${backup_project}_attachments:/data" alpine:3.20 \
  sh -c "mkdir -p /data/ab && printf '%s' \"\$0\" > /data/ab/synthetic.txt" "$attachment_content"

printf -- '--- running backup\n'
backup_has_run=1
docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
  --profile core --profile backup up --build --force-recreate --exit-code-from backup backup

# ---------------------------------------------------------------------------
# Every assertion about the backup root runs INSIDE a container, never on the
# host (review finding F1 - see this file's PLATFORM TRAP header note). The
# `restore-test` service is reused for this because it already bind-mounts
# ${TC_BACKUP_ROOT} read-only, already runs as the uid that owns those files,
# and Compose already resolves that path correctly on both platforms - which
# the host side does not. `--no-deps` because none of these need the scratch
# database; `-T` because there is no TTY in CI.
# ---------------------------------------------------------------------------
backup_root_exec() {
  # $1 = --user value, remaining args before the trailing script are extra
  # `docker compose run` flags. The last argument is the bash script text.
  local as_user="$1"
  shift
  local script="${!#}"
  local flags=("${@:1:$#-1}")
  docker compose --env-file "$env_file" -p "$restore_project" -f "$restore_compose_file" \
    run --rm --no-deps -T --user "$as_user" "${flags[@]}" \
    --entrypoint /bin/bash restore-test -c "$script"
}

# ---------------------------------------------------------------------------
# Permission property (review finding F1, step 1). backup.sh's closing
# `chmod 700` is a real security property on Linux - the backup contains the
# full plaintext database and every attachment - so assert it deliberately
# instead of leaving it as the accident that used to break this script. An
# unrelated, non-root uid must not be able to read even `latest.txt`.
#
# Soft-passes where the bind mount does not enforce POSIX bits at all (Docker
# Desktop for Windows), and says so rather than passing silently: on that
# platform the property is untestable, not satisfied. CI runs on Linux, where
# it is asserted for real.
# ---------------------------------------------------------------------------
printf -- '--- asserting the backup root is unreadable to an unrelated non-root uid\n'
backup_root_ownership="$(backup_root_exec 0:0 "stat -c '%u %a' /backups" 2>/dev/null | tr -d '\r' | tail -n 1 || true)"
if [[ "$backup_root_ownership" != "999 700" ]]; then
  printf 'SKIP: this platform does not enforce POSIX ownership/mode across the backup bind mount (saw %s, expected "999 700") - the restrictive-permission property cannot be asserted here; CI on Linux does assert it\n' \
    "\"$backup_root_ownership\""
else
  unrelated_uid_verdict="$(backup_root_exec 4242:4242 'if cat /backups/latest.txt >/dev/null 2>&1; then echo READABLE; else echo NOT-READABLE; fi' 2>/dev/null | tr -d '\r' | tail -n 1 || true)"
  if [[ "$unrelated_uid_verdict" != "NOT-READABLE" ]]; then
    printf 'FAIL: uid 4242 could read the backup root (%s) - backup.sh must leave it readable only by its owner\n' \
      "$unrelated_uid_verdict" >&2
    exit 1
  fi
  printf 'OK: backup root is 0700/uid-999 and unreadable to an unrelated uid\n'
fi

printf -- '--- asserting the seeded attachment was actually copied and hashed\n'
attachment_assert_script="$(cat <<'CONTAINER_SCRIPT'
set -euo pipefail
copied="/backups/attachments/ab/synthetic.txt"
if [[ ! -f "$copied" ]]; then
  echo "FAIL: backup did not copy the seeded attachment to <backup root>/attachments/ab/synthetic.txt" >&2
  ls -la /backups /backups/attachments >&2 || true
  exit 1
fi
if [[ "$(cat "$copied")" != "$EXPECTED_CONTENT" ]]; then
  echo "FAIL: copied attachment content does not match what was seeded" >&2
  exit 1
fi
latest_name="$(tr -d '[:space:]' </backups/latest.txt)"
manifest_name="$(grep -oE '"attachments_manifest_file": *"[^"]*"' "/backups/$latest_name" | sed -E 's/.*"([^"]+)"$/\1/')"
if [[ -z "$manifest_name" ]]; then
  echo "FAIL: could not find attachments_manifest_file in $latest_name" >&2
  exit 1
fi
if ! grep -q "^${EXPECTED_SHA256}  \./ab/synthetic\.txt$" "/backups/$manifest_name"; then
  echo "FAIL: attachment manifest $manifest_name does not record the seeded file's sha256 ($EXPECTED_SHA256)" >&2
  cat "/backups/$manifest_name" >&2
  exit 1
fi
echo "OK: seeded attachment present and hash-valid after backup"
CONTAINER_SCRIPT
)"
if ! backup_root_exec postgres \
  -e "EXPECTED_CONTENT=$attachment_content" \
  -e "EXPECTED_SHA256=$attachment_sha256" \
  "$attachment_assert_script"; then
  printf 'FAIL: the container-side backup-root assertions failed (see above)\n' >&2
  exit 1
fi

# A fresh postgres-scratch every round, not reused across rounds: the same
# bug this guards against for repeated script invocations
# (scripts/restore-test.sh's own comment/commit explains it) applies
# equally between the rounds run here.
run_restore_test() {
  docker compose --env-file "$env_file" -p "$restore_project" -f "$restore_compose_file" \
    down -v --remove-orphans >/dev/null 2>&1 || true
  docker compose --env-file "$env_file" -p "$restore_project" -f "$restore_compose_file" \
    up -d --build --force-recreate --wait postgres-scratch
  docker compose --env-file "$env_file" -p "$restore_project" -f "$restore_compose_file" \
    run --rm restore-test
}

printf -- '--- round 1: restore-test against a complete backup (expect success)\n'
run_restore_test

# ---------------------------------------------------------------------------
# Negative test (review finding): restore-test.sh's manifest re-check can
# only ever re-verify files that WERE copied - a copy step that silently
# omits a blob the database still references would produce a manifest with
# nothing to complain about, and restore-test would pass while the restored
# `blobs` row points at a file that exists nowhere in the backup. Proves
# deploy/compose/backup/restore-test.sh's newer "every database-referenced
# blob" check (queries the restored `blobs` table directly, independent of
# the manifest) actually catches that - not just that the happy path above
# passes, which an empty or trivially-complete manifest could also do.
#
# Simulates the omission the cheap way: insert a `blobs` row for content
# that was never written to the attachments volume at all, rather than
# reproducing an actual copy bug. From restore-test.sh's point of view the
# two are indistinguishable - either way, the database says a blob should
# exist and the backup directory does not have it.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Race regression (review finding, Finding 3): backup.sh used to take its
# pg_dump snapshot, then count rows through SEPARATE, later connections. A
# capture committed in between made the recorded count higher than the
# dump actually held, and restore-test.sh failed a perfectly valid backup.
# This starts a continuous writer BEFORE the backup begins and keeps it
# running for the backup's entire duration (not a single, hard-to-time
# insert), so the race window is covered regardless of exactly how long
# pg_dump takes - on the old code this reliably fails restore-test; on the
# snapshot-consistent code (backup.sh now exports one snapshot and counts
# rows in that same still-open transaction) it reliably passes.
#
# Deliberately runs as round 2, BEFORE the orphan-blob negative test below:
# that test permanently inserts a `blobs` row with no backing file into
# $backup_project's database (it is never rolled back), and this round needs
# a database that isn't already broken that way - confirmed directly while
# building this: running it after the negative test failed restore-test for
# the orphan blob's real, pre-existing reason, not for anything to do with
# the race itself.
# ---------------------------------------------------------------------------
printf -- '--- round 2: concurrent writer during backup (race regression)\n'
writer_stop_file="$(mktemp -u)"
# Records one line per successfully COMMITted insert (review finding: the
# prior version fired-and-forgot with `|| true`, so a writer that failed on
# every attempt - e.g. a schema/constraint mismatch - would silently pass
# this round without ever having exercised the race at all). 'api' is a
# real allowed `thoughts.source` value (migrations/versions/
# 0001_canonical_capture_layer.py's CHECK); the previous 'race-test' value
# violated that CHECK, so this loop was silently failing every iteration.
writer_success_log="$(mktemp -u)"
: >"$writer_success_log"
(
  i=0
  while [[ ! -f "$writer_stop_file" ]]; do
    i=$((i + 1))
    if docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
      exec -T -e PGPASSWORD=check-only-not-a-real-secret postgres \
      psql -v ON_ERROR_STOP=1 --quiet -U tc_migrator -d thought_capture -c \
      "INSERT INTO thoughts (workspace_id, author_user_id, source, source_message_id, body, client_created_at, client_timezone, client_local_date, client_local_time, received_at, content_language)
       SELECT w.id, u.id, 'api', 'race-test-$i-' || extract(epoch from clock_timestamp()), 'synthetic race-regression thought', now(), 'UTC', current_date, current_time, now(), 'en'
       FROM workspaces w JOIN users u ON true LIMIT 1" \
      >/dev/null 2>&1; then
      printf '%s\n' "$i" >>"$writer_success_log"
    fi
    sleep 0.1
  done
) &
writer_pid=$!

docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
  --profile core --profile backup up --build --force-recreate --exit-code-from backup backup

: >"$writer_stop_file"
wait "$writer_pid" 2>/dev/null || true
rm -f "$writer_stop_file"

# Asserts the race was actually exercised, not merely that the backup
# succeeded - a writer that never committed anything (e.g. every insert
# rejected) would make this round pass trivially without ever having
# started a live snapshot against a moving target.
writer_commit_count="$(wc -l <"$writer_success_log" | tr -d ' ')"
rm -f "$writer_success_log"
if [[ "$writer_commit_count" -lt 1 ]]; then
  echo "FAIL: the concurrent writer never committed a row during the backup - race regression not actually exercised" >&2
  exit 1
fi
printf -- '--- round 2: concurrent writer committed %s row(s) during the backup\n' "$writer_commit_count"

printf -- '--- round 2: restore-test against the concurrently-written backup (expect success)\n'
run_restore_test
printf 'OK: restore-test passed against a backup taken while a writer committed rows concurrently\n'

# ---------------------------------------------------------------------------
# Negative test (review finding): restore-test.sh's manifest re-check can
# only ever re-verify files that WERE copied - a copy step that silently
# omits a blob the database still references would produce a manifest with
# nothing to complain about, and restore-test would pass while the restored
# `blobs` row points at a file that exists nowhere in the backup. Proves
# deploy/compose/backup/restore-test.sh's newer "every database-referenced
# blob" check (queries the restored `blobs` table directly, independent of
# the manifest) actually catches that - not just that the happy path above
# passes, which an empty or trivially-complete manifest could also do.
#
# Simulates the omission the cheap way: insert a `blobs` row for content
# that was never written to the attachments volume at all, rather than
# reproducing an actual copy bug. From restore-test.sh's point of view the
# two are indistinguishable - either way, the database says a blob should
# exist and the backup directory does not have it.
#
# Deliberately runs LAST: this permanently corrupts $backup_project's
# database (the orphan `blobs` row is never rolled back), so nothing after
# it may depend on a clean database.
# ---------------------------------------------------------------------------
printf -- '--- round 3: seeding a database-referenced blob with no backing file (negative test)\n'
orphan_content="synthetic orphan blob for check-backup-restore.sh's negative test - never written to attachments - $(date -u +%Y%m%dT%H%M%SZ)"
orphan_sha256="$(printf '%s' "$orphan_content" | sha256sum | cut -d' ' -f1)"
orphan_size="$(printf '%s' "$orphan_content" | wc -c)"
# Matches packages/infrastructure/src/tc_infrastructure/storage/blob_store.py's
# own `storage_key_for`: two 2-character fan-out levels, then the full hash.
orphan_storage_key="${orphan_sha256:0:2}/${orphan_sha256:2:2}/${orphan_sha256}"
docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
  exec -T -e PGPASSWORD=check-only-not-a-real-secret postgres \
  psql -v ON_ERROR_STOP=1 --quiet -U tc_migrator -d thought_capture -c \
  "INSERT INTO blobs (sha256, size_bytes, media_type, storage_key) VALUES ('$orphan_sha256', $orphan_size, 'text/plain', '$orphan_storage_key')"

printf -- '--- round 3: running backup again (the dump now references the orphan blob)\n'
docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
  --profile core --profile backup up --build --force-recreate --exit-code-from backup backup

printf -- '--- round 3: running restore-test against the orphan-blob backup (expect FAILURE)\n'
round_3_output="$(mktemp)"
round_3_exit=0
run_restore_test >"$round_3_output" 2>&1 || round_3_exit=$?
cat "$round_3_output"

if [[ "$round_3_exit" -eq 0 ]]; then
  printf 'FAIL: restore-test succeeded against a backup missing a database-referenced blob (sha256=%s) - it should have failed\n' \
    "$orphan_sha256" >&2
  rm -f "$round_3_output"
  exit 1
fi
if ! grep -q "$orphan_sha256" "$round_3_output"; then
  printf 'FAIL: restore-test failed (exit %s), as expected, but its output never named the missing blob (sha256=%s) - the failure may be for the wrong reason\n' \
    "$round_3_exit" "$orphan_sha256" >&2
  rm -f "$round_3_output"
  exit 1
fi
rm -f "$round_3_output"
printf 'OK: restore-test correctly failed (exit %s) on a database-referenced blob missing from the backup\n' "$round_3_exit"

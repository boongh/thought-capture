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
# A name distinct from every real dev-stack container (CLAUDE.md's Docker
# section): round 2 runs this as a long-lived writer against the throwaway
# project's attachments volume and database.
blob_writer_container="tc-check-backup-blob-writer"
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

# Ambient shell environment wins over `--env-file` during Compose variable
# interpolation - the same class of trap this file's own header already
# documents for TC_BACKUP_ROOT, just for a different variable. .github/
# workflows/ci.yml exports its own POSTGRES_PASSWORD (for an entirely
# unrelated Postgres service it runs directly, not through this project's
# Compose files) as an ambient env var for the whole job, which makes THAT
# value - not "check-only-not-a-real-secret" above - the real password
# Compose gives this throwaway project's `postgres` service. Every service
# defined in $compose_file that reads `${POSTGRES_PASSWORD}` resolves it
# correctly regardless (Compose interpolation is consistent), but every ad-
# hoc `docker run`/`docker compose exec` invocation below that needs to
# authenticate as tc_migrator must resolve the password the same way
# Compose does, via this variable, rather than assuming the literal above -
# confirmed directly in CI (run 34572786627): a hardcoded PGPASSWORD there
# authenticated with the wrong password and failed every single attempt.
resolved_postgres_password="${POSTGRES_PASSWORD:-check-only-not-a-real-secret}"

# PLATFORM TRAP - argv path translation. Every `--entrypoint` below names
# `bash`, never `/bin/bash`, and that is load-bearing rather than a style
# choice. Confirmed directly on this platform: Git Bash rewrites a
# leading-slash argv element into a Windows path before the native
# `docker.exe` ever sees it, so a `--entrypoint /bin/bash` arrives as
# `C:/Program Files/Git/bin/bash` and the container refuses to start with
# `stat C:/Program: no such file or directory`. A bare `bash` has no leading
# slash, is never rewritten, and resolves on PATH inside every image used
# here. Blanket-disabling the translation (MSYS_NO_PATHCONV /
# MSYS2_ARG_CONV_EXCL) was tried first and is worse: it also stops the
# translation this script DEPENDS on for `--env-file "$env_file"`, whose
# `mktemp` path must be translated, and that failed with
# `couldn't find env file: C:\tmp\tmp.XXXX`. The compose files themselves are
# unaffected either way - a path inside YAML is file CONTENT, never argv -
# which is why only these CLI-level invocations care.

cleanup() {
  docker rm -f "$blob_writer_container" >/dev/null 2>&1 || true
  # The `thoughts` writer subshell (round 2) loops until `$writer_stop_file`
  # exists, which is only ever created on round 2's own success path. Any
  # failure between starting it and that point - including round 2's backup
  # itself failing, which is exactly the scenario this round exists to
  # exercise - would otherwise leave an orphaned infinite loop invoking
  # `docker compose exec` against a project this same cleanup is tearing
  # down. Guarded on the variable being set: cleanup also runs on every exit
  # path before round 2 ever starts the writer.
  if [[ -n "${writer_pid:-}" ]]; then
    kill "$writer_pid" 2>/dev/null || true
    wait "$writer_pid" 2>/dev/null || true
  fi
  # Ownership handback FIRST (review finding F1). backup.sh leaves this tree
  # owned by uid 999, mode 0700; on Linux the invoking user genuinely cannot
  # delete inside it, so every run used to leave a
  # deploy/compose/.tc-check-backup-restore-* directory behind - and an `rm`
  # that fails inside an EXIT trap can mask the real exit code too. The
  # `backup` service is the one that mounts the backup root read-write;
  # `restore-test` mounts it :ro and so cannot chown it. Numeric ids, not
  # names: the invoking uid has no passwd entry inside the container.
  #
  # `--profile core --profile backup` is REQUIRED even with `--no-deps` - a
  # second review round found this call failing outright with "no such
  # service: postgres" without it, because Compose drops a profile-gated
  # service (`backup`'s own `depends_on: postgres`) from the resolved model
  # entirely when no matching profile is active, and `--no-deps` only skips
  # STARTING a dependency, not resolving whether it exists. This had been
  # silently failing every run (masked by `|| true`), invisible on Windows
  # only because Docker Desktop for Windows does not enforce the permissions
  # this handback exists to undo in the first place - `rm -rf` below still
  # succeeded regardless of whether the chown ever ran. On real Linux CI,
  # where the permissions ARE enforced, this would have left every run's
  # backup root behind uncleaned.
  if [[ "$backup_has_run" -eq 1 ]]; then
    docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
      --profile core --profile backup \
      run --rm --no-deps -T --user 0:0 --entrypoint bash backup \
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
  #
  # Reads only: `restore-test` mounts the backup root `:ro`
  # (deploy/compose/backup.restore-test.docker-compose.yml) - a write here
  # fails with EROFS regardless of uid. Use `backup_root_write_exec` below for
  # anything that writes.
  local as_user="$1"
  shift
  local script="${!#}"
  local flags=("${@:1:$#-1}")
  # "${flags[@]+"${flags[@]}"}", not a bare "${flags[@]}": bash before 4.4
  # treats expanding an empty array under `set -u` as an unbound-variable
  # error, and the first call site below (the ownership/enforcement probes)
  # passes no extra flags at all.
  docker compose --env-file "$env_file" -p "$restore_project" -f "$restore_compose_file" \
    run --rm --no-deps -T --user "$as_user" ${flags[@]+"${flags[@]}"} \
    --entrypoint bash restore-test -c "$script"
}

backup_root_write_exec() {
  # Same calling convention as `backup_root_exec`, but through the `backup`
  # service instead of `restore-test`: `backup` is the only service that
  # mounts the backup root read-write (deploy/compose/docker-compose.yml) -
  # the same reason cleanup's ownership handback below uses it rather than
  # `restore-test`. `--profile core --profile backup` is required even with
  # `--no-deps` - see the long comment on cleanup's own use of this same
  # pattern for why (Compose drops a profile-gated service's dependency from
  # the resolved model entirely, "no such service: postgres", unless a
  # matching profile is active).
  local as_user="$1"
  shift
  local script="${!#}"
  local flags=("${@:1:$#-1}")
  docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
    --profile core --profile backup \
    run --rm --no-deps -T --user "$as_user" ${flags[@]+"${flags[@]}"} \
    --entrypoint bash backup -c "$script"
}

# ---------------------------------------------------------------------------
# Permission property (review finding F1, step 1; corrected by a second review
# round after this script's own first version conflated two different
# questions). backup.sh's closing `chmod 700` is a real security property on
# Linux - the backup contains the full plaintext database and every
# attachment - so assert it deliberately instead of leaving it as the
# accident that used to break this script. An unrelated, non-root uid must
# not be able to read even `latest.txt`.
#
# The two questions, kept deliberately separate:
#   (a) Does THIS PLATFORM's bind mount enforce POSIX ownership/mode at all?
#   (b) Given that it does, did backup.sh actually leave the REAL backup root
#       at the correct 999/700?
#
# The original version of this check collapsed both into one comparison
# (`stat` the real backup root, soft-pass on anything but "999 700") - which
# meant a real regression in backup.sh's own chown/chmod (say, a lost
# `chmod 700`) was INDISTINGUISHABLE from "this platform doesn't enforce
# permissions" and silently printed SKIP instead of FAIL. (a) is answered
# first, independently, with a disposable scratch fixture that has nothing to
# do with the real backup root; only once enforcement is confirmed does this
# treat a mismatch on the real backup root as a hard failure.
# ---------------------------------------------------------------------------
printf -- '--- probing whether this platform enforces POSIX ownership/mode across the bind mount\n'
# A throwaway probe file, unrelated to anything backup.sh writes. Written and
# chowned as ROOT (0:0) through the `backup` service (the only one with a
# read-write mount on the backup root) - by this point in the script the
# backup root is already uid-999/mode-700 from the real backup that just ran,
# so a non-root uid could never write into it at all regardless of platform;
# only root's own privilege bypasses that check the same way backup.sh's own
# root phase does. The probe is then read back as an UNRELATED uid (5252,
# neither root nor the 4242 it is chowned to) through `restore-test` (whose
# read-only mount is fine for reading). Whether 5252 can read it answers
# question (a) on its own - it says nothing about whether backup.sh got its
# own chown/chmod right.
enforcement_probe_verdict="$(backup_root_write_exec 0:0 \
  'echo enforcement-probe >/backups/.tc-enforcement-probe && chown 4242:4242 /backups/.tc-enforcement-probe && chmod 600 /backups/.tc-enforcement-probe && echo PROBE-WRITTEN' \
  2>/dev/null | tr -d '\r' | tail -n 1 || true)"
if [[ "$enforcement_probe_verdict" != "PROBE-WRITTEN" ]]; then
  printf 'FAIL: could not write the permission-enforcement probe file through a container - the probe did not run\n' >&2
  exit 1
fi
enforcement_holds_verdict="$(backup_root_exec 5252:5252 \
  'if cat /backups/.tc-enforcement-probe >/dev/null 2>&1; then echo READABLE; else echo NOT-READABLE; fi' \
  2>/dev/null | tr -d '\r' | tail -n 1 || true)"
backup_root_write_exec 0:0 'rm -f /backups/.tc-enforcement-probe' >/dev/null 2>&1 || true
if [[ -z "$enforcement_holds_verdict" ]]; then
  printf 'FAIL: could not read the permission-enforcement probe back through a container - the probe did not run\n' >&2
  exit 1
fi

printf -- '--- asserting the backup root is unreadable to an unrelated non-root uid\n'
if [[ "$enforcement_holds_verdict" != "NOT-READABLE" ]]; then
  # Question (a) answered NO: this bind mount does not enforce POSIX bits for
  # a container running as an unrelated uid at all (Docker Desktop for
  # Windows). The real backup root's permissions cannot be asserted here -
  # loudly SKIP rather than silently pass, and rather than mistake this for a
  # regression in backup.sh, which is a separate question this platform
  # cannot answer either way.
  printf 'SKIP: this platform does not enforce POSIX ownership/mode across the backup bind mount at all (an unrelated uid could read a freshly-created 0600 file) - the restrictive-permission property cannot be asserted here; CI on Linux does assert it\n'
else
  # Question (a) answered YES: enforcement demonstrably works on this
  # platform, so a mismatch on the REAL backup root from here on is a real
  # regression in backup.sh, not a platform limitation - hard FAIL, no soft
  # pass.
  backup_root_ownership="$(backup_root_exec 0:0 "stat -c '%u %a' /backups" 2>/dev/null | tr -d '\r' | tail -n 1 || true)"
  if [[ -z "$backup_root_ownership" ]]; then
    printf 'FAIL: could not read the backup root'"'"'s ownership through a container - the permission probe did not run\n' >&2
    exit 1
  fi
  if [[ "$backup_root_ownership" != "999 700" ]]; then
    printf 'FAIL: the backup root is not uid-999/mode-700 (saw "%s") on a platform that enforces POSIX permissions - backup.sh must leave it readable only by its owner\n' \
      "$backup_root_ownership" >&2
    exit 1
  fi
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
# Runs inside the long-lived writer container started below. `<<'BLOB_WRITER'`
# so nothing here is expanded by THIS shell - every variable in it belongs to
# the container's own shell.
blob_writer_script="$(cat <<'BLOB_WRITER'
set -u
i=0
while true; do
  i=$((i + 1))
  content="synthetic race blob $i $(date -u +%s%N)"
  sha="$(printf '%s' "$content" | sha256sum | cut -d' ' -f1)"
  size="$(printf '%s' "$content" | wc -c)"
  # Matches blob_store.py's own `storage_key_for`: two 2-character fan-out
  # levels, then the full hash.
  key="${sha:0:2}/${sha:2:2}/$sha"
  mkdir -p "/data/attachments/${sha:0:2}/${sha:2:2}"
  # File first, durably, THEN the row - blob_store.py's `put` order, and the
  # premise of the fix under test: everything a snapshot can see already has
  # its bytes on disk.
  printf '%s' "$content" >"/data/attachments/$key"
  sync
  # Diagnostic (review finding, fourth round): the `if ... >/dev/null 2>&1`
  # form this used to have swallows every failure completely - a writer
  # failing on every single attempt (wrong host, wrong role, wrong
  # password, schema drift) produces the exact same empty log as one that
  # simply has not run yet, making the two indistinguishable from outside
  # the container. Capturing psql's stderr and printing it (rate-limited to
  # roughly once a second, not once per 50ms attempt) turns a silent,
  # permanent failure into a diagnosable one without flooding the log on
  # the expected happy path where every attempt already prints COMMITTED.
  if psql_stderr="$(psql -v ON_ERROR_STOP=1 --quiet -h postgres -U tc_migrator -d thought_capture \
    -c "INSERT INTO blobs (sha256, size_bytes, media_type, storage_key) VALUES ('$sha', $size, 'text/plain', '$key')" \
    2>&1 >/dev/null)"; then
    echo "COMMITTED $sha"
  elif [[ $((i % 20)) -eq 1 ]]; then
    echo "INSERT_FAILED (attempt $i): $psql_stderr" >&2
  fi
  sleep 0.05
done
BLOB_WRITER
)"
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
      exec -T -e "PGPASSWORD=$resolved_postgres_password" postgres \
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

# ---------------------------------------------------------------------------
# Blob-ordering regression (review finding F3). The `thoughts` writer above
# only ever exercised row counts; it never touched the attachments volume, so
# it could not detect that backup.sh used to copy attachments BEFORE exporting
# the snapshot pg_dump reads through. A blob written and committed in that
# window is in the dump but not in the backup directory.
#
# This writer does the real thing, in blob-store order (write and sync the
# file, THEN commit the `blobs` row - the same order
# packages/infrastructure/src/tc_infrastructure/storage/blob_store.py's `put`
# guarantees, and the order the fix's correctness argument depends on), for
# the backup's whole duration. On the old copy-first ordering this reliably
# leaves the restored database referencing a blob the backup never copied,
# which restore-test.sh's "every database-referenced blob" step then fails on,
# naming its sha256. On the fixed ordering the copy is a superset by
# construction and it passes.
#
# One long-lived container rather than a `docker run` per iteration: container
# startup dominates otherwise, and a writer that manages one or two blobs over
# the whole backup is not a race test. It gets psql and the attachments volume
# in the same place - the `backup` service mounts attachments read-only, so it
# cannot be reused for this.
# ---------------------------------------------------------------------------
docker rm -f "$blob_writer_container" >/dev/null 2>&1 || true
docker run -d --name "$blob_writer_container" \
  --network "${backup_project}_default" \
  -v "${backup_project}_attachments:/data/attachments" \
  -e "PGPASSWORD=$resolved_postgres_password" \
  postgres:18.6-trixie@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280 \
  bash -c "$blob_writer_script" >/dev/null

# Wait for the writer's first COMMITTED blob before starting the backup
# (review finding, third round). The backup this exercises now finishes in
# well under a second (the attachment copy runs concurrently with pg_dump,
# not serially after it), which is faster than `docker run -d` above can
# boot a fresh container and get its first `psql` INSERT committed - so
# starting the backup immediately lost the race outright on a CI runner,
# passing zero commits and failing with "the concurrent blob writer never
# committed a blob during the backup" without ever having exercised
# anything. Blocking here until the writer is demonstrably already
# committing - bounded, so a genuinely broken writer still fails fast via
# the existing post-backup blob_commit_count check below - makes the race
# actually start under load instead of racing container startup itself.
writer_ready=0
writer_ready_waited=0
while [[ "$writer_ready_waited" -lt 30 ]]; do
  if docker logs "$blob_writer_container" 2>&1 | grep -q '^COMMITTED '; then
    writer_ready=1
    break
  fi
  if ! docker ps -q -f "name=^${blob_writer_container}\$" | grep -q .; then
    break
  fi
  sleep 0.2
  writer_ready_waited=$((writer_ready_waited + 1))
done
if [[ "$writer_ready" -eq 0 ]]; then
  printf 'FAIL: the concurrent blob writer never committed a blob within 6s of starting - it may have failed to start or connect\n' >&2
  docker logs "$blob_writer_container" >&2 2>&1 || true
  exit 1
fi

docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
  --profile core --profile backup up --build --force-recreate --exit-code-from backup backup

: >"$writer_stop_file"
wait "$writer_pid" 2>/dev/null || true
rm -f "$writer_stop_file"

docker stop -t 2 "$blob_writer_container" >/dev/null 2>&1 || true
blob_writer_log="$(docker logs "$blob_writer_container" 2>&1 || true)"
docker rm -f "$blob_writer_container" >/dev/null 2>&1 || true

# Same guard as the `thoughts` writer's, for the same reason: a blob writer
# that failed on every iteration - a schema change, a wrong password, an
# unreachable host - would make this round pass while testing nothing at all.
blob_commit_count="$(printf '%s\n' "$blob_writer_log" | grep -c '^COMMITTED ' || true)"
if [[ "$blob_commit_count" -lt 1 ]]; then
  printf 'FAIL: the concurrent blob writer never committed a blob during the backup - the copy-ordering regression is not actually exercised\n' >&2
  printf '%s\n' "$blob_writer_log" >&2
  exit 1
fi
printf -- '--- round 2: concurrent blob writer committed %s blob(s) during the backup\n' "$blob_commit_count"

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
  exec -T -e "PGPASSWORD=$resolved_postgres_password" postgres \
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

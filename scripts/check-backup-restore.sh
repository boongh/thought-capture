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

env_file="$(mktemp)"
cat >"$env_file" <<'ENV'
POSTGRES_PASSWORD=check-only-not-a-real-secret
TC_APP_DB_PASSWORD=check-only-not-a-real-secret
POSTGRES_PORT=15498
TC_DISCORD_OWNER_USER_ID=100000000000000001
ENV

cleanup() {
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
}
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
docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
  --profile core --profile backup up --build --force-recreate --exit-code-from backup backup

printf -- '--- asserting the seeded attachment was actually copied and hashed\n'
backup_root="${TC_BACKUP_ROOT:-deploy/compose/backups}"
copied_attachment="$backup_root/attachments/ab/synthetic.txt"
if [[ ! -f "$copied_attachment" ]]; then
  printf 'FAIL: backup did not copy the seeded attachment to %s\n' "$copied_attachment" >&2
  exit 1
fi
copied_content="$(cat "$copied_attachment")"
if [[ "$copied_content" != "$attachment_content" ]]; then
  printf 'FAIL: copied attachment content does not match what was seeded\n' >&2
  exit 1
fi
latest_manifest="$backup_root/$(cat "$backup_root/latest.txt")"
attachments_manifest_file="$backup_root/$(grep -oE '"attachments_manifest_file": *"[^"]*"' "$latest_manifest" | sed -E 's/.*"([^"]+)"$/\1/')"
if ! grep -q "^${attachment_sha256}  \./ab/synthetic\.txt$" "$attachments_manifest_file"; then
  printf 'FAIL: attachment manifest %s does not record the seeded file'"'"'s sha256 (%s)\n' \
    "$attachments_manifest_file" "$attachment_sha256" >&2
  cat "$attachments_manifest_file" >&2
  exit 1
fi
printf 'OK: seeded attachment present and hash-valid after backup\n'

# A fresh postgres-scratch every round, not reused across rounds: the same
# bug this guards against for repeated script invocations
# (scripts/restore-test.sh's own comment/commit explains it) applies
# equally between the two rounds run here.
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
printf -- '--- round 2: seeding a database-referenced blob with no backing file (negative test)\n'
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

printf -- '--- round 2: running backup again (the dump now references the orphan blob)\n'
docker compose --env-file "$env_file" -p "$backup_project" -f "$compose_file" \
  --profile core --profile backup up --build --force-recreate --exit-code-from backup backup

printf -- '--- round 2: running restore-test against the orphan-blob backup (expect FAILURE)\n'
round_2_output="$(mktemp)"
round_2_exit=0
run_restore_test >"$round_2_output" 2>&1 || round_2_exit=$?
cat "$round_2_output"

if [[ "$round_2_exit" -eq 0 ]]; then
  printf 'FAIL: restore-test succeeded against a backup missing a database-referenced blob (sha256=%s) - it should have failed\n' \
    "$orphan_sha256" >&2
  rm -f "$round_2_output"
  exit 1
fi
if ! grep -q "$orphan_sha256" "$round_2_output"; then
  printf 'FAIL: restore-test failed (exit %s), as expected, but its output never named the missing blob (sha256=%s) - the failure may be for the wrong reason\n' \
    "$round_2_exit" "$orphan_sha256" >&2
  rm -f "$round_2_output"
  exit 1
fi
rm -f "$round_2_output"
printf 'OK: restore-test correctly failed (exit %s) on a database-referenced blob missing from the backup\n' "$round_2_exit"

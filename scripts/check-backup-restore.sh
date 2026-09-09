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

printf -- '--- starting postgres-scratch (project: %s)\n' "$restore_project"
docker compose --env-file "$env_file" -p "$restore_project" -f "$restore_compose_file" \
  up -d --build --force-recreate --wait postgres-scratch

printf -- '--- running restore-test\n'
docker compose --env-file "$env_file" -p "$restore_project" -f "$restore_compose_file" \
  run --rm restore-test

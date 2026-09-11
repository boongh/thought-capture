#!/usr/bin/env bash
# Runs a one-shot local backup: pg_dump (custom format, atomic write) plus
# an attachment manifest/incremental copy, to TC_BACKUP_ROOT (default
# ./backups relative to deploy/compose/ - a HOST directory, never a Docker
# named volume; docs/incidents/0001-docker-compose-down-deleted-the-real-
# dev-stack.md is why that distinction matters). See
# deploy/compose/backup/backup.sh for the actual logic this runs.
#
# Usage: scripts/backup.sh [additional `docker compose up` args...]
set -euo pipefail

script_directory="${BASH_SOURCE[0]%/*}"
if [[ "$script_directory" == "${BASH_SOURCE[0]}" ]]; then
  script_directory="."
fi
cd "$script_directory/.."

compose_file="deploy/compose/docker-compose.yml"
env_file=".env"

# Must match deploy/compose/docker-compose.yml's own `name:` field - the same
# constant scripts/compose-teardown.sh pins, for the same reason.
real_project_name="thought-capture"

# A backup must target the REAL project, and must be able to prove it did
# (review finding, PR #32). Compose resolves a project name in this order:
# `-p` beats COMPOSE_PROJECT_NAME, which beats the compose file's own `name:`.
# This script originally passed no `-p` at all and relied on
# `name: thought-capture` - so an operator with COMPOSE_PROJECT_NAME exported
# for a second stack, or set in .env (which `--env-file` makes Compose read
# for exactly this variable), got a green, successful backup of a different,
# probably empty stack. A backup that silently captures the wrong data is the
# worst failure this script can have.
#
# Two controls, deliberately both: `-p` is pinned on every invocation below so
# the target is never ambient, AND a conflicting override is refused outright
# rather than silently ignored - the operator asked for something this script
# will not do, and should be told so rather than handed a backup of something
# else. Same fail-closed posture, and same absence of an override flag, as
# scripts/compose-teardown.sh.
refuse_project_override() {
  printf 'Refusing: %s sets COMPOSE_PROJECT_NAME="%s", but a backup only ever\n' "$1" "$2" >&2
  printf 'targets this repository'"'"'s real Compose project ("%s").\n' "$real_project_name" >&2
  printf '\n' >&2
  printf 'Running anyway would either back up a different stack or report success\n' >&2
  printf 'over an empty one. Remove or correct that setting, then run this again.\n' >&2
  exit 1
}

if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]] && [[ "$COMPOSE_PROJECT_NAME" != "$real_project_name" ]]; then
  refuse_project_override "the environment" "$COMPOSE_PROJECT_NAME"
fi

# The env file as well as the ambient environment: `--env-file "$env_file"`
# below makes Compose read COMPOSE_PROJECT_NAME from that file too, so an
# ambient-only check would miss the likelier case. Last assignment wins,
# matching how Compose itself reads the file; `tr -d '\r'` because a .env
# edited on Windows carries CRLF line endings that would otherwise become part
# of the value and turn an identical name into a mismatch.
if [[ -f "$env_file" ]]; then
  env_file_project_name="$(
    sed -n 's/^[[:space:]]*COMPOSE_PROJECT_NAME[[:space:]]*=[[:space:]]*//p' "$env_file" \
      | tr -d '\r' \
      | tail -n 1 \
      | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
  )"
  if [[ -n "$env_file_project_name" ]] && [[ "$env_file_project_name" != "$real_project_name" ]]; then
    refuse_project_override "$env_file" "$env_file_project_name"
  fi
fi

# `core` must be active too: `backup` only depends_on `postgres`, which is
# scoped to the `core` profile - without also activating `core` here,
# Compose would refuse to start postgres as a dependency of a service in a
# different profile.
printf -- '--- ensuring postgres is up (core profile)\n' >&2
docker compose --env-file "$env_file" -p "$real_project_name" -f "$compose_file" \
  --profile core --profile backup \
  up -d postgres

printf -- '--- running backup\n' >&2
docker compose --env-file "$env_file" -p "$real_project_name" -f "$compose_file" \
  --profile core --profile backup \
  up --build --force-recreate --exit-code-from backup "$@" backup

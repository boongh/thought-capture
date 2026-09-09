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

# `core` must be active too: `backup` only depends_on `postgres`, which is
# scoped to the `core` profile - without also activating `core` here,
# Compose would refuse to start postgres as a dependency of a service in a
# different profile.
printf -- '--- ensuring postgres is up (core profile)\n' >&2
docker compose --env-file .env -f "$compose_file" --profile core --profile backup \
  up -d postgres

printf -- '--- running backup\n' >&2
docker compose --env-file .env -f "$compose_file" --profile core --profile backup \
  up --build --force-recreate --exit-code-from backup "$@" backup

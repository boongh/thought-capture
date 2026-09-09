#!/usr/bin/env bash
# Validates the most recent scripts/backup.sh backup by actually restoring
# it into a throwaway, isolated Postgres (deploy/compose/
# backup.restore-test.docker-compose.yml, project `thought-capture-restore-
# test` - never `thought-capture`) and checking schema/constraints, every
# table's row count, and every attachment's blob hash. See
# deploy/compose/backup/restore-test.sh for what "validates" means exactly.
#
# Always tears the scratch stack down on exit (success, failure, or Ctrl-C)
# via scripts/compose-teardown.sh, itself passed the throwaway project name
# explicitly - dogfooding the same guard docs/incidents/0001-... introduced,
# on the one script in this repository most likely to run unattended.
set -euo pipefail

script_directory="${BASH_SOURCE[0]%/*}"
if [[ "$script_directory" == "${BASH_SOURCE[0]}" ]]; then
  script_directory="."
fi
cd "$script_directory/.."

compose_file="deploy/compose/backup.restore-test.docker-compose.yml"
restore_test_project="thought-capture-restore-test"

cleanup() {
  printf -- '--- tearing down the restore-test stack (project: %s)\n' "$restore_test_project" >&2
  # --env-file .env is required here even for `down`: Compose interpolates
  # every service's environment in the whole file before running any
  # command, and postgres-scratch's POSTGRES_PASSWORD uses `:?` (required) -
  # omitting it here isn't a silent no-op, it fails the teardown itself and
  # leaves the scratch container running. Confirmed directly while testing
  # this script: the first version of this line omitted --env-file, and a
  # completed run's own cleanup silently failed, leaving
  # thought-capture-restore-test-postgres-scratch-1 up afterward.
  scripts/compose-teardown.sh --env-file .env -p "$restore_test_project" -f "$compose_file" down -v --remove-orphans || true
}
trap cleanup EXIT

# Tear down BEFORE starting too, not only after: a prior run that crashed or
# was killed before its own `trap cleanup EXIT` could run (a machine crash,
# `docker compose` itself being killed) could leave a stale postgres-scratch
# behind. `up -d` alone reuses an already-running container unchanged, which
# would silently validate against LAST run's restored schema/data instead of
# a fresh one - confirmed directly while testing this script: a second
# restore-test run against a still-running postgres-scratch from the first
# failed with "already exists" errors, because it was never actually empty.
printf -- '--- ensuring no stale restore-test stack remains (project: %s)\n' "$restore_test_project" >&2
scripts/compose-teardown.sh --env-file .env -p "$restore_test_project" -f "$compose_file" down -v --remove-orphans || true

printf -- '--- starting postgres-scratch (project: %s)\n' "$restore_test_project" >&2
docker compose --env-file .env -p "$restore_test_project" -f "$compose_file" \
  up -d --build --force-recreate --wait postgres-scratch

printf -- '--- running restore-test\n' >&2
docker compose --env-file .env -p "$restore_test_project" -f "$compose_file" \
  run --rm restore-test

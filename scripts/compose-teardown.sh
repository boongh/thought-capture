#!/usr/bin/env bash
# A guarded wrapper around `docker compose down`.
#
# Written after docs/incidents/0001-docker-compose-down-deleted-the-real-dev-stack.md:
# an unscoped `docker compose down -v`, run during manual verification, fell
# back to this repo's own default project name (docker-compose.yml's own
# `name: thought-capture`) and deleted the real, already-running local
# development stack's Postgres and attachments volumes. "Remember to pass
# -p" is not a control - this is.
#
# Usage:
#   scripts/compose-teardown.sh -p <project-name> [additional `docker compose down` args...]
#
# Refuses to run at all without an explicit -p/--project-name. Refuses,
# with no override, to run a volume-destroying teardown (-v/--volumes)
# against this repo's real project name - that name is reserved for the
# persistent local development stack and must never be torn down by an
# automated or exploratory command. There is deliberately no bypass flag:
# the underlying `docker compose -p thought-capture down -v` command is
# still available directly, unwrapped, for the rare legitimate case of
# intentionally wiping the real stack's data - this script only removes
# it as an easy default.
set -euo pipefail

# Must match deploy/compose/docker-compose.yml's own `name:` field.
REAL_PROJECT_NAME="thought-capture"

# Docker's own flag parser (Cobra/pflag) also accepts `-v=<value>` and
# `--volumes=<value>`, not just the bare `-v`/`--volumes` this script
# originally matched - confirmed empirically: `docker compose down
# --volumes=true` and `-v=true` both deleted a real named volume in a
# throwaway test stack while the prior exact-string `case` match here let
# them through as unrecognized, un-flagged arguments. Fails closed: any
# `=`-form value other than pflag's own recognized falsy spellings (matching
# Go's strconv.ParseBool - 0/f/F/false/False/FALSE) is treated as
# volume-destroying, and the flag is only ever escalated true, never
# downgraded back to false by a later token.
_is_falsy() {
  case "$1" in
    0 | f | F | false | False | FALSE) return 0 ;;
    *) return 1 ;;
  esac
}

project_name=""
has_volumes_flag=false
args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--project-name)
      project_name="${2:-}"
      args+=("$1" "$2")
      shift 2
      ;;
    -p=*|--project-name=*)
      project_name="${1#*=}"
      args+=("$1")
      shift
      ;;
    -v|--volumes)
      has_volumes_flag=true
      args+=("$1")
      shift
      ;;
    -v=*|--volumes=*)
      if ! _is_falsy "${1#*=}"; then
        has_volumes_flag=true
      fi
      args+=("$1")
      shift
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$project_name" ]]; then
  printf 'Refusing: no -p/--project-name given.\n' >&2
  printf 'A bare "docker compose down" targets this repo'"'"'s default project name\n' >&2
  printf '("%s"), which is reserved for the real, persistent local\n' "$REAL_PROJECT_NAME" >&2
  printf 'development stack. Pass an explicit, unique -p <name> for throwaway or\n' >&2
  printf 'manual verification instead, e.g.:\n' >&2
  printf '  %s -p tc-scratch-$(date +%%s) down -v\n' "$0" >&2
  exit 1
fi

if [[ "$project_name" == "$REAL_PROJECT_NAME" && "$has_volumes_flag" == true ]]; then
  printf 'Refusing: -v/--volumes against project "%s" would delete the real\n' "$REAL_PROJECT_NAME" >&2
  printf 'local development stack'"'"'s data volumes (postgres-data, attachments).\n' >&2
  printf 'This is the exact incident recorded in\n' >&2
  printf 'docs/incidents/0001-docker-compose-down-deleted-the-real-dev-stack.md.\n' >&2
  printf '\n' >&2
  printf 'If you genuinely mean to wipe the real dev stack'"'"'s data, run the\n' >&2
  printf 'underlying command directly and explicitly - this wrapper will not do\n' >&2
  printf 'it for you:\n' >&2
  printf '  docker compose -p %s down -v\n' "$REAL_PROJECT_NAME" >&2
  exit 1
fi

printf -- '--- docker compose down (project: %s)\n' "$project_name" >&2
exec docker compose "${args[@]}"

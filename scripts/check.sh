#!/usr/bin/env bash
set -euo pipefail

script_directory="${BASH_SOURCE[0]%/*}"
if [[ "$script_directory" == "${BASH_SOURCE[0]}" ]]; then
  script_directory="."
fi
cd "$script_directory/.."

# Steps that could not run are reported explicitly and never counted as a pass.
# See AGENTS.md: never convert a failing or unavailable check into a warning.
skipped=()

printf '%s\n' "Thought Capture AI - repository checks"

resolve_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  # uv is commonly installed into the interpreter's Scripts/bin directory,
  # which is not always on PATH.
  local scripts_dir
  if command -v python >/dev/null 2>&1; then
    scripts_dir="$(python -c 'import sysconfig; print(sysconfig.get_path("scripts"))' 2>/dev/null || true)"
    for candidate in "$scripts_dir/uv" "$scripts_dir/uv.exe"; do
      if [[ -x "$candidate" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
  fi
  printf 'uv was not found. Install it with: python -m pip install uv\n' >&2
  return 1
}

uv_bin="$(resolve_uv)"

step() {
  local name="$1"
  shift
  printf '\n--- %s\n' "$name"
  if ! "$@"; then
    printf 'FAIL: %s\n' "$name" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# Repository baseline
# ---------------------------------------------------------------------------
required_paths=(
  "AGENTS.md"
  "CLAUDE.md"
  ".claude/agents/architecture-researcher.md"
  ".codex/agents/implementation-reviewer.toml"
  ".codex/agents/security-privacy-reviewer.toml"
  "README.md"
  "LICENSE"
  "docs/DESIGN.md"
  "docs/Thought-Capture-AI-System-Design.docx"
  "scripts/check.ps1"
  "scripts/check.sh"
  "pyproject.toml"
  "uv.lock"
  ".python-version"
  "env.example"
  "deploy/compose/docker-compose.yml"
  "alembic.ini"
  "migrations/env.py"
)

for path in "${required_paths[@]}"; do
  if [[ ! -e "$path" ]]; then
    printf 'Required repository file is missing: %s\n' "$path" >&2
    exit 1
  fi
done
printf '%s\n' "OK: required files present"

git diff --check
printf '%s\n' "OK: git diff --check"

# A committed .env would leak credentials. Fail loudly rather than warn.
tracked_env="$(git ls-files '.env' '.env.*')"
if [[ -n "$tracked_env" ]]; then
  printf 'Refusing to pass: environment file(s) are tracked by git: %s\n' "$tracked_env" >&2
  exit 1
fi
printf '%s\n' "OK: no tracked .env files"

# ---------------------------------------------------------------------------
# Toolchain, lint, types, tests
# ---------------------------------------------------------------------------
step "lockfile is current" "$uv_bin" lock --check
step "format (ruff)"       "$uv_bin" run ruff format --check .
step "lint (ruff)"         "$uv_bin" run ruff check .
step "types (mypy)"        "$uv_bin" run mypy
step "unit tests"          "$uv_bin" run pytest -m "not integration and not contract"

# ---------------------------------------------------------------------------
# Integration tests: require PostgreSQL from the 'core' compose profile.
# ---------------------------------------------------------------------------
printf '\n--- integration tests\n'
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if ! "$uv_bin" run pytest -m integration; then
    printf 'FAIL: integration tests\n' >&2
    exit 1
  fi
  printf '%s\n' "OK: integration tests"
else
  skipped+=("integration tests (Docker engine unavailable; start Docker, then: docker compose --profile core up -d)")
  printf '%s\n' "SKIPPED: Docker engine unavailable"
fi

# ---------------------------------------------------------------------------
printf '\n'
if [[ ${#skipped[@]} -gt 0 ]]; then
  printf '%s\n' "PASS WITH SKIPS"
  for item in "${skipped[@]}"; do
    printf '  NOT RUN: %s\n' "$item"
  done
  printf '%s\n' "These checks were not executed. Do not treat this run as full verification."
else
  printf '%s\n' "PASS: all checks"
fi

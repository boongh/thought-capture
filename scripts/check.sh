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
# apps/embedding_sidecar carries its own toolchain (Python 3.12, own
# pyproject.toml/uv.lock - docs/adr/0010 §5) and is deliberately excluded
# from the root uv workspace, so none of the four steps above ever touch it.
# Required, not skippable: this needs only uv + network access to provision
# Python 3.12 (the same way the root project's own 3.14 is provisioned), not
# Docker - a ruff/mypy/test regression here must fail the same way a root
# regression does, not silently pass because nothing ever ran it (CLAUDE.md:
# "extend both check scripts" whenever a formatter/linter/type checker/test
# suite is added).
# ---------------------------------------------------------------------------
step "embedding sidecar: lockfile is current" \
  bash -c "cd apps/embedding_sidecar && \"\$0\" lock --check" "$uv_bin"
step "embedding sidecar: format (ruff)" \
  bash -c "cd apps/embedding_sidecar && \"\$0\" run ruff format --check ." "$uv_bin"
step "embedding sidecar: lint (ruff)" \
  bash -c "cd apps/embedding_sidecar && \"\$0\" run ruff check ." "$uv_bin"
step "embedding sidecar: types (mypy)" \
  bash -c "cd apps/embedding_sidecar && \"\$0\" run mypy" "$uv_bin"
step "embedding sidecar: unit tests" \
  bash -c "cd apps/embedding_sidecar && \"\$0\" run pytest" "$uv_bin"

# ---------------------------------------------------------------------------
# Integration tests: require PostgreSQL from the 'core' compose profile.
# ---------------------------------------------------------------------------
printf '\n--- integration tests\n'
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  # Tells the integration conftest that a skipped test is a failure. An
  # unreachable database must never be reported as a passing suite.
  if ! TC_REQUIRE_INTEGRATION=1 "$uv_bin" run pytest -m integration; then
    printf 'FAIL: integration tests\n' >&2
    exit 1
  fi
  printf '%s\n' "OK: integration tests"
else
  skipped+=("integration tests (Docker engine unavailable; start Docker, then: docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core up -d --build)")
  printf '%s\n' "SKIPPED: Docker engine unavailable"
fi

# ---------------------------------------------------------------------------
# Compose config sanity: the 'ai' profile's required TC_KHOJ_* variables must
# never block a core-only deployment (review remediation, PR #17 - Compose
# interpolates every service in every `-f` file before applying `--profile`
# filtering, so a required Khoj variable living in the same file as 'core'
# silently made 'ai' a hard dependency of 'core'). 'ai' must still fail
# clearly, not silently, when its own secrets are absent.
#
# Uses a synthetic env file with only the 'core'-required variables, not the
# real `.env` - a real `.env` may already define TC_KHOJ_*, and this needs to
# prove the failure happens when they are genuinely absent, not rely on
# every developer's `.env` happening to lack them. Client-side only (no
# running daemon needed for `config`), gated on docker being installed.
# ---------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  printf '\n--- compose config sanity\n'
  core_only_env="$(mktemp)"
  ai_error_file="$(mktemp)"
  printf 'POSTGRES_PASSWORD=sanity-check-only\nTC_APP_DB_PASSWORD=sanity-check-only\n' >"$core_only_env"
  if ! docker compose --env-file "$core_only_env" -f deploy/compose/docker-compose.yml \
    --profile core config --quiet 2>/dev/null; then
    printf "FAIL: compose config sanity ('core' must validate without any TC_KHOJ_* set)\n" >&2
    rm -f "$core_only_env" "$ai_error_file"
    exit 1
  fi

  # LLM configuration forwarding (independent Codex review): the bot/worker
  # environment must actually forward TC_OPENROUTER_API_KEY and the model
  # pins into the rendered container environment, not just accept them at
  # the top-level interpolation stage - a variable can validate here and
  # still fail to reach a service if it were only referenced in, say,
  # `x-service-image` rather than `llm-env`. Just as important: api/migrate
  # must NOT receive them - `llm-env` is a separate anchor from
  # `service-env` specifically so a secret (TC_OPENROUTER_API_KEY) and the
  # model pins reach only the two services with a code path that reads them
  # (discord-bot's select, worker's organize), not every container in the
  # shared network.
  llm_env="$(mktemp)"
  printf 'POSTGRES_PASSWORD=sanity-check-only\nTC_APP_DB_PASSWORD=sanity-check-only\nTC_OPENROUTER_API_KEY=sanity-check-api-key\nTC_MODEL_ORGANIZE=sanity-check-organize-model\nTC_MODEL_SELECT=sanity-check-select-model\n' >"$llm_env"
  llm_config="$(docker compose --env-file "$llm_env" -f deploy/compose/docker-compose.yml --profile core config 2>/dev/null)"
  llm_config_status=$?
  if [[ $llm_config_status -ne 0 ]]; then
    printf "FAIL: compose config sanity (rendering 'core' config with LLM variables set failed)\n" >&2
    rm -f "$core_only_env" "$ai_error_file" "$llm_env"
    exit 1
  fi
  service_block() {
    printf '%s\n' "$llm_config" | awk -v svc="$1" '
      $0 == "  " svc ":" { in_service = 1; next }
      in_service && /^  [a-zA-Z]/ { in_service = 0 }
      in_service { print }
    '
  }
  for service in discord-bot worker; do
    service_env="$(service_block "$service")"
    if ! grep -q "TC_OPENROUTER_API_KEY: sanity-check-api-key" <<<"$service_env"; then
      printf "FAIL: compose config sanity (%s did not receive TC_OPENROUTER_API_KEY)\n" "$service" >&2
      rm -f "$core_only_env" "$ai_error_file" "$llm_env"
      exit 1
    fi
    if ! grep -q "TC_MODEL_ORGANIZE: sanity-check-organize-model" <<<"$service_env"; then
      printf "FAIL: compose config sanity (%s did not receive TC_MODEL_ORGANIZE)\n" "$service" >&2
      rm -f "$core_only_env" "$ai_error_file" "$llm_env"
      exit 1
    fi
    if ! grep -q "TC_MODEL_SELECT: sanity-check-select-model" <<<"$service_env"; then
      printf "FAIL: compose config sanity (%s did not receive TC_MODEL_SELECT)\n" "$service" >&2
      rm -f "$core_only_env" "$ai_error_file" "$llm_env"
      exit 1
    fi
  done
  for service in api migrate; do
    service_env="$(service_block "$service")"
    if grep -q "TC_OPENROUTER_API_KEY" <<<"$service_env"; then
      printf "FAIL: compose config sanity (%s must not receive TC_OPENROUTER_API_KEY, but does)\n" "$service" >&2
      rm -f "$core_only_env" "$ai_error_file" "$llm_env"
      exit 1
    fi
    if grep -qE "TC_MODEL_(ORGANIZE|SELECT|QUERY_PLAN)" <<<"$service_env"; then
      printf "FAIL: compose config sanity (%s must not receive model pins, but does)\n" "$service" >&2
      rm -f "$core_only_env" "$ai_error_file" "$llm_env"
      exit 1
    fi
  done
  rm -f "$llm_env"
  if docker compose --env-file "$core_only_env" -f deploy/compose/docker-compose.yml \
    -f deploy/compose/khoj.docker-compose.yml --profile ai config --quiet 2>"$ai_error_file"; then
    printf "FAIL: compose config sanity (expected 'ai' config to fail without TC_KHOJ_* set, but it succeeded)\n" >&2
    rm -f "$core_only_env" "$ai_error_file"
    exit 1
  fi
  if ! grep -q "TC_KHOJ_" "$ai_error_file"; then
    printf "FAIL: compose config sanity ('ai' failed, but not for a missing TC_KHOJ_* variable):\n" >&2
    cat "$ai_error_file" >&2
    rm -f "$core_only_env" "$ai_error_file"
    exit 1
  fi

  # -------------------------------------------------------------------
  # Embedding sidecar network isolation (review finding): a regression
  # that silently drops embedding-sidecar's `embedding: internal: true`
  # network attachment, or the contract-test overlay's second network
  # that restores its published port, would otherwise only surface as an
  # unreachable-sidecar SKIP below - indistinguishable from "the operator
  # simply hasn't started 'core' yet". This is a static config check
  # (client-side only, same as the sanity checks above), so it catches
  # the regression even when nothing is running. `--format json`, not
  # text grep/awk: compose's rendered YAML nests a service's own
  # `networks:` and the top-level `networks:` definitions differently,
  # and a text scan risks confusing one for the other.
  # -------------------------------------------------------------------
  base_config_json="$(docker compose --env-file "$core_only_env" -f deploy/compose/docker-compose.yml \
    --profile core config --format json 2>/dev/null)"
  if [[ -z "$base_config_json" ]]; then
    printf "FAIL: compose config sanity (rendering 'core' base config as JSON failed)\n" >&2
    rm -f "$core_only_env" "$ai_error_file"
    exit 1
  fi
  overlay_config_json="$(docker compose --env-file "$core_only_env" -f deploy/compose/docker-compose.yml \
    -f deploy/compose/embedding-sidecar.contract-test.docker-compose.yml \
    --profile core config --format json 2>/dev/null)"
  if [[ -z "$overlay_config_json" ]]; then
    printf "FAIL: compose config sanity (rendering 'core' + contract-test overlay config as JSON failed)\n" >&2
    rm -f "$core_only_env" "$ai_error_file"
    exit 1
  fi
  python_bin="$(command -v python3 || command -v python || true)"
  if [[ -z "$python_bin" ]]; then
    printf "FAIL: compose config sanity (no python interpreter available to parse compose config JSON)\n" >&2
    rm -f "$core_only_env" "$ai_error_file"
    exit 1
  fi
  if ! "$python_bin" - "$base_config_json" "$overlay_config_json" <<'PYEOF'
import json
import sys

base = json.loads(sys.argv[1])
overlay = json.loads(sys.argv[2])

base_sidecar = base["services"]["embedding-sidecar"]
base_networks = base_sidecar.get("networks") or {}
if "embedding" not in base_networks:
    print("embedding-sidecar is not attached to the 'embedding' network in the base compose file", file=sys.stderr)
    sys.exit(1)
if set(base_networks) != {"embedding"}:
    print(f"embedding-sidecar is attached to more than just 'embedding' in the base compose file: {sorted(base_networks)}", file=sys.stderr)
    sys.exit(1)

embedding_network = (base.get("networks") or {}).get("embedding") or {}
if not embedding_network.get("internal"):
    print("the 'embedding' network is not internal: true in the base compose file", file=sys.stderr)
    sys.exit(1)

overlay_sidecar = overlay["services"]["embedding-sidecar"]
if not overlay_sidecar.get("ports"):
    print("embedding-sidecar has no published port under the contract-test overlay", file=sys.stderr)
    sys.exit(1)

print("OK: embedding-sidecar network isolation config is as expected")
PYEOF
  then
    printf "FAIL: compose config sanity (embedding-sidecar network isolation assertion failed - see above)\n" >&2
    rm -f "$core_only_env" "$ai_error_file"
    exit 1
  fi

  rm -f "$core_only_env" "$ai_error_file"
  printf '%s\n' "OK: compose config sanity"
else
  skipped+=("compose config sanity (Docker engine unavailable)")
  printf '\n%s\n' "SKIPPED: compose config sanity (Docker engine unavailable)"
fi

# ---------------------------------------------------------------------------
# Contract tests: require the pinned Khoj instance from the 'ai' compose
# profile (docs/adr/0003). Unlike PostgreSQL/'core' above, 'ai' is a new,
# heavy, optional-so-far dependency - reachability is probed explicitly
# (Docker being up does not imply this profile was ever started) and an
# unreachable Khoj is a skip, not a hard failure, until Phase 2 makes it
# required.
#
# `--ignore=tests/contract/embedding_sidecar`, not a `tests/contract/khoj`
# path filter: some Khoj contract tests deliberately live under
# `tests/integration` instead (test_khoj_index_sync_contract.py,
# test_khoj_semantic_search_contract.py - see either file's own docstring),
# because they need the disposable-database fixtures only
# `tests/integration/conftest.py` provides, while still carrying
# `pytest.mark.contract` so a skip here is required, not silent. A bare
# `tests/contract/khoj` path scope was found to never collect them at all
# (Codex review of PR #31) - this selects everything marked `contract`
# except the embedding sidecar's own suite (which gets its own gate below),
# matching what the original unscoped `pytest -m contract` collected before
# this stage was split in two.
# ---------------------------------------------------------------------------
printf '\n--- contract tests (khoj)\n'
khoj_url="${TC_KHOJ_BASE_URL:-http://127.0.0.1:42110}"
if curl --silent --fail --max-time 3 "$khoj_url/api/search?q=check" >/dev/null 2>&1; then
  if ! TC_REQUIRE_CONTRACT=1 "$uv_bin" run pytest -m contract --ignore=tests/contract/embedding_sidecar; then
    printf 'FAIL: contract tests (khoj)\n' >&2
    exit 1
  fi
  printf '%s\n' "OK: contract tests (khoj)"
else
  skipped+=("contract tests (khoj) (Khoj unreachable at $khoj_url; docker compose --env-file .env -f deploy/compose/docker-compose.yml -f deploy/compose/khoj.docker-compose.yml --profile ai up -d)")
  printf '%s\n' "SKIPPED: Khoj unreachable at $khoj_url"
fi

# ---------------------------------------------------------------------------
# Contract tests: require the embedding sidecar from the 'core' compose
# profile (docs/adr/0010). First-party and part of 'core', not an optional
# add-on the way Khoj/'ai' is - still gated on explicit reachability, not a
# hard failure, since 'core' being started at all is not implied by Docker
# merely being available (same reasoning as the integration-test gate above).
# ---------------------------------------------------------------------------
printf '\n--- contract tests (embedding sidecar)\n'
embedding_sidecar_url="${TC_EMBEDDING_SIDECAR_BASE_URL:-http://127.0.0.1:8081}"
if curl --silent --fail --max-time 3 "$embedding_sidecar_url/health" >/dev/null 2>&1; then
  if ! TC_REQUIRE_CONTRACT=1 "$uv_bin" run pytest -m contract tests/contract/embedding_sidecar; then
    printf 'FAIL: contract tests (embedding sidecar)\n' >&2
    exit 1
  fi
  printf '%s\n' "OK: contract tests (embedding sidecar)"
else
  skipped+=("contract tests (embedding sidecar) (unreachable at $embedding_sidecar_url; docker compose --env-file .env -f deploy/compose/docker-compose.yml -f deploy/compose/embedding-sidecar.contract-test.docker-compose.yml --profile core up -d --build embedding-sidecar)")
  printf '%s\n' "SKIPPED: embedding sidecar unreachable at $embedding_sidecar_url"
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

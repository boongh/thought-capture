#!/usr/bin/env bash
set -euo pipefail

script_directory="${BASH_SOURCE[0]%/*}"
if [[ "$script_directory" == "${BASH_SOURCE[0]}" ]]; then
  script_directory="."
fi
cd "$script_directory/.."

printf '%s\n' "Thought Capture AI - repository checks"

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
)

for path in "${required_paths[@]}"; do
  if [[ ! -e "$path" ]]; then
    printf 'Required repository file is missing: %s\n' "$path" >&2
    exit 1
  fi
done

git diff --check

printf '%s\n' "PASS: design-stage repository baseline"
printf '%s\n' "NOTE: application checks are not configured yet."
printf '%s\n' "Future implementation must extend this script with formatting, linting, typing, migrations, tests, and service contract checks."

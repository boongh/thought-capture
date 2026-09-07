# Development guide

This guide collects repository setup and contributor-oriented technical information. For running the local service after setup, see [OPERATING.md](OPERATING.md). The accepted architecture and product constraints are in [DESIGN.md](DESIGN.md).

## Toolchain and setup

The workspace uses [uv](https://docs.astral.sh/uv/). CPython is pinned to the patch version in `.python-version` and installed by uv itself.

```bash
python -m pip install uv
uv python install 3.14.7
uv sync --all-packages
```

Copy the configuration template and fill in the required local values. `.env` is gitignored and must never be committed.

```bash
cp env.example .env
```

For the required settings and local service commands, continue in [OPERATING.md](OPERATING.md).

## Verification

Run the repository checks before declaring a change complete.

```bash
./scripts/check.ps1
```

On Bash, run `./scripts/check.sh`. The checks validate the lockfile, formatting, linting, types, and unit suite. They also run integration tests when Docker is reachable; unavailable integration or contract dependencies are reported as `NOT RUN`, not as a full verification pass.

## Repository map

```text
apps/
  api/                 public gateway and read API
  discord_bot/         Discord Gateway adapter
  worker/              migration bootstrap and organization scheduler
packages/
  domain/              entities, commands, policies, ports
  application/         use cases and orchestration
  infrastructure/      PostgreSQL, OpenRouter, Khoj, and object-store adapters
migrations/            Alembic migrations
tests/                 unit, integration, and contract tests
docs/                  design, operations, and architecture decisions
deploy/compose/        local Compose stack and optional service overlays
```

## Working agreements

Claude is the primary abstract architect and planner; Codex independently evaluates and verifies plans and changes. Their repository instructions are in `CLAUDE.md` and `AGENTS.md`. When both are active, they work in separate branches or worktrees.

Changes must preserve the accepted design. A change to an architecture decision requires an ADR and a corresponding update to `docs/DESIGN.md`.

Never commit secrets, exported memories, attachments, backups, or other personal-memory content. Use synthetic data in tests and keep message bodies, tokens, and signed attachment URLs out of normal diagnostic output.

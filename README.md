# Thought Capture AI

Thought Capture AI is a self-hosted, single-user-first personal memory system. A Discord bot captures low-friction text and file attachments, an append-only store preserves the original record, and a scheduled pipeline turns captured fragments into cited daily and evolving documents. The intended retrieval architecture reserves Khoj for semantic retrieval and Ask/RAG and the application for deterministic date, time, entity, and lexical search; that Phase 2 capability is not implemented yet.

## Project status

Implementation in progress. The `core` Compose profile now runs PostgreSQL,
one-shot migration and workspace bootstrap, the Discord capture bot, the API,
and the worker. It supports durable capture, organization of closed capture
windows, versioned documents and entities, and queued Discord digest delivery.
The default safe-mode model configuration remains deliberately offline until a
reviewed provider model is registered. Khoj integration and the future unified
custom UI are not runnable yet; the current read-only operator debug pages are
an inspection tool, not that future UI.

The implementation target and acceptance criteria are defined in
[docs/DESIGN.md](docs/DESIGN.md). That document remains the project anchor:
changes that alter its accepted decisions require an Architecture Decision
Record (ADR) and a design version update.

## First release outcome

By the end of Release 1, the owner can:

1. Send English text to an allowlisted Discord channel or DM and receive a durable acknowledgement.
2. Attach files or images for archival without OCR, vision analysis, or transcription.
3. Receive a Discord digest at 20:00 in the configured IANA timezone.
4. Run `/organize` to force a safe, repeatable organization pass.
5. Search with semantic meaning plus exact text, date/time, source, type, and entity constraints.
6. Ask cited questions through self-hosted Khoj's existing interface and the gateway API.
7. inspect every generated revision and restore an older state by creating a new revision.

## Intended stack

- Python 3.14 for first-party services
- FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, APScheduler, and discord.py
- PostgreSQL with `pg_trgm` and full-text search as the canonical database
- Self-hosted Khoj, pinned and isolated in its own container
- OpenRouter for initial model access through an OpenAI-compatible API
- Docker Compose for local hosting; OCI images remain portable to a later Linux VPS

Khoj uses a separate runtime because its current source package supports Python 3.10-3.12, while this project's services use Python 3.14.

## Local development

The workspace uses [uv](https://docs.astral.sh/uv/). CPython is pinned to the
patch version in `.python-version` and installed by uv itself.

```bash
python -m pip install uv
uv python install 3.14.7
uv sync --all-packages
```

Copy the configuration template and fill it in. `.env` is gitignored and must
never be committed:

```bash
cp env.example .env
```

Start the runnable local stack. `--env-file .env` is required: Compose resolves
a bare `.env` relative to the compose file, not the repository root, so without
it the required configuration is unset and the command fails.

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core up -d --build
```

The one-shot `migrate` service applies Alembic migrations and seeds the
workspace before the API and bot start. Check readiness and inspect service
state with:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core ps
curl http://127.0.0.1:8080/health/ready
```

See [docs/OPERATING.md](docs/OPERATING.md) for required configuration, logs,
API access, database inspection, and shutdown commands.

Run the checks before declaring any change complete:

```bash
./scripts/check.ps1
```

The check script runs the lockfile check, `ruff format --check`, `ruff check`,
`mypy`, and the unit suite, then the integration suite if a Docker engine is
reachable. When Docker is unavailable it reports the integration tests as
`NOT RUN` rather than passing silently — a run with skips is not full
verification.

## Current repository shape

```text
thought-capture-ai/
  apps/
    api/                 # public gateway and read API
    discord_bot/         # Discord Gateway adapter
    worker/              # migration bootstrap and organization scheduler
  packages/
    domain/              # entities, commands, policies, ports
    application/         # use cases and orchestration
    infrastructure/      # PostgreSQL, OpenRouter, and object-store adapters
  migrations/            # Alembic migrations
  tests/                 # unit and PostgreSQL integration tests
  docs/
    DESIGN.md            # canonical design
    adr/                  # decision records created during implementation
  deploy/
    compose/              # current local Compose stack
```

## Development sequence

Implementation follows the release gates in the design document. Durable
capture, scheduled organization, derived document/entity persistence, digest
delivery, and operator inspection endpoints are now represented in the stack.
Khoj retrieval/Ask, backup and restore operations, and the future unified
custom UI remain later slices.

## AI-assisted development workflow

Claude is the primary abstract architect and planner; Codex is the secondary evaluator and verification agent. Their repository instructions live in `CLAUDE.md` and `AGENTS.md`. They must work in separate branches or worktrees when active concurrently.

Before completing any repository change, run `./scripts/check.ps1` in PowerShell or `./scripts/check.sh` in Bash. They must be expanded alongside implementation as migrations, integration suites, and service contract checks are introduced.

## Security baseline

Never commit `.env`, Discord tokens, OpenRouter keys, Khoj credentials, database passwords, exported memories, attachments, or backups. Local services bind to loopback by default. External access is not part of Release 1.

## Design sources

The design was verified against official Discord, Khoj, OpenRouter, Python, and PostgreSQL-facing project documentation on 2026-08-30. Links and compatibility notes are recorded in the References section of [docs/DESIGN.md](docs/DESIGN.md).

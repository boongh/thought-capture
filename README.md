# Thought Capture AI

Thought Capture AI is a self-hosted, single-user-first personal memory system. A Discord bot captures low-friction text and file attachments, an append-only store preserves the original record, and a scheduled pipeline turns captured fragments into cited daily and evolving documents. Khoj supplies semantic retrieval and Ask/RAG; the application adds deterministic date, time, entity, and lexical search.

## Project status

Design-first. The implementation target and acceptance criteria are defined in [docs/DESIGN.md](docs/DESIGN.md). That document is the project anchor: changes that alter its accepted decisions require an Architecture Decision Record (ADR) and a design version update.

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

## Planned repository shape

```text
thought-capture-ai/
  apps/
    api/                 # public gateway and read API
    discord_bot/         # Discord Gateway adapter
    worker/              # organize, index, digest, retry jobs
  packages/
    domain/              # entities, commands, policies, ports
    application/         # use cases and orchestration
    infrastructure/      # PostgreSQL, Khoj, OpenRouter, object store adapters
  prompts/               # versioned structured-output prompts
  migrations/            # Alembic migrations
  tests/                 # unit, integration, contract, eval, end-to-end
  docs/
    DESIGN.md            # canonical design
    adr/                  # decision records created during implementation
  deploy/
    compose/              # local and VPS Compose overlays
  exports/               # generated locally; ignored by Git
```

## Development sequence

Implementation must follow the release gates in the design document. The first vertical slice is Discord text -> append-only PostgreSQL -> acknowledgement. Khoj, LLM organization, and the future custom UI are deliberately added only after capture durability is proven.

## AI-assisted development workflow

Claude is the primary abstract architect and planner; Codex is the secondary evaluator and verification agent. Their repository instructions live in `CLAUDE.md` and `AGENTS.md`. They must work in separate branches or worktrees when active concurrently.

Before completing any repository change, run `./scripts/check.ps1` in PowerShell or `./scripts/check.sh` in Bash. These scripts currently validate the design-stage baseline only. They must be expanded alongside implementation to include formatting, linting, typing, migrations, tests, and service contract checks.

## Security baseline

Never commit `.env`, Discord tokens, OpenRouter keys, Khoj credentials, database passwords, exported memories, attachments, or backups. Local services bind to loopback by default. External access is not part of Release 1.

## Design sources

The design was verified against official Discord, Khoj, OpenRouter, Python, and PostgreSQL-facing project documentation on 2026-08-30. Links and compatibility notes are recorded in the References section of [docs/DESIGN.md](docs/DESIGN.md).

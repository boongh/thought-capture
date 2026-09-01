"""Derived layer: runs, immutable revisions, provenance, entities, journals.

Implements docs/DESIGN.md 6.3, 6.4, 6.5, and 7.3.5, plus the LLM call journal
required by ADR-0008 for deterministic rebuild.

Two append-only guarantees are added here, alongside the one on `thoughts`:

- `document_revisions` are immutable full snapshots. Reversion is forward
  motion: restoring an old state writes a *new* revision whose parent is the
  current one (docs/DESIGN.md 3.4). Nothing is ever edited in place.
- `llm_calls` is a journal. A journal that can be rewritten cannot be used to
  reproduce anything.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tc_app"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Runs (docs/DESIGN.md 6.3)
    #
    # `replay` and `regenerate` extend the design's kind list for ADR-0008:
    # replay rebuilds derived output from the journal without calling a
    # provider; regenerate re-derives it under a different model or prompt.
    # `context_recall` and `context_degraded` record the stage metric and the
    # selector-failure degradation required by docs/DESIGN.md 7.3.4 and 7.3.5.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE runs (
          id uuid PRIMARY KEY,
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          kind text NOT NULL CHECK (kind IN
            ('organize','force_organize','khoj_sync','export','restore_test','reembed',
             'replay','regenerate')),
          window_start timestamptz,
          window_end timestamptz,
          status text NOT NULL CHECK (status IN
            ('queued','running','succeeded','partial','failed')),
          prompt_version text,
          model_provider text,
          model_id text,
          embedding_model_id text,
          input_tokens bigint,
          output_tokens bigint,
          estimated_cost_usd numeric(12,6),
          context_recall numeric(4,3) CHECK (context_recall BETWEEN 0 AND 1),
          context_degraded boolean NOT NULL DEFAULT false,
          replay_of_run_id uuid REFERENCES runs(id),
          started_at timestamptz,
          finished_at timestamptz,
          error_code text,
          error_detail text,
          created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX runs_workspace_created_idx
          ON runs (workspace_id, created_at DESC)
    """)

    # ------------------------------------------------------------------
    # Documents and immutable revisions (docs/DESIGN.md 6.3)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE documents (
          id uuid PRIMARY KEY,
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          kind text NOT NULL CHECK (kind IN
            ('daily_digest','person','project','place','organization','topic','decision','todo')),
          stable_key text NOT NULL,
          title text NOT NULL,
          current_revision_id uuid,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (workspace_id, kind, stable_key)
        )
    """)

    op.execute("""
        CREATE TABLE document_revisions (
          id uuid PRIMARY KEY,
          document_id uuid NOT NULL REFERENCES documents(id),
          parent_revision_id uuid REFERENCES document_revisions(id),
          run_id uuid NOT NULL REFERENCES runs(id),
          revision_number integer NOT NULL,
          body_markdown text NOT NULL,
          body_sha256 char(64) NOT NULL,
          change_summary text NOT NULL,
          change_kind text NOT NULL CHECK (change_kind IN
            ('create','organize','manual_restore','supersede','regenerate')),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (document_id, revision_number)
        )
    """)

    op.execute("""
        ALTER TABLE documents ADD CONSTRAINT documents_current_revision_fk
          FOREIGN KEY (current_revision_id) REFERENCES document_revisions(id)
    """)

    op.execute("""
        CREATE INDEX document_revisions_document_idx
          ON document_revisions (document_id, revision_number DESC)
    """)

    op.execute("""
        CREATE TABLE revision_sources (
          revision_id uuid NOT NULL REFERENCES document_revisions(id),
          thought_id bigint NOT NULL REFERENCES thoughts(id),
          support_type text NOT NULL CHECK (support_type IN ('direct','context')),
          PRIMARY KEY (revision_id, thought_id)
        )
    """)
    # "Every generated claim is traceable to one or more raw thought IDs"
    # (docs/DESIGN.md 1.1) is a lookup in both directions.
    op.execute("CREATE INDEX revision_sources_thought_idx ON revision_sources (thought_id)")

    # ------------------------------------------------------------------
    # Entities (docs/DESIGN.md 6.4)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE entities (
          id uuid PRIMARY KEY,
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          entity_type text NOT NULL CHECK (entity_type IN
            ('person','project','place','organization','topic')),
          canonical_name text NOT NULL,
          normalized_name text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (workspace_id, entity_type, normalized_name)
        )
    """)
    # The alias signal in docs/DESIGN.md 7.3.2 matches window text against
    # names and aliases, including misspellings, so both need trigram support.
    op.execute("""
        CREATE INDEX entities_normalized_trgm_idx
          ON entities USING gin (normalized_name gin_trgm_ops)
    """)

    op.execute("""
        CREATE TABLE entity_aliases (
          entity_id uuid NOT NULL REFERENCES entities(id),
          alias text NOT NULL,
          normalized_alias text NOT NULL,
          PRIMARY KEY (entity_id, normalized_alias)
        )
    """)
    op.execute("""
        CREATE INDEX entity_aliases_normalized_trgm_idx
          ON entity_aliases USING gin (normalized_alias gin_trgm_ops)
    """)

    op.execute("""
        CREATE TABLE entity_mentions (
          entity_id uuid NOT NULL REFERENCES entities(id),
          thought_id bigint REFERENCES thoughts(id),
          revision_id uuid REFERENCES document_revisions(id),
          run_id uuid NOT NULL REFERENCES runs(id),
          surface_form text NOT NULL,
          confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
          CHECK ((thought_id IS NOT NULL) <> (revision_id IS NOT NULL))
        )
    """)
    # The recency and co-occurrence signals both scan mentions by entity.
    op.execute("CREATE INDEX entity_mentions_entity_idx ON entity_mentions (entity_id)")
    op.execute(
        "CREATE INDEX entity_mentions_thought_idx ON entity_mentions (thought_id)"
        " WHERE thought_id IS NOT NULL"
    )

    # ------------------------------------------------------------------
    # Capture windows (docs/DESIGN.md 4.2, 6.5)
    #
    # A window is (previous_successful_cutoff, current_cutoff], not a calendar
    # day, so that a late-evening thought is never lost between days.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE capture_windows (
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          window_start timestamptz NOT NULL,
          window_end timestamptz NOT NULL,
          status text NOT NULL CHECK (status IN ('open','closed','organized')),
          organized_by_run_id uuid REFERENCES runs(id),
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (workspace_id, window_end),
          CHECK (window_end > window_start)
        )
    """)
    # The catch-up job asks for closed, unorganized windows oldest-first.
    op.execute("""
        CREATE INDEX capture_windows_unorganized_idx
          ON capture_windows (workspace_id, window_end)
          WHERE status <> 'organized'
    """)

    # ------------------------------------------------------------------
    # LLM call journal (ADR-0008)
    #
    # Holds the exact request and response of every model call, which is what
    # makes `rebuild --from-journal` deterministic. It contains raw personal
    # content, because prompts embed thought bodies: it lives beside `thoughts`
    # in the canonical database, is never logged, and is never exported to Khoj.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE llm_calls (
          id uuid PRIMARY KEY,
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          run_id uuid NOT NULL REFERENCES runs(id),
          step text NOT NULL CHECK (step IN
            ('select','entity_extract','organize','query_plan','repair')),
          sequence integer NOT NULL,
          prompt_version text NOT NULL,
          schema_version text NOT NULL,
          model_requested text NOT NULL,
          model_served text,
          provider text,
          generation_id text,
          request_messages jsonb NOT NULL,
          request_params jsonb NOT NULL,
          response_raw jsonb,
          input_tokens bigint,
          output_tokens bigint,
          estimated_cost_usd numeric(12,6),
          latency_ms integer,
          error_code text,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (run_id, step, sequence)
        )
    """)

    # ------------------------------------------------------------------
    # Context assembly observability (docs/DESIGN.md 7.3.5)
    #
    # Without this, a selection failure and a generation failure look identical
    # from the output alone, so an incorrect digest cannot be attributed.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE run_context_selections (
          run_id uuid NOT NULL REFERENCES runs(id),
          document_id uuid NOT NULL REFERENCES documents(id),
          signals text[] NOT NULL,
          inclusion text NOT NULL CHECK (inclusion IN ('index_only','partial','full')),
          body_tokens integer NOT NULL DEFAULT 0,
          referenced_in_output boolean NOT NULL DEFAULT false,
          PRIMARY KEY (run_id, document_id)
        )
    """)

    # ------------------------------------------------------------------
    # Append-only enforcement for the immutable tables.
    # Same shape as the trigger on `thoughts` (ADR-0001), including the
    # restore-only escape.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE FUNCTION reject_derived_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF coalesce(current_setting('tc.allow_thought_restore', true), 'off') = 'on' THEN
            RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
          END IF;
          RAISE EXCEPTION
            '% is append-only: % rejected. Write a new row instead.',
            TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
        END;
        $$
    """)

    for table in ("document_revisions", "llm_calls"):
        op.execute(f"""
            CREATE TRIGGER {table}_append_only
              BEFORE UPDATE OR DELETE ON {table}
              FOR EACH ROW EXECUTE FUNCTION reject_derived_mutation()
        """)

    # ------------------------------------------------------------------
    # Least-privilege grants.
    # UPDATE only where the running system genuinely mutates state: run status,
    # a document's current revision pointer, window status, and the
    # referenced_in_output flag written after generation.
    # ------------------------------------------------------------------
    for table in ("runs", "documents", "capture_windows", "run_context_selections", "entities"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {APP_ROLE}")

    for table in (
        "document_revisions",
        "revision_sources",
        "entity_aliases",
        "entity_mentions",
        "llm_calls",
    ):
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    for table in (
        "runs",
        "documents",
        "capture_windows",
        "run_context_selections",
        "entities",
        "document_revisions",
        "revision_sources",
        "entity_aliases",
        "entity_mentions",
        "llm_calls",
    ):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")

    for table in ("document_revisions", "llm_calls"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_derived_mutation()")

    op.execute("DROP TABLE IF EXISTS run_context_selections")
    op.execute("DROP TABLE IF EXISTS llm_calls")
    op.execute("DROP TABLE IF EXISTS capture_windows")
    op.execute("DROP TABLE IF EXISTS entity_mentions")
    op.execute("DROP TABLE IF EXISTS entity_aliases")
    op.execute("DROP TABLE IF EXISTS entities")
    op.execute("DROP TABLE IF EXISTS revision_sources")
    # The circular reference between documents and its current revision must be
    # broken before either table can be dropped.
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_current_revision_fk")
    op.execute("DROP TABLE IF EXISTS document_revisions")
    op.execute("DROP TABLE IF EXISTS documents")
    op.execute("DROP TABLE IF EXISTS runs")

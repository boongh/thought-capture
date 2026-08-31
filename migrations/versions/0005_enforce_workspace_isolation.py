"""Make workspace ownership an enforced invariant rather than a convention.

Review of migrations 0001 and 0004 established that a row owned by workspace A
could reference data owned by workspace B: every foreign key was single-column,
so nothing tied a revision, mention, journal entry, or context selection to the
workspace of the thing it pointed at. docs/DESIGN.md 6 states that every domain
row carries workspace ownership; until now that was true of the *columns* but
not of the *relationships*.

Three further integrity gaps found in the same review are closed here:

1. ``thoughts`` was unique on ``(source, source_message_id)`` globally, so an
   API idempotency key reused in a second workspace would return the first
   workspace's thought instead of capturing the caller's.
2. ``documents.current_revision_id`` and ``document_revisions.parent_revision_id``
   were global, so a document could point at another document's revision.
3. ``body_sha256`` was an unvalidated string, so the checksum that
   ``rebuild --verify`` depends on guaranteed nothing.

And the shared restore bypass is split: the raw-thought restore escape must not
also unlock immutable derived revisions and the model-call journal.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tc_app"

# Child tables that gain a workspace_id, and the parent to backfill it from.
BACKFILL: tuple[tuple[str, str], ...] = (
    ("document_revisions", "SELECT workspace_id FROM documents d WHERE d.id = t.document_id"),
    (
        "revision_sources",
        "SELECT r.workspace_id FROM document_revisions r WHERE r.id = t.revision_id",
    ),
    ("entity_aliases", "SELECT e.workspace_id FROM entities e WHERE e.id = t.entity_id"),
    ("entity_mentions", "SELECT e.workspace_id FROM entities e WHERE e.id = t.entity_id"),
    ("run_context_selections", "SELECT r.workspace_id FROM runs r WHERE r.id = t.run_id"),
    ("thought_attachments", "SELECT th.workspace_id FROM thoughts th WHERE th.id = t.thought_id"),
)

# (table, constraint to drop, new name, referencing columns, referenced target)
#
# Every relationship that could previously cross a workspace boundary. The two
# lineage keys are scoped by document rather than workspace, which is stricter:
# a revision's parent, and a document's current pointer, must belong to that
# same document.
COMPOSITE_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "document_revisions",
        "document_revisions_document_id_fkey",
        "document_revisions_document_ws_fk",
        "(document_id, workspace_id)",
        "documents (id, workspace_id)",
    ),
    (
        "document_revisions",
        "document_revisions_run_id_fkey",
        "document_revisions_run_ws_fk",
        "(run_id, workspace_id)",
        "runs (id, workspace_id)",
    ),
    (
        "document_revisions",
        "document_revisions_parent_revision_id_fkey",
        "document_revisions_parent_document_fk",
        "(parent_revision_id, document_id)",
        "document_revisions (id, document_id)",
    ),
    (
        "documents",
        "documents_current_revision_fk",
        "documents_current_revision_document_fk",
        "(current_revision_id, id)",
        "document_revisions (id, document_id)",
    ),
    (
        "revision_sources",
        "revision_sources_revision_id_fkey",
        "revision_sources_revision_ws_fk",
        "(revision_id, workspace_id)",
        "document_revisions (id, workspace_id)",
    ),
    (
        "revision_sources",
        "revision_sources_thought_id_fkey",
        "revision_sources_thought_ws_fk",
        "(thought_id, workspace_id)",
        "thoughts (id, workspace_id)",
    ),
    (
        "entity_aliases",
        "entity_aliases_entity_id_fkey",
        "entity_aliases_entity_ws_fk",
        "(entity_id, workspace_id)",
        "entities (id, workspace_id)",
    ),
    (
        "entity_mentions",
        "entity_mentions_entity_id_fkey",
        "entity_mentions_entity_ws_fk",
        "(entity_id, workspace_id)",
        "entities (id, workspace_id)",
    ),
    (
        "entity_mentions",
        "entity_mentions_run_id_fkey",
        "entity_mentions_run_ws_fk",
        "(run_id, workspace_id)",
        "runs (id, workspace_id)",
    ),
    (
        "entity_mentions",
        "entity_mentions_thought_id_fkey",
        "entity_mentions_thought_ws_fk",
        "(thought_id, workspace_id)",
        "thoughts (id, workspace_id)",
    ),
    (
        "entity_mentions",
        "entity_mentions_revision_id_fkey",
        "entity_mentions_revision_ws_fk",
        "(revision_id, workspace_id)",
        "document_revisions (id, workspace_id)",
    ),
    (
        "llm_calls",
        "llm_calls_run_id_fkey",
        "llm_calls_run_ws_fk",
        "(run_id, workspace_id)",
        "runs (id, workspace_id)",
    ),
    (
        "run_context_selections",
        "run_context_selections_run_id_fkey",
        "run_context_selections_run_ws_fk",
        "(run_id, workspace_id)",
        "runs (id, workspace_id)",
    ),
    (
        "run_context_selections",
        "run_context_selections_document_id_fkey",
        "run_context_selections_document_ws_fk",
        "(document_id, workspace_id)",
        "documents (id, workspace_id)",
    ),
    (
        "capture_windows",
        "capture_windows_organized_by_run_id_fkey",
        "capture_windows_run_ws_fk",
        "(organized_by_run_id, workspace_id)",
        "runs (id, workspace_id)",
    ),
    (
        "runs",
        "runs_replay_of_run_id_fkey",
        "runs_replay_of_run_ws_fk",
        "(replay_of_run_id, workspace_id)",
        "runs (id, workspace_id)",
    ),
    (
        "thought_attachments",
        "thought_attachments_thought_id_fkey",
        "thought_attachments_thought_ws_fk",
        "(thought_id, workspace_id)",
        "thoughts (id, workspace_id)",
    ),
)


# What each composite key replaced, so downgrade restores 0004's schema exactly
# rather than leaving the relationships unconstrained.
_SINGLE_COLUMN_KEYS: dict[str, str] = {
    "document_revisions_document_id_fkey": "FOREIGN KEY (document_id) REFERENCES documents(id)",
    "document_revisions_run_id_fkey": "FOREIGN KEY (run_id) REFERENCES runs(id)",
    "document_revisions_parent_revision_id_fkey": (
        "FOREIGN KEY (parent_revision_id) REFERENCES document_revisions(id)"
    ),
    "documents_current_revision_fk": (
        "FOREIGN KEY (current_revision_id) REFERENCES document_revisions(id)"
    ),
    "revision_sources_revision_id_fkey": (
        "FOREIGN KEY (revision_id) REFERENCES document_revisions(id)"
    ),
    "revision_sources_thought_id_fkey": "FOREIGN KEY (thought_id) REFERENCES thoughts(id)",
    "entity_aliases_entity_id_fkey": "FOREIGN KEY (entity_id) REFERENCES entities(id)",
    "entity_mentions_entity_id_fkey": "FOREIGN KEY (entity_id) REFERENCES entities(id)",
    "entity_mentions_run_id_fkey": "FOREIGN KEY (run_id) REFERENCES runs(id)",
    "entity_mentions_thought_id_fkey": "FOREIGN KEY (thought_id) REFERENCES thoughts(id)",
    "entity_mentions_revision_id_fkey": (
        "FOREIGN KEY (revision_id) REFERENCES document_revisions(id)"
    ),
    "llm_calls_run_id_fkey": "FOREIGN KEY (run_id) REFERENCES runs(id)",
    "run_context_selections_run_id_fkey": "FOREIGN KEY (run_id) REFERENCES runs(id)",
    "run_context_selections_document_id_fkey": (
        "FOREIGN KEY (document_id) REFERENCES documents(id)"
    ),
    "capture_windows_organized_by_run_id_fkey": (
        "FOREIGN KEY (organized_by_run_id) REFERENCES runs(id)"
    ),
    "runs_replay_of_run_id_fkey": "FOREIGN KEY (replay_of_run_id) REFERENCES runs(id)",
    "thought_attachments_thought_id_fkey": "FOREIGN KEY (thought_id) REFERENCES thoughts(id)",
}


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Workspace-scoped idempotency for raw capture.
    #
    # The old constraint made an idempotency key global. Two workspaces using
    # the same key would collide, and the second would silently receive the
    # first's thought ID.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE thoughts DROP CONSTRAINT thoughts_source_source_message_id_key")
    op.execute("""
        ALTER TABLE thoughts
          ADD CONSTRAINT thoughts_workspace_source_message_key
          UNIQUE (workspace_id, source, source_message_id)
    """)

    # ------------------------------------------------------------------
    # 2. Targets for composite foreign keys.
    #
    # A composite FK can only reference a uniquely-constrained column pair, so
    # each parent gains UNIQUE (id, workspace_id). These are redundant with the
    # primary key by definition and cost one index each; that is the price of
    # making the relationship checkable by the database.
    # ------------------------------------------------------------------
    for table in ("thoughts", "runs", "documents", "entities"):
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_id_workspace_key UNIQUE (id, workspace_id)"
        )

    # ------------------------------------------------------------------
    # 3. Give every child table its own workspace column, backfilled from its
    #    parent, then made NOT NULL.
    # ------------------------------------------------------------------
    for table, source in BACKFILL:
        op.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id uuid")
        op.execute(f"UPDATE {table} AS t SET workspace_id = ({source})")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN workspace_id SET NOT NULL")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_workspace_fk"
            f" FOREIGN KEY (workspace_id) REFERENCES workspaces(id)"
        )

    # document_revisions needs its own composite targets before children point
    # at it, and (id, document_id) so lineage cannot cross documents.
    op.execute("""
        ALTER TABLE document_revisions
          ADD CONSTRAINT document_revisions_id_workspace_key UNIQUE (id, workspace_id)
    """)
    op.execute("""
        ALTER TABLE document_revisions
          ADD CONSTRAINT document_revisions_id_document_key UNIQUE (id, document_id)
    """)

    # ------------------------------------------------------------------
    # 4. Replace every single-column reference with a workspace-aware one.
    # ------------------------------------------------------------------
    for table, dropped, name, columns, target in COMPOSITE_KEYS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {dropped}")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY {columns} REFERENCES {target}"
        )

    # ------------------------------------------------------------------
    # 5. The checksum becomes an integrity guarantee.
    #
    # `rebuild --verify` re-renders Markdown and compares body_sha256
    # (docs/DESIGN.md Appendix B). A checksum nobody validates on write proves
    # nothing on read.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE FUNCTION enforce_revision_checksum() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.body_sha256 <> encode(sha256(convert_to(NEW.body_markdown, 'UTF8')), 'hex') THEN
            RAISE EXCEPTION
              'body_sha256 does not match body_markdown; the revision checksum must be authoritative'
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER document_revisions_checksum
          BEFORE INSERT ON document_revisions
          FOR EACH ROW EXECUTE FUNCTION enforce_revision_checksum()
    """)

    # ------------------------------------------------------------------
    # 6. Split the restore bypass.
    #
    # 0004 reused `tc.allow_thought_restore` for derived tables, so restoring a
    # raw thought from backup also unlocked rewriting document revisions and
    # the model-call journal. Those are separate operations with separate
    # blast radii and now need separate, explicit settings.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION reject_derived_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF coalesce(current_setting('tc.allow_derived_restore', true), 'off') = 'on' THEN
            RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
          END IF;
          RAISE EXCEPTION
            '% is append-only: % rejected. Write a new row instead.',
            TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
        END;
        $$
    """)

    # ------------------------------------------------------------------
    # 7. Column-level grants.
    #
    # Table-wide UPDATE let the application change workspace_id and other
    # columns that are supposed to be immutable after insert. The running
    # system only ever advances run state, retargets a document's current
    # revision, moves a window's status, and flags a context selection.
    # ------------------------------------------------------------------
    op.execute(
        f"REVOKE UPDATE ON runs, documents, capture_windows, run_context_selections, entities FROM {APP_ROLE}"
    )
    op.execute(f"""
        GRANT UPDATE (status, started_at, finished_at, error_code, error_detail,
                      input_tokens, output_tokens, estimated_cost_usd,
                      context_recall, context_degraded, prompt_version,
                      model_provider, model_id, embedding_model_id)
          ON runs TO {APP_ROLE}
    """)
    op.execute(f"GRANT UPDATE (current_revision_id, title) ON documents TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (status, organized_by_run_id) ON capture_windows TO {APP_ROLE}")
    op.execute(
        f"GRANT UPDATE (referenced_in_output, body_tokens) ON run_context_selections TO {APP_ROLE}"
    )
    op.execute(f"GRANT UPDATE (canonical_name) ON entities TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS document_revisions_checksum ON document_revisions")
    op.execute("DROP FUNCTION IF EXISTS enforce_revision_checksum()")

    op.execute("""
        CREATE OR REPLACE FUNCTION reject_derived_mutation() RETURNS trigger
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

    for table, dropped, name, _columns, _target in COMPOSITE_KEYS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        # Restore the single-column key these replaced, so a downgrade leaves
        # 0004's schema rather than a weaker one.
        single = _SINGLE_COLUMN_KEYS.get(dropped)
        if single is not None:
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {dropped} {single}")

    for table, _ in BACKFILL:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_workspace_fk")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS workspace_id")

    op.execute(
        "ALTER TABLE document_revisions"
        " DROP CONSTRAINT IF EXISTS document_revisions_id_workspace_key,"
        " DROP CONSTRAINT IF EXISTS document_revisions_id_document_key"
    )
    for table in ("thoughts", "runs", "documents", "entities"):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_id_workspace_key")

    op.execute(
        "ALTER TABLE thoughts DROP CONSTRAINT IF EXISTS thoughts_workspace_source_message_key"
    )
    op.execute(
        "ALTER TABLE thoughts"
        " ADD CONSTRAINT thoughts_source_source_message_id_key UNIQUE (source, source_message_id)"
    )

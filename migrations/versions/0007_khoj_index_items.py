"""Khoj index sync bookkeeping (docs/DESIGN.md 6.5, slice 18).

One row per document - only the current revision is ever indexed
(docs/DESIGN.md 8.3), so a document never has more than one live Khoj
filename. Records the *last successful* push only (filename, sha256, synced
time), matching docs/DESIGN.md 6.5's own description exactly. A sync failure
is never recorded here: it stays on the ``khoj.sync_requested`` outbox event's
own ``attempts``/``last_error`` (the same durable retry/observability surface
digest delivery already relies on for its own failures) - a document whose
very first sync attempt fails has no filename/revision/sha to record yet, so
this table would need nullable columns for a case the outbox already covers
better. Independent of ``runs`` entirely (docs/adr/0003's index-sync design:
outbox-only, no dedicated ``khoj_sync`` run rows - see the slice-18 plan for
why).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tc_app"


def upgrade() -> None:
    # Composite FKs against documents(id, workspace_id) and
    # document_revisions(id, workspace_id) - the same pattern migration 0005
    # established everywhere else a child row points at a workspace-scoped
    # parent - so the database itself rejects a row whose document_id or
    # revision_id belongs to a different workspace than its own workspace_id
    # column claims, not just application code that happens to pass a
    # consistent pair.
    op.execute("""
        CREATE TABLE khoj_index_items (
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          document_id uuid PRIMARY KEY,
          filename text NOT NULL,
          revision_id uuid NOT NULL,
          body_sha256 char(64) NOT NULL,
          synced_at timestamptz NOT NULL,
          FOREIGN KEY (document_id, workspace_id) REFERENCES documents (id, workspace_id),
          FOREIGN KEY (revision_id, workspace_id)
            REFERENCES document_revisions (id, workspace_id)
        )
    """)
    op.execute("CREATE UNIQUE INDEX khoj_index_items_filename_idx ON khoj_index_items (filename)")
    op.execute("CREATE INDEX khoj_index_items_workspace_idx ON khoj_index_items (workspace_id)")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON khoj_index_items TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS khoj_index_items")

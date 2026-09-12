"""Grant DELETE on document_embeddings for the tracked reembed lifecycle (docs/adr/0010, F15).

Migration 0008 deliberately withheld DELETE: "a document's embedding row is
superseded in place, never removed while the document itself still exists."
That invariant is still true for the sync-writer's own upsert path
(`embedding_writer.py`), which never deletes and is unaffected by this
migration. It did not anticipate a second, admin-triggered path: a tracked
`reembed` run (`runs.kind='reembed'`) that changes a workspace's target
embedding model.

That path needs DELETE for a structural reason, not convenience.
`EmbeddingModelConflictError` (round-3 finding F13-A, already accepted
architecture) refuses every write that would leave a workspace's
`document_embeddings` holding rows from two different models at once. A
`reembed` run enqueues `embedding.sync_requested` for every document under
the *new* model while the workspace's existing rows still carry the *old*
model - if those old rows are not first removed, the model-conflict guard
would reject every one of those new-model writes, permanently deadlocking
the very operation this migration exists to unblock.

So the accepted invariant remains "never removed while the document itself
still exists, *except* by the tracked reembed lifecycle admin operation" -
narrower in wording, not weaker in effect: nothing about the sync-writer's
own upsert-only contract changes, and DELETE reaches this table through
exactly one call site (`packages/infrastructure/src/tc_infrastructure/db/
reembed_run.py`'s `StartReembedRun`, workspace-scoped via the same
`documents` join `embedding_writer.py` already uses - `document_embeddings`
still has no `workspace_id` column of its own, per migration 0008).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-12

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tc_app"


def upgrade() -> None:
    op.execute(f"GRANT DELETE ON document_embeddings TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE DELETE ON document_embeddings FROM {APP_ROLE}")

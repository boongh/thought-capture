"""Self-hosted embedding storage: pgvector, ``document_embeddings`` (docs/adr/0010).

Slice 1 of ADR-0010's migration path. Adds the `vector` extension (now
first-party: `deploy/compose/postgres/Dockerfile` layers `pgvector` onto the
already-pinned Postgres base image, ADR-0010 §2) and one row-per-document
embedding table (`docs/DESIGN.md` 8.4), keyed by ``document_id`` and carrying
whichever revision was most recently embedded successfully.

The ``(revision_id, document_id)`` pairing is enforced by a **composite**
foreign key against ``document_revisions (id, document_id)`` - not two
independent single-column foreign keys - so the database itself rejects a
row that pairs the right document with the wrong document's revision
(ADR-0010 §3, `docs/DESIGN.md` 8.4). The unique target this composite key
needs, ``document_revisions_id_document_key``, already exists (migration
0005). There is deliberately no ``workspace_id`` column here: every read
joins ``document_embeddings -> documents`` for it instead of trusting a
second, independently-writable copy that could drift (same section).

No vector index in this migration. ``docs/DESIGN.md`` 8.4 and ADR-0010 §2
chose an exact scan for v1 over an HNSW index deliberately: HNSW's
approximate graph traversal runs *before* this design's required workspace/
date/entity/type/source filters are applied, which can silently under-return
results for a selective filter. An approximate index is revisited only once
a stated row-count or measured-latency trigger fires (8.4), never built
speculatively.

``runs.kind`` gains ``'embedding_sync'`` here, purely additively -
``'khoj_sync'`` stays valid, since Khoj sync keeps running alongside the new
embedding sync until a later slice proves parity and cuts over
(ADR-0010's "Migration and rollback": "a migration must never narrow a CHECK
constraint in the same step it widens one").

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tc_app"

# thenlper/gte-small (ADR-0010 §5's recommended v1 model) outputs 384-dim
# vectors. Changing the embedding model requires a `reembed` run recomputing
# every row (docs/DESIGN.md 8.5) - never comparing vectors of different
# dimensionality is enforced structurally by the column type itself.
EMBEDDING_DIMENSIONS = 384

_RUNS_KIND_VALUES_BEFORE = (
    "organize",
    "force_organize",
    "khoj_sync",
    "export",
    "restore_test",
    "reembed",
    "replay",
    "regenerate",
)
_RUNS_KIND_VALUES_AFTER = (*_RUNS_KIND_VALUES_BEFORE, "embedding_sync")


def _runs_kind_check(values: tuple[str, ...]) -> str:
    joined = ",".join(f"'{v}'" for v in values)
    return f"CHECK (kind IN ({joined}))"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_kind_check")
    op.execute(
        f"ALTER TABLE runs ADD CONSTRAINT runs_kind_check {_runs_kind_check(_RUNS_KIND_VALUES_AFTER)}"
    )

    op.execute(f"""
        CREATE TABLE document_embeddings (
          document_id uuid PRIMARY KEY REFERENCES documents(id),
          revision_id uuid NOT NULL,
          embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL,
          embedding_model_id text NOT NULL,
          -- DEFAULT now() only fires on INSERT. The sync-writer's upsert
          -- (below) must set this column explicitly in its own DO UPDATE
          -- SET clause - see tests/integration/test_document_embeddings.py
          -- ::test_upsert_by_document_id_replaces_the_prior_revision for the
          -- exact shape - or it silently keeps the original insert's
          -- timestamp on every later revision.
          updated_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (revision_id, document_id)
            REFERENCES document_revisions (id, document_id)
        )
    """)

    # No index beyond the primary key - `docs/DESIGN.md` 8.4's stored row
    # shape names none, and the semantic search query that would benefit
    # from one (filtering on `revision_id = documents.current_revision_id`)
    # is not built until a later slice; adding an index ahead of a real
    # query plan to justify it is exactly the speculative-index mistake this
    # ADR's "no HNSW until a stated trigger fires" reasoning warns against
    # applying case by case.

    # Sync-writer upsert only (`INSERT ... ON CONFLICT (document_id) DO
    # UPDATE ...`, docs/DESIGN.md 8.4's write-path guard) - no DELETE, since
    # a document's embedding row is superseded in place, never removed while
    # the document itself still exists.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON document_embeddings TO {APP_ROLE}")


def downgrade() -> None:
    # Preflight, before any DDL (review finding): restoring the narrower
    # runs_kind_check below rejects any EXISTING `runs` row with
    # kind='embedding_sync' - a real possibility once embedding sync starts
    # recording runs in a later slice, not merely a hypothetical. Without
    # this check, downgrade would first DROP document_embeddings (genuinely
    # destructive - the sync-writer's own upsert history, not recoverable
    # from the constraint alone) and only THEN fail on the ADD CONSTRAINT,
    # leaving the schema downgraded halfway with no way to recover the
    # dropped table's contents even by re-running upgrade(). Aborting here,
    # before the DROP TABLE ever runs, means an unsafe downgrade changes
    # nothing at all - matching this file's own upgrade() docstring
    # principle ("a migration must never narrow a CHECK constraint in the
    # same step it widens one") applied to the downgrade direction too.
    connection = op.get_bind()
    unsafe_run_count = connection.execute(
        sa.text("SELECT count(*) FROM runs WHERE kind = 'embedding_sync'")
    ).scalar_one()
    if unsafe_run_count:
        raise RuntimeError(
            f"Cannot downgrade migration 0008: {unsafe_run_count} row(s) in "
            "`runs` have kind='embedding_sync', which the constraint this "
            "downgrade restores would reject. Downgrading would either fail "
            "mid-migration after already dropping document_embeddings, or "
            "silently require deleting those audit rows - neither is safe "
            "to do automatically. Resolve manually (e.g. decide whether "
            "those runs may be deleted, or recreate them under a "
            "pre-existing `kind` value) before downgrading past 0008."
        )

    op.execute(f"REVOKE ALL ON document_embeddings FROM {APP_ROLE}")
    op.execute("DROP TABLE IF EXISTS document_embeddings")

    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_kind_check")
    op.execute(
        f"ALTER TABLE runs ADD CONSTRAINT runs_kind_check {_runs_kind_check(_RUNS_KIND_VALUES_BEFORE)}"
    )

    # The extension is left installed deliberately, matching this repo's
    # existing convention for `pg_trgm` (migration 0001, never dropped on
    # downgrade): dropping an extension is a separate, riskier operation than
    # reverting the schema that happens to use it, and CASCADE would be
    # required the moment any other object still depends on the `vector`
    # type.

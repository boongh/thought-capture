"""Exact-search indexes over generated documents (docs/DESIGN.md 9.1, 10).

Search runs over the *current* revision of each document, mirroring the Khoj
semantic index's own scope (docs/DESIGN.md 8.3: "Only the current revision is
present in the live Khoj index"), so a hybrid result later fusing both
channels compares like with like and a superseded fact is never surfaced by
one channel but not the other.

Adds an English full-text index and a trigram index on
``document_revisions.body_markdown`` (the same pairing ``thoughts`` already
has, migration 0001) for websearch-style terms and exact-phrase/fuzzy
matching respectively, a trigram index on ``documents.title`` so a title
match ranks even when the phrase never appears in the body, and an index on
``entity_mentions.revision_id`` - added only now because this is the first
query that needs to join *from* a revision to its mentions; the existing
``entity_mentions_thought_idx`` mirrors the same partial-index shape for the
thought-side join.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-02

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE document_revisions ADD COLUMN body_tsv tsvector GENERATED ALWAYS AS
          (to_tsvector('english', coalesce(body_markdown,''))) STORED
    """)
    op.execute("CREATE INDEX document_revisions_tsv_idx ON document_revisions USING gin (body_tsv)")
    op.execute(
        "CREATE INDEX document_revisions_trgm_idx"
        " ON document_revisions USING gin (body_markdown gin_trgm_ops)"
    )
    op.execute("CREATE INDEX documents_title_trgm_idx ON documents USING gin (title gin_trgm_ops)")
    op.execute(
        "CREATE INDEX entity_mentions_revision_idx ON entity_mentions (revision_id)"
        " WHERE revision_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS entity_mentions_revision_idx")
    op.execute("DROP INDEX IF EXISTS documents_title_trgm_idx")
    op.execute("DROP INDEX IF EXISTS document_revisions_trgm_idx")
    op.execute("DROP INDEX IF EXISTS document_revisions_tsv_idx")
    op.execute("ALTER TABLE document_revisions DROP COLUMN body_tsv")

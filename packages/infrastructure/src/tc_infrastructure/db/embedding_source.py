"""Read access to the current revision content an embedding is computed from.

Mirrors ``PostgresDocumentReader.get_export``'s workspace-scoping shape
(docs/DESIGN.md 8.4, ADR-0010 §3): a document must resolve *and* belong to
the given workspace, never on ``document_id`` alone, or a leaked/miswired
event could compute an embedding against another workspace's content.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.embedding_ports import RevisionForEmbedding
from tc_domain.errors import EmbeddingSourceNotFound
from tc_infrastructure.db.tables import document_revisions, documents


class PostgresEmbeddingSource:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_revision(
        self, *, workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> RevisionForEmbedding:
        """Raises ``EmbeddingSourceNotFound`` unless the document resolves and
        belongs to this workspace.
        """
        statement = (
            sa.select(
                documents.c.id,
                documents.c.title,
                document_revisions.c.id.label("revision_id"),
                document_revisions.c.revision_number,
                document_revisions.c.body_markdown,
            )
            .select_from(
                documents.join(
                    document_revisions, document_revisions.c.id == documents.c.current_revision_id
                )
            )
            .where(documents.c.id == document_id, documents.c.workspace_id == workspace_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).first()

        if row is None:
            raise EmbeddingSourceNotFound(
                f"no document {document_id} in workspace {workspace_id}"
            )

        return RevisionForEmbedding(
            document_id=row.id,
            revision_id=row.revision_id,
            revision_number=row.revision_number,
            title=row.title,
            body_markdown=row.body_markdown,
        )

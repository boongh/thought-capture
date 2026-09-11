"""Embedding force-sync's write side (docs/DESIGN.md 10,
``POST /v1/admin/embedding-sync``).

Selects only documents with a current revision (``current_revision_id IS NOT
NULL``), since ``EmbeddingSource.get_revision`` has nothing to embed for a
document that has never been written. Mirrors ``khoj_force_sync.py``, which
selects every document unconditionally because Khoj export has no such
guard; the extra condition here is the one deliberate deviation, not an
oversight.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_infrastructure.db.embedding_sync_enqueue import enqueue_embedding_sync
from tc_infrastructure.db.tables import documents


class PostgresEmbeddingForceSync:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue_all(self, workspace_id: uuid.UUID) -> int:
        async with self._session_factory() as session, session.begin():
            document_ids = (
                (
                    await session.execute(
                        sa.select(documents.c.id).where(
                            documents.c.workspace_id == workspace_id,
                            documents.c.current_revision_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for document_id in document_ids:
                await enqueue_embedding_sync(
                    session, workspace_id=workspace_id, document_id=document_id
                )
        return len(document_ids)

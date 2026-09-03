"""``POST /v1/admin/khoj-sync``'s write side (docs/DESIGN.md 10).

Selects every current document id directly rather than through
``PostgresDocumentReader.list_documents`` - that method caps at
``MAX_LIMIT`` for API pagination, which would silently truncate a force
resync of a larger corpus.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_infrastructure.db.khoj_sync_enqueue import enqueue_khoj_sync
from tc_infrastructure.db.tables import documents


class PostgresKhojForceSync:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue_all(self, workspace_id: uuid.UUID) -> int:
        async with self._session_factory() as session, session.begin():
            document_ids = (
                (
                    await session.execute(
                        sa.select(documents.c.id).where(documents.c.workspace_id == workspace_id)
                    )
                )
                .scalars()
                .all()
            )
            for document_id in document_ids:
                await enqueue_khoj_sync(session, workspace_id=workspace_id, document_id=document_id)
        return len(document_ids)

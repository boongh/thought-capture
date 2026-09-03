"""Write side of ``khoj_index_items`` (docs/DESIGN.md 6.5).

Separate from ``document_reader``'s read side for the same reason
``thought_repository`` is separate from ``thought_reader`` (docs/DESIGN.md 5.3).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_infrastructure.db.tables import khoj_index_items


class PostgresKhojIndexRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_synced(
        self,
        *,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        filename: str,
        revision_id: uuid.UUID,
        body_sha256: str,
    ) -> None:
        statement = (
            pg_insert(khoj_index_items)
            .values(
                workspace_id=workspace_id,
                document_id=document_id,
                filename=filename,
                revision_id=revision_id,
                body_sha256=body_sha256,
                synced_at=sa.func.now(),
            )
            .on_conflict_do_update(
                index_elements=["document_id"],
                set_={
                    "filename": filename,
                    "revision_id": revision_id,
                    "body_sha256": body_sha256,
                    "synced_at": sa.func.now(),
                },
            )
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(statement)

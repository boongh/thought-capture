"""Reads one digest document revision for delivery."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.digest import DigestContent
from tc_domain.errors import DigestNotFound
from tc_infrastructure.db.tables import document_revisions, documents, runs


class PostgresDigestReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, document_id: uuid.UUID, revision_id: uuid.UUID) -> DigestContent:
        statement = (
            sa.select(
                documents.c.title,
                document_revisions.c.body_markdown,
                runs.c.window_start,
                runs.c.window_end,
            )
            .select_from(document_revisions)
            .join(documents, documents.c.id == document_revisions.c.document_id)
            .join(runs, runs.c.id == document_revisions.c.run_id)
            .where(
                document_revisions.c.id == revision_id,
                document_revisions.c.document_id == document_id,
            )
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).first()

        if row is None:
            raise DigestNotFound(
                f"no digest revision for document={document_id} revision={revision_id}"
            )

        return DigestContent(
            title=row.title,
            body_markdown=row.body_markdown,
            window_start=row.window_start,
            window_end=row.window_end,
        )

"""Reads one digest document revision for delivery.

``get`` is a security boundary, not just a lookup: a ``digest.ready`` event
is trusted to point at a real digest, but a malformed or miswired one (a
bug elsewhere, or a corrupted payload) must never be able to make an
unrelated document reach Discord. Every dimension the event carries -
workspace, run, and the ``daily_digest`` kind itself - is checked, not just
the ``(document_id, revision_id)`` pair a bare join would accept.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.digest import DigestContent
from tc_domain.errors import DigestNotFound
from tc_infrastructure.db.organize_writer import DIGEST_KIND
from tc_infrastructure.db.tables import document_revisions, documents, runs


class PostgresDigestReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> DigestContent:
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
                # The revision's own run must be the exact run the event
                # claims produced it - not merely "some run in this
                # workspace" - so a mismatched or hijacked revision
                # reference cannot slip through.
                document_revisions.c.run_id == run_id,
                document_revisions.c.workspace_id == workspace_id,
                documents.c.workspace_id == workspace_id,
                documents.c.kind == DIGEST_KIND,
                runs.c.workspace_id == workspace_id,
            )
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).first()

        if row is None:
            raise DigestNotFound(
                f"no daily_digest revision for workspace={workspace_id} run={run_id} "
                f"document={document_id} revision={revision_id}"
            )

        return DigestContent(
            title=row.title,
            body_markdown=row.body_markdown,
            window_start=row.window_start,
            window_end=row.window_end,
        )

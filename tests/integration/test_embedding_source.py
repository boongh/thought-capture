"""``PostgresEmbeddingSource.get_revision`` - workspace-scoped current-revision
lookup for the embedding sync consumer (docs/DESIGN.md 8.4, ADR-0010 §3).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.errors import EmbeddingSourceNotFound
from tc_infrastructure.db.embedding_source import PostgresEmbeddingSource
from tc_infrastructure.db.tables import document_revisions, documents, runs

pytestmark = pytest.mark.integration


async def _document_with_revision(
    session: AsyncSession, workspace_id: uuid.UUID, *, body: str = "synthetic body"
) -> tuple[uuid.UUID, uuid.UUID]:
    document_id = uuid.uuid4()
    run_id = uuid.uuid4()
    revision_id = uuid.uuid4()

    await session.execute(
        sa.insert(runs).values(
            id=run_id, workspace_id=workspace_id, kind="organize", status="succeeded"
        )
    )
    await session.execute(
        sa.insert(documents).values(
            id=document_id,
            workspace_id=workspace_id,
            kind="topic",
            stable_key=f"k-{uuid.uuid4()}",
            title="Synthetic title",
        )
    )
    await session.execute(
        sa.insert(document_revisions).values(
            id=revision_id,
            document_id=document_id,
            workspace_id=workspace_id,
            run_id=run_id,
            revision_number=1,
            body_markdown=body,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            change_summary="synthetic",
            change_kind="create",
        )
    )
    await session.execute(
        sa.update(documents)
        .where(documents.c.id == document_id)
        .values(current_revision_id=revision_id)
    )
    return document_id, revision_id


async def test_get_revision_returns_the_current_revision_content(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        document_id, revision_id = await _document_with_revision(
            session, workspace_id, body="hello embedding"
        )

    source = PostgresEmbeddingSource(admin_session_factory)
    result = await source.get_revision(workspace_id=workspace_id, document_id=document_id)

    assert result.document_id == document_id
    assert result.revision_id == revision_id
    assert result.revision_number == 1
    assert result.title == "Synthetic title"
    assert result.body_markdown == "hello embedding"


async def test_get_revision_raises_for_a_document_in_a_different_workspace(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Never resolve on ``document_id`` alone - a document from another
    workspace must not leak its content into this workspace's embedding."""
    owning_workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        document_id, _revision_id = await _document_with_revision(session, owning_workspace_id)

    other_workspace_id = uuid.uuid4()
    source = PostgresEmbeddingSource(admin_session_factory)
    with pytest.raises(EmbeddingSourceNotFound):
        await source.get_revision(workspace_id=other_workspace_id, document_id=document_id)


async def test_get_revision_raises_for_an_unknown_document_id(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    source = PostgresEmbeddingSource(admin_session_factory)
    with pytest.raises(EmbeddingSourceNotFound):
        await source.get_revision(workspace_id=workspace_id, document_id=uuid.uuid4())

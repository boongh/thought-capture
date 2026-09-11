"""``PostgresEmbeddingWriter.upsert`` - the sync-writer's write-path guard
(docs/DESIGN.md 8.4, ADR-0010 §3).

``tests/integration/test_document_embeddings.py`` already covers the raw
``document_embeddings`` table shape and its composite foreign key at the SQL
level (``test_rejects_a_revision_belonging_to_a_different_document``); this
file is deliberately a separate, additional file rather than an edit to that
one - it exercises the FK guarantee (and the newer/older revision guard)
through ``PostgresEmbeddingWriter`` itself, the actual code path a real sync
consumer calls, without touching a file this change does not own.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.embedding_ports import EmbeddingVector
from tc_infrastructure.db.embedding_writer import PostgresEmbeddingWriter
from tc_infrastructure.db.tables import (
    EMBEDDING_DIMENSIONS,
    document_embeddings,
    document_revisions,
    documents,
    runs,
)

pytestmark = pytest.mark.integration

MODEL_ID = "thenlper/gte-small"
OTHER_MODEL_ID = "BAAI/bge-small-en-v1.5"


def _vector(seed: float, *, model_id: str = MODEL_ID) -> EmbeddingVector:
    return EmbeddingVector(
        values=tuple([seed] * EMBEDDING_DIMENSIONS),
        model_id=model_id,
        dimensions=EMBEDDING_DIMENSIONS,
    )


async def _document_with_revision(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    body: str = "synthetic body",
    revision_number: int = 1,
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
            title="Synthetic",
        )
    )
    await session.execute(
        sa.insert(document_revisions).values(
            id=revision_id,
            document_id=document_id,
            workspace_id=workspace_id,
            run_id=run_id,
            revision_number=revision_number,
            body_markdown=body,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            change_summary="synthetic",
            change_kind="create",
        )
    )
    return document_id, revision_id


async def _add_revision(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    parent_revision_id: uuid.UUID,
    run_id: uuid.UUID,
    revision_number: int,
    body: str,
) -> uuid.UUID:
    revision_id = uuid.uuid4()
    await session.execute(
        sa.insert(document_revisions).values(
            id=revision_id,
            document_id=document_id,
            workspace_id=workspace_id,
            parent_revision_id=parent_revision_id,
            run_id=run_id,
            revision_number=revision_number,
            body_markdown=body,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            change_summary="synthetic",
            change_kind="organize",
        )
    )
    return revision_id


async def test_a_fresh_insert_writes_the_row_and_reports_true(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        document_id, revision_id = await _document_with_revision(session, workspace_id)

    writer = PostgresEmbeddingWriter(admin_session_factory)
    written = await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=revision_id,
        revision_number=1,
        vector=_vector(0.5),
    )

    assert written is True
    async with admin_session_factory() as session:
        stored = (
            await session.execute(
                sa.select(document_embeddings.c.revision_id).where(
                    document_embeddings.c.document_id == document_id
                )
            )
        ).scalar_one()
    assert stored == revision_id


async def test_a_newer_revision_replaces_the_stored_row_and_reports_true(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    writer = PostgresEmbeddingWriter(admin_session_factory)

    async with admin_session_factory() as session, session.begin():
        document_id, first_revision_id = await _document_with_revision(
            session, workspace_id, body="revision one", revision_number=1
        )
        run_id = await session.scalar(
            sa.select(document_revisions.c.run_id).where(
                document_revisions.c.id == first_revision_id
            )
        )

    await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=first_revision_id,
        revision_number=1,
        vector=_vector(0.1),
    )

    async with admin_session_factory() as session, session.begin():
        second_revision_id = await _add_revision(
            session,
            workspace_id=workspace_id,
            document_id=document_id,
            parent_revision_id=first_revision_id,
            run_id=run_id,
            revision_number=2,
            body="revision two",
        )

    written = await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=second_revision_id,
        revision_number=2,
        vector=_vector(0.9),
    )

    assert written is True
    async with admin_session_factory() as session:
        row = (
            await session.execute(
                sa.select(document_embeddings.c.revision_id, document_embeddings.c.embedding).where(
                    document_embeddings.c.document_id == document_id
                )
            )
        ).one()
    assert row.revision_id == second_revision_id
    assert row.embedding == list(_vector(0.9).values)


async def test_an_older_revision_does_not_replace_the_stored_row(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The out-of-order-redelivery guard: an at-least-once, out-of-order
    outbox redelivery must never overwrite a newer embedding with an older
    one."""
    workspace_id, _ = fresh_identity
    writer = PostgresEmbeddingWriter(admin_session_factory)

    async with admin_session_factory() as session, session.begin():
        document_id, first_revision_id = await _document_with_revision(
            session, workspace_id, body="revision one", revision_number=1
        )
        run_id = await session.scalar(
            sa.select(document_revisions.c.run_id).where(
                document_revisions.c.id == first_revision_id
            )
        )
        second_revision_id = await _add_revision(
            session,
            workspace_id=workspace_id,
            document_id=document_id,
            parent_revision_id=first_revision_id,
            run_id=run_id,
            revision_number=2,
            body="revision two",
        )

    # The newer revision lands first (as if it were delivered before an
    # earlier-queued, out-of-order redelivery of revision one's event).
    await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=second_revision_id,
        revision_number=2,
        vector=_vector(0.9),
    )

    written = await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=first_revision_id,
        revision_number=1,
        vector=_vector(0.1),
    )

    assert written is False
    async with admin_session_factory() as session:
        row = (
            await session.execute(
                sa.select(document_embeddings.c.revision_id, document_embeddings.c.embedding).where(
                    document_embeddings.c.document_id == document_id
                )
            )
        ).one()
    assert row.revision_id == second_revision_id, "the newer row must survive the stale redelivery"
    assert row.embedding == list(_vector(0.9).values)


async def test_a_cross_document_revision_id_is_rejected_by_the_composite_fk(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The exact guarantee ADR-0010 §3 names, exercised through the real
    write path: pairing the right document with the wrong document's
    revision must be unrepresentable, not just unusual."""
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        document_id, _own_revision_id = await _document_with_revision(
            session, workspace_id, body="doc a"
        )
        _other_document_id, foreign_revision_id = await _document_with_revision(
            session, workspace_id, body="doc b"
        )

    writer = PostgresEmbeddingWriter(admin_session_factory)
    with pytest.raises(sa.exc.IntegrityError):
        await writer.upsert(
            workspace_id=workspace_id,
            document_id=document_id,
            # Belongs to the *other* document created above.
            revision_id=foreign_revision_id,
            revision_number=1,
            vector=_vector(0.1),
        )


async def test_updated_at_only_advances_on_a_real_write(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    writer = PostgresEmbeddingWriter(admin_session_factory)

    async with admin_session_factory() as session, session.begin():
        document_id, first_revision_id = await _document_with_revision(
            session, workspace_id, body="revision one", revision_number=1
        )
        run_id = await session.scalar(
            sa.select(document_revisions.c.run_id).where(
                document_revisions.c.id == first_revision_id
            )
        )
        second_revision_id = await _add_revision(
            session,
            workspace_id=workspace_id,
            document_id=document_id,
            parent_revision_id=first_revision_id,
            run_id=run_id,
            revision_number=2,
            body="revision two",
        )

    await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=first_revision_id,
        revision_number=1,
        vector=_vector(0.1),
    )
    async with admin_session_factory() as session:
        first_updated_at = await session.scalar(
            sa.select(document_embeddings.c.updated_at).where(
                document_embeddings.c.document_id == document_id
            )
        )

    # A rejected, out-of-order write (older-than-stored) must not touch
    # updated_at at all - not even to the same timestamp again.
    rejected = await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=first_revision_id,
        revision_number=1,
        vector=_vector(0.2),
    )
    assert rejected is False
    async with admin_session_factory() as session:
        unchanged_updated_at = await session.scalar(
            sa.select(document_embeddings.c.updated_at).where(
                document_embeddings.c.document_id == document_id
            )
        )
    assert unchanged_updated_at == first_updated_at

    accepted = await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=second_revision_id,
        revision_number=2,
        vector=_vector(0.3),
    )
    assert accepted is True
    async with admin_session_factory() as session:
        advanced_updated_at = await session.scalar(
            sa.select(document_embeddings.c.updated_at).where(
                document_embeddings.c.document_id == document_id
            )
        )
    assert advanced_updated_at > first_updated_at


async def test_same_revision_different_model_replaces_the_stored_row_and_reports_true(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A forced re-embed sweep (docs/DESIGN.md 8.5) after an embedding-model
    change writes a new vector for a document whose revision has not moved -
    the model-gated branch of the write guard (F11)."""
    workspace_id, _ = fresh_identity
    writer = PostgresEmbeddingWriter(admin_session_factory)

    async with admin_session_factory() as session, session.begin():
        document_id, revision_id = await _document_with_revision(
            session, workspace_id, body="revision one", revision_number=1
        )

    await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=revision_id,
        revision_number=1,
        vector=_vector(0.1, model_id=MODEL_ID),
    )
    async with admin_session_factory() as session:
        first_updated_at = await session.scalar(
            sa.select(document_embeddings.c.updated_at).where(
                document_embeddings.c.document_id == document_id
            )
        )

    written = await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=revision_id,
        revision_number=1,
        vector=_vector(0.9, model_id=OTHER_MODEL_ID),
    )

    assert written is True
    async with admin_session_factory() as session:
        row = (
            await session.execute(
                sa.select(
                    document_embeddings.c.revision_id,
                    document_embeddings.c.embedding,
                    document_embeddings.c.embedding_model_id,
                    document_embeddings.c.updated_at,
                ).where(document_embeddings.c.document_id == document_id)
            )
        ).one()
    assert row.revision_id == revision_id
    assert row.embedding == list(_vector(0.9, model_id=OTHER_MODEL_ID).values)
    assert row.embedding_model_id == OTHER_MODEL_ID
    assert row.updated_at > first_updated_at


async def test_same_revision_same_model_does_not_replace_the_stored_row(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The existing invariant preserved alongside F11: a redundant
    redelivery of a revision already embedded by the current model is a
    no-op, not a rewrite of an identical vector."""
    workspace_id, _ = fresh_identity
    writer = PostgresEmbeddingWriter(admin_session_factory)

    async with admin_session_factory() as session, session.begin():
        document_id, revision_id = await _document_with_revision(
            session, workspace_id, body="revision one", revision_number=1
        )

    await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=revision_id,
        revision_number=1,
        vector=_vector(0.1, model_id=MODEL_ID),
    )
    async with admin_session_factory() as session:
        first_updated_at = await session.scalar(
            sa.select(document_embeddings.c.updated_at).where(
                document_embeddings.c.document_id == document_id
            )
        )

    written = await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=revision_id,
        revision_number=1,
        vector=_vector(0.9, model_id=MODEL_ID),
    )

    assert written is False
    async with admin_session_factory() as session:
        row = (
            await session.execute(
                sa.select(
                    document_embeddings.c.embedding,
                    document_embeddings.c.updated_at,
                ).where(document_embeddings.c.document_id == document_id)
            )
        ).one()
    assert row.embedding == list(_vector(0.1, model_id=MODEL_ID).values)
    assert row.updated_at == first_updated_at


async def test_older_revision_different_model_does_not_replace_the_stored_row(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The model gate must not become a back door for stale, out-of-order
    redeliveries: an older revision is still rejected even when the model
    also changed."""
    workspace_id, _ = fresh_identity
    writer = PostgresEmbeddingWriter(admin_session_factory)

    async with admin_session_factory() as session, session.begin():
        document_id, first_revision_id = await _document_with_revision(
            session, workspace_id, body="revision one", revision_number=1
        )
        run_id = await session.scalar(
            sa.select(document_revisions.c.run_id).where(
                document_revisions.c.id == first_revision_id
            )
        )
        second_revision_id = await _add_revision(
            session,
            workspace_id=workspace_id,
            document_id=document_id,
            parent_revision_id=first_revision_id,
            run_id=run_id,
            revision_number=2,
            body="revision two",
        )

    await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=second_revision_id,
        revision_number=2,
        vector=_vector(0.9, model_id=MODEL_ID),
    )

    written = await writer.upsert(
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=first_revision_id,
        revision_number=1,
        vector=_vector(0.1, model_id=OTHER_MODEL_ID),
    )

    assert written is False
    async with admin_session_factory() as session:
        row = (
            await session.execute(
                sa.select(
                    document_embeddings.c.revision_id,
                    document_embeddings.c.embedding,
                    document_embeddings.c.embedding_model_id,
                ).where(document_embeddings.c.document_id == document_id)
            )
        ).one()
    assert row.revision_id == second_revision_id
    assert row.embedding == list(_vector(0.9, model_id=MODEL_ID).values)
    assert row.embedding_model_id == MODEL_ID

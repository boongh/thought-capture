"""``document_embeddings`` storage and its composite foreign key (docs/adr/0010).

The composite foreign key against ``document_revisions (id, document_id)``
is what makes a document/revision mismatch unrepresentable, not merely
application-code discipline (ADR-0010 §3, ``docs/DESIGN.md`` 8.4): two
independent single-column foreign keys could each individually reference
something real while still jointly describing a lie - a faulty sync writer
pairing the right document with the wrong document's revision. This is the
integration test the ADR's own Verification section names explicitly.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_infrastructure.db.tables import (
    EMBEDDING_DIMENSIONS,
    document_embeddings,
    document_revisions,
    documents,
    runs,
)

pytestmark = pytest.mark.integration

MODEL_ID = "thenlper/gte-small"


def _vector(seed: float) -> list[float]:
    return [seed] * EMBEDDING_DIMENSIONS


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
            title="Synthetic",
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
    return document_id, revision_id


async def test_a_valid_row_round_trips_through_the_vector_column(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity

    async with admin_session_factory() as session, session.begin():
        document_id, revision_id = await _document_with_revision(session, workspace_id)
        await session.execute(
            sa.insert(document_embeddings).values(
                document_id=document_id,
                revision_id=revision_id,
                embedding=_vector(0.5),
                embedding_model_id=MODEL_ID,
            )
        )

    async with admin_session_factory() as session:
        stored = await session.scalar(
            sa.select(document_embeddings.c.embedding).where(
                document_embeddings.c.document_id == document_id
            )
        )

    assert stored == _vector(0.5)


async def test_rejects_a_revision_belonging_to_a_different_document(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The exact guarantee ADR-0010 §3 names: pairing the right document with
    the wrong document's revision must be unrepresentable, not just unusual."""
    workspace_id, _ = fresh_identity

    async with admin_session_factory() as session, session.begin():
        document_id, _own_revision_id = await _document_with_revision(
            session, workspace_id, body="doc a"
        )
        _other_document_id, foreign_revision_id = await _document_with_revision(
            session, workspace_id, body="doc b"
        )

    async with admin_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.IntegrityError):
            await session.execute(
                sa.insert(document_embeddings).values(
                    document_id=document_id,
                    # Belongs to the *other* document created above.
                    revision_id=foreign_revision_id,
                    embedding=_vector(0.1),
                    embedding_model_id=MODEL_ID,
                )
            )


async def test_upsert_by_document_id_replaces_the_prior_revision(
    admin_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The sync-writer's own write shape (`docs/DESIGN.md` 8.4's write-path
    guard): one row per document, replaced in place as new revisions land."""
    workspace_id, _ = fresh_identity

    async with admin_session_factory() as session, session.begin():
        document_id, first_revision_id = await _document_with_revision(
            session, workspace_id, body="revision one"
        )
        await session.execute(
            sa.insert(document_embeddings).values(
                document_id=document_id,
                revision_id=first_revision_id,
                embedding=_vector(0.1),
                embedding_model_id=MODEL_ID,
            )
        )

    async with admin_session_factory() as session:
        first_updated_at = await session.scalar(
            sa.select(document_embeddings.c.updated_at).where(
                document_embeddings.c.document_id == document_id
            )
        )

    # A later revision of the *same* document (a fresh row, standing in for
    # what an `organize` run would create - document_revisions is immutable).
    async with admin_session_factory() as session, session.begin():
        second_revision_id = uuid.uuid4()
        run_id = await session.scalar(
            sa.select(document_revisions.c.run_id).where(
                document_revisions.c.id == first_revision_id
            )
        )
        body = "revision two"
        await session.execute(
            sa.insert(document_revisions).values(
                id=second_revision_id,
                document_id=document_id,
                workspace_id=workspace_id,
                parent_revision_id=first_revision_id,
                run_id=run_id,
                revision_number=2,
                body_markdown=body,
                body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                change_summary="synthetic",
                change_kind="organize",
            )
        )
        upsert = pg.insert(document_embeddings).values(
            document_id=document_id,
            revision_id=second_revision_id,
            embedding=_vector(0.9),
            embedding_model_id=MODEL_ID,
        )
        await session.execute(
            upsert.on_conflict_do_update(
                index_elements=[document_embeddings.c.document_id],
                set_={
                    "revision_id": upsert.excluded.revision_id,
                    "embedding": upsert.excluded.embedding,
                    "embedding_model_id": upsert.excluded.embedding_model_id,
                    # `updated_at`'s DEFAULT `now()` only fires on INSERT; an
                    # upsert's `DO UPDATE` must set it explicitly or it keeps
                    # the original insert's timestamp forever, silently going
                    # stale on every later revision. This is the write shape
                    # a real `EmbeddingSyncLoop` must copy.
                    "updated_at": sa.func.now(),
                },
            )
        )

    async with admin_session_factory() as session:
        row = (
            await session.execute(
                sa.select(
                    document_embeddings.c.revision_id,
                    document_embeddings.c.embedding,
                    document_embeddings.c.updated_at,
                ).where(document_embeddings.c.document_id == document_id)
            )
        ).one()

    assert row.revision_id == second_revision_id
    assert row.embedding == _vector(0.9)
    assert row.updated_at > first_updated_at

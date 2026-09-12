"""``ReembedRunStore`` against Postgres (F15-A, docs/plans/
embedding-sync-review-round-4.md; docs/DESIGN.md 8.5, 9.2).

Workspace scope for touching ``document_embeddings`` comes from the
``documents`` join, exactly as ``PostgresEmbeddingWriter``'s model-uniformity
precondition already does - ``document_embeddings`` has no ``workspace_id``
column of its own (ADR-0010 §3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.embedding_ports import ReembedRun
from tc_infrastructure.db.embedding_sync_enqueue import (
    EMBEDDING_SYNC_REQUESTED_EVENT,
    enqueue_embedding_sync,
)
from tc_infrastructure.db.tables import document_embeddings, documents, outbox_events, runs

REEMBED_KIND = "reembed"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


def _row_to_run(row: sa.Row[Any], *, enqueued: int) -> ReembedRun:
    return ReembedRun(
        run_id=row.id,
        workspace_id=row.workspace_id,
        embedding_model_id=row.embedding_model_id,
        status=row.status,
        enqueued=enqueued,
        started_at=row.started_at,
    )


class PostgresReembedRunStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def start(self, workspace_id: uuid.UUID, *, embedding_model_id: str) -> ReembedRun:
        """See ``ReembedRunStore.start``.

        Not locked against a second, truly concurrent caller racing this
        same check (READ COMMITTED gives no protection here) - the same
        accepted gap ``PostgresEmbeddingWriter.upsert``'s workspace-
        uniformity precondition documents, for the same reason: it requires
        two admin calls landing within the same commit window, which this
        single-operator surface does not make a realistic threat today.
        """
        async with self._session_factory() as session, session.begin():
            existing = await self._select_running(session, workspace_id)
            if existing is not None:
                # Idempotent (owner decision, round-4 F15): nothing new is
                # written or enqueued by this call, so `enqueued=0` reflects
                # what *this* call actually did, not the original sweep's
                # total.
                return _row_to_run(existing, enqueued=0)

            run_id = uuid.uuid4()
            # Postgres's own `now()`, not a Python-side timestamp: the dead
            # letters `has_dead_lettered_sync_events` scopes against below
            # get their `created_at` from the same database's `now()` (via
            # `enqueue_embedding_sync`'s column default) moments later in
            # this same transaction. Using the app server's clock for
            # `started_at` instead would risk clock skew making this run's
            # own genuinely-relevant dead letters look like they predate it
            # - under-catching a real failure, not just over-catching a
            # stale one.
            started_at = await session.scalar(
                sa.insert(runs)
                .values(
                    id=run_id,
                    workspace_id=workspace_id,
                    kind=REEMBED_KIND,
                    status=RUNNING,
                    embedding_model_id=embedding_model_id,
                    started_at=sa.func.now(),
                )
                .returning(runs.c.started_at)
            )
            assert started_at is not None

            # Wipe this workspace's document_embeddings before re-enqueuing -
            # the audited replacement for docs/OPERATING.md's former
            # hand-run `DELETE FROM document_embeddings` (docs/incidents/0001).
            # Scoped via the `documents` join, never a bare workspace_id
            # column - document_embeddings has none.
            await session.execute(
                sa.delete(document_embeddings).where(
                    document_embeddings.c.document_id.in_(
                        sa.select(documents.c.id).where(documents.c.workspace_id == workspace_id)
                    )
                )
            )

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

        return ReembedRun(
            run_id=run_id,
            workspace_id=workspace_id,
            embedding_model_id=embedding_model_id,
            status=RUNNING,
            enqueued=len(document_ids),
            started_at=started_at,
        )

    async def find_running(self, workspace_id: uuid.UUID) -> ReembedRun | None:
        async with self._session_factory() as session:
            row = await self._select_running(session, workspace_id)
        return _row_to_run(row, enqueued=0) if row is not None else None

    async def _select_running(
        self, session: AsyncSession, workspace_id: uuid.UUID
    ) -> sa.Row[Any] | None:
        return (
            await session.execute(
                sa.select(runs).where(
                    runs.c.workspace_id == workspace_id,
                    runs.c.kind == REEMBED_KIND,
                    runs.c.status == RUNNING,
                )
            )
        ).first()

    async def list_running(self) -> tuple[ReembedRun, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(runs).where(runs.c.kind == REEMBED_KIND, runs.c.status == RUNNING)
                )
            ).all()
        return tuple(_row_to_run(row, enqueued=0) for row in rows)

    async def count_current_documents(self, workspace_id: uuid.UUID) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(
                sa.select(sa.func.count(documents.c.id)).where(
                    documents.c.workspace_id == workspace_id,
                    documents.c.current_revision_id.is_not(None),
                )
            )
        return int(count or 0)

    async def count_embedded_under_model(
        self, workspace_id: uuid.UUID, embedding_model_id: str
    ) -> int:
        # Workspace scope via the `documents` join - document_embeddings has
        # no workspace_id column of its own (ADR-0010 §3), mirroring
        # PostgresEmbeddingWriter's model-uniformity precondition. The join
        # also pins revision_id == documents.current_revision_id so a stale
        # embedding of a superseded revision never counts as "done" (F18):
        # otherwise a reembed run could report success while the document's
        # *current* revision has no embedding under the new model at all.
        async with self._session_factory() as session:
            count = await session.scalar(
                sa.select(sa.func.count(document_embeddings.c.document_id))
                .select_from(
                    document_embeddings.join(
                        documents,
                        sa.and_(
                            documents.c.id == document_embeddings.c.document_id,
                            document_embeddings.c.revision_id == documents.c.current_revision_id,
                        ),
                    )
                )
                .where(
                    documents.c.workspace_id == workspace_id,
                    document_embeddings.c.embedding_model_id == embedding_model_id,
                )
            )
        return int(count or 0)

    async def has_dead_lettered_sync_events(
        self, workspace_id: uuid.UUID, *, since: datetime
    ) -> bool:
        async with self._session_factory() as session:
            found = await session.scalar(
                sa.select(sa.literal(True))
                .select_from(outbox_events)
                .where(
                    outbox_events.c.workspace_id == workspace_id,
                    outbox_events.c.event_type == EMBEDDING_SYNC_REQUESTED_EVENT,
                    outbox_events.c.created_at >= since,
                    outbox_events.c.delivered_at.is_(None),
                    outbox_events.c.attempts >= outbox_events.c.max_attempts,
                )
                .limit(1)
            )
        return bool(found)

    async def mark_succeeded(self, run_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.update(runs)
                .where(runs.c.id == run_id)
                .values(status=SUCCEEDED, finished_at=sa.func.now())
            )

    async def mark_failed(self, run_id: uuid.UUID, *, error_code: str) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.update(runs)
                .where(runs.c.id == run_id)
                .values(status=FAILED, finished_at=sa.func.now(), error_code=error_code)
            )

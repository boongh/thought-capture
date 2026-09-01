"""Atomic write side of one organize run (docs/DESIGN.md 7.2 step 9).

Every document revision, its provenance, every entity mention it produced,
every ``run_context_selections`` row, and the digest outbox event commit in
one transaction - or none of them do. A validation failure is caught by the
caller *before* ``write`` is ever called (docs/DESIGN.md 7.2 step 8), so this
class's only failure mode is a database error, and a database error here must
not leave a half-written document set behind.
"""

from __future__ import annotations

import hashlib
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.organize import OrganizeWriteRequest, OrganizeWriteResult
from tc_infrastructure.db.entity_repository import PostgresEntityRepository
from tc_infrastructure.db.tables import (
    document_revisions,
    documents,
    outbox_events,
    revision_sources,
    run_context_selections,
)

DIGEST_READY_EVENT = "digest.ready"
DIGEST_KIND = "daily_digest"


class PostgresOrganizeWriter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        entities: PostgresEntityRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._entities = entities or PostgresEntityRepository()

    async def write(
        self, *, workspace_id: WorkspaceId, run_id: uuid.UUID, request: OrganizeWriteRequest
    ) -> OrganizeWriteResult:
        document_ids: dict[str, uuid.UUID] = {}
        revision_ids: dict[str, uuid.UUID] = {}
        digest_document_id: uuid.UUID | None = None
        digest_revision_id: uuid.UUID | None = None

        async with self._session_factory() as session, session.begin():
            for doc in request.documents:
                document_id, parent_revision_id = await self._upsert_document(
                    session,
                    workspace_id=workspace_id,
                    kind=doc.kind,
                    stable_key=doc.stable_key,
                    title=doc.title,
                )
                revision_id = await self._write_revision(
                    session,
                    workspace_id=workspace_id,
                    document_id=document_id,
                    run_id=run_id,
                    parent_revision_id=parent_revision_id,
                    body_markdown=doc.body_markdown,
                    change_summary=doc.change_summary,
                    change_kind="organize" if parent_revision_id is not None else "create",
                )
                await self._write_sources(
                    session,
                    workspace_id=workspace_id,
                    revision_id=revision_id,
                    thought_ids=doc.source_thought_ids,
                )
                await session.execute(
                    sa.update(documents)
                    .where(documents.c.id == document_id)
                    .values(current_revision_id=revision_id)
                )
                for mention in doc.mentioned_entities:
                    await self._entities.resolve_mention(
                        session,
                        workspace_id=workspace_id,
                        run_id=run_id,
                        entity_type=mention.entity_type,
                        canonical_name=mention.canonical_name,
                        surface_form=mention.surface_form,
                        confidence=mention.confidence,
                        revision_id=revision_id,
                    )

                document_ids[doc.stable_key] = document_id
                revision_ids[doc.stable_key] = revision_id
                if doc.kind == DIGEST_KIND:
                    digest_document_id = document_id
                    digest_revision_id = revision_id

            await self._write_context_selections(
                session, workspace_id=workspace_id, run_id=run_id, request=request
            )

            if digest_document_id is not None and digest_revision_id is not None:
                await self._enqueue_digest(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    document_id=digest_document_id,
                    revision_id=digest_revision_id,
                )

        return OrganizeWriteResult(
            document_ids=document_ids,
            revision_ids=revision_ids,
            digest_document_id=digest_document_id,
        )

    async def _upsert_document(
        self,
        session: AsyncSession,
        *,
        workspace_id: WorkspaceId,
        kind: str,
        stable_key: str,
        title: str,
    ) -> tuple[uuid.UUID, uuid.UUID | None]:
        """Return ``(document_id, current_revision_id)``, creating the document if new.

        ``ON CONFLICT ... DO UPDATE`` targets ``title`` - the one column
        migration 0005 grants the application role UPDATE on besides
        ``current_revision_id`` - so a repeat mention across runs can still
        pick up a corrected title without needing a second write path.
        """
        statement = (
            pg_insert(documents)
            .values(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                kind=kind,
                stable_key=stable_key,
                title=title,
            )
            .on_conflict_do_update(
                index_elements=["workspace_id", "kind", "stable_key"], set_={"title": title}
            )
            .returning(documents.c.id, documents.c.current_revision_id)
        )
        row = (await session.execute(statement)).one()
        return uuid.UUID(str(row.id)), row.current_revision_id

    async def _write_revision(
        self,
        session: AsyncSession,
        *,
        workspace_id: WorkspaceId,
        document_id: uuid.UUID,
        run_id: uuid.UUID,
        parent_revision_id: uuid.UUID | None,
        body_markdown: str,
        change_summary: str,
        change_kind: str,
    ) -> uuid.UUID:
        next_number = await session.scalar(
            sa.select(
                sa.func.coalesce(sa.func.max(document_revisions.c.revision_number), 0) + 1
            ).where(document_revisions.c.document_id == document_id)
        )
        revision_id = uuid.uuid4()
        await session.execute(
            sa.insert(document_revisions).values(
                id=revision_id,
                workspace_id=workspace_id,
                document_id=document_id,
                parent_revision_id=parent_revision_id,
                run_id=run_id,
                revision_number=next_number,
                body_markdown=body_markdown,
                body_sha256=hashlib.sha256(body_markdown.encode("utf-8")).hexdigest(),
                change_summary=change_summary,
                change_kind=change_kind,
            )
        )
        return revision_id

    async def _write_sources(
        self,
        session: AsyncSession,
        *,
        workspace_id: WorkspaceId,
        revision_id: uuid.UUID,
        thought_ids: tuple[int, ...],
    ) -> None:
        if not thought_ids:
            return
        await session.execute(
            sa.insert(revision_sources),
            [
                {
                    "workspace_id": workspace_id,
                    "revision_id": revision_id,
                    "thought_id": thought_id,
                    "support_type": "direct",
                }
                for thought_id in thought_ids
            ],
        )

    async def _write_context_selections(
        self,
        session: AsyncSession,
        *,
        workspace_id: WorkspaceId,
        run_id: uuid.UUID,
        request: OrganizeWriteRequest,
    ) -> None:
        if not request.context_selections:
            return
        keys = [c.stable_key for c in request.context_selections]
        rows = (
            await session.execute(
                sa.select(documents.c.id, documents.c.stable_key).where(
                    documents.c.workspace_id == workspace_id, documents.c.stable_key.in_(keys)
                )
            )
        ).all()
        document_id_by_key = {row.stable_key: row.id for row in rows}

        values = [
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "document_id": document_id_by_key[selection.stable_key],
                "signals": list(selection.signals),
                "inclusion": selection.inclusion,
                "referenced_in_output": selection.referenced_in_output,
            }
            for selection in request.context_selections
            if selection.stable_key in document_id_by_key
        ]
        if values:
            await session.execute(sa.insert(run_context_selections), values)

    async def _enqueue_digest(
        self,
        session: AsyncSession,
        *,
        workspace_id: WorkspaceId,
        run_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> None:
        """Same transaction as the digest revision itself (docs/DESIGN.md 6.5).

        A committed digest with no outbox event would never be delivered; an
        outbox event for a digest that failed to commit would deliver
        nothing. Writing both together is what keeps the two from diverging.
        """
        await session.execute(
            sa.insert(outbox_events).values(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                event_type=DIGEST_READY_EVENT,
                aggregate_id=str(run_id),
                payload={
                    "run_id": str(run_id),
                    "document_id": str(document_id),
                    "revision_id": str(revision_id),
                },
            )
        )

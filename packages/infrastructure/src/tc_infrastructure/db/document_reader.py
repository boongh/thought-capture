"""Read access to generated documents: entity documents and daily digests.

Separate from ``organize_writer``'s write side for the same reason
``thought_reader`` is separate from ``thought_repository`` (docs/DESIGN.md
5.3). Every query is workspace-scoped.

Unpaginated up to ``limit``, matching ``entity_reader``'s precedent: a
personal corpus's document count is small compared to its thought count
(docs/DESIGN.md 7.3.1 already treats the whole entity index as cheap enough to
send in full on every organize call).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.khoj_export import DocumentExportRecord
from tc_infrastructure.db.tables import (
    document_revisions,
    documents,
    entities,
    entity_mentions,
    revision_sources,
    runs,
)

DEFAULT_LIMIT = 100
MAX_LIMIT = 200


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    id: uuid.UUID
    kind: str
    stable_key: str
    title: str
    revision_number: int
    change_summary: str
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class DocumentDetail:
    id: uuid.UUID
    kind: str
    stable_key: str
    title: str
    revision_number: int
    body_markdown: str
    change_summary: str
    updated_at: dt.datetime
    source_thought_ids: tuple[int, ...]


_SUMMARY_COLUMNS = (
    documents.c.id,
    documents.c.kind,
    documents.c.stable_key,
    documents.c.title,
    document_revisions.c.revision_number,
    document_revisions.c.change_summary,
    document_revisions.c.created_at,
)


class PostgresDocumentReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_documents(
        self,
        workspace_id: WorkspaceId,
        *,
        kind: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[DocumentSummary]:
        """Most recently updated first, i.e. by the current revision's timestamp."""
        limit = max(1, min(limit, MAX_LIMIT))
        conditions = [documents.c.workspace_id == workspace_id]
        if kind is not None:
            conditions.append(documents.c.kind == kind)

        statement = (
            sa.select(*_SUMMARY_COLUMNS)
            .select_from(documents)
            .join(document_revisions, document_revisions.c.id == documents.c.current_revision_id)
            .where(*conditions)
            .order_by(document_revisions.c.created_at.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()

        return [_to_summary(row) for row in rows]

    async def get_document(
        self, workspace_id: WorkspaceId, document_id: uuid.UUID
    ) -> DocumentDetail | None:
        statement = (
            sa.select(
                documents.c.id,
                documents.c.kind,
                documents.c.stable_key,
                documents.c.title,
                document_revisions.c.id.label("revision_id"),
                document_revisions.c.revision_number,
                document_revisions.c.body_markdown,
                document_revisions.c.change_summary,
                document_revisions.c.created_at,
            )
            .select_from(documents)
            .join(document_revisions, document_revisions.c.id == documents.c.current_revision_id)
            .where(documents.c.id == document_id, documents.c.workspace_id == workspace_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).first()
            if row is None:
                return None
            source_rows = (
                await session.execute(
                    sa.select(revision_sources.c.thought_id)
                    .where(revision_sources.c.revision_id == row.revision_id)
                    .order_by(revision_sources.c.thought_id)
                )
            ).all()

        return DocumentDetail(
            id=row.id,
            kind=row.kind,
            stable_key=row.stable_key,
            title=row.title,
            revision_number=row.revision_number,
            body_markdown=row.body_markdown,
            change_summary=row.change_summary,
            updated_at=row.created_at,
            source_thought_ids=tuple(r.thought_id for r in source_rows),
        )

    async def list_all_current_for_export(
        self, workspace_id: WorkspaceId
    ) -> list[DocumentExportRecord]:
        """Every current revision in the workspace, unpaginated (docs/adr/0010's
        full-resync Khoj sync).

        Deliberately not capped at ``MAX_LIMIT`` like ``list_documents``: a
        resync that silently dropped documents past a page would leave Khoj's
        index quietly incomplete, and docs/DESIGN.md 7.5's degrade-explicit
        contract does not tolerate that kind of silent gap. A personal
        corpus's document count is small (docs/DESIGN.md 7.3.1 already treats
        the *whole* entity index as cheap enough to send on every organize
        call), so one unpaginated query is the right cost/complexity tradeoff
        for this slice - a follow-up incremental sync (docs/adr/0010) would
        replace this with a smaller, changed-only query instead of paginating
        this one.
        """
        statement = (
            sa.select(
                documents.c.id,
                documents.c.kind,
                documents.c.stable_key,
                documents.c.title,
                document_revisions.c.id.label("revision_id"),
                document_revisions.c.body_markdown,
                runs.c.window_start,
                runs.c.window_end,
            )
            .select_from(
                documents.join(
                    document_revisions, document_revisions.c.id == documents.c.current_revision_id
                ).join(runs, runs.c.id == document_revisions.c.run_id)
            )
            .where(documents.c.workspace_id == workspace_id)
            .order_by(documents.c.id)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            revision_ids = [row.revision_id for row in rows]
            thought_ids = await _export_thought_ids_for(session, workspace_id, revision_ids)
            entity_names = await _export_entity_names_for(session, workspace_id, revision_ids)

        return [
            DocumentExportRecord(
                id=row.id,
                kind=row.kind,
                stable_key=row.stable_key,
                title=row.title,
                revision_id=row.revision_id,
                body_markdown=row.body_markdown,
                window_start=row.window_start,
                window_end=row.window_end,
                entities=entity_names.get(row.revision_id, ()),
                source_thought_ids=thought_ids.get(row.revision_id, ()),
            )
            for row in rows
        ]


async def _export_thought_ids_for(
    session: AsyncSession, workspace_id: WorkspaceId, revision_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[ThoughtId, ...]]:
    """One query for every revision being exported, mirroring
    ``search_reader._thought_ids_for`` - each reader owns its own queries
    (docs/DESIGN.md 5.3's repository-per-read-shape precedent), so this is a
    deliberate small duplication rather than a cross-module import of another
    reader's private helper."""
    if not revision_ids:
        return {}
    rows = (
        await session.execute(
            sa.select(revision_sources.c.revision_id, revision_sources.c.thought_id)
            .where(
                revision_sources.c.revision_id.in_(revision_ids),
                revision_sources.c.workspace_id == workspace_id,
            )
            .order_by(revision_sources.c.revision_id, revision_sources.c.thought_id)
        )
    ).all()
    grouped: dict[uuid.UUID, list[ThoughtId]] = {}
    for row in rows:
        grouped.setdefault(row.revision_id, []).append(ThoughtId(row.thought_id))
    return {key: tuple(value) for key, value in grouped.items()}


async def _export_entity_names_for(
    session: AsyncSession, workspace_id: WorkspaceId, revision_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, ...]]:
    if not revision_ids:
        return {}
    rows = (
        await session.execute(
            sa.select(entity_mentions.c.revision_id, entities.c.canonical_name)
            .select_from(
                entity_mentions.join(entities, entities.c.id == entity_mentions.c.entity_id)
            )
            .where(
                entity_mentions.c.revision_id.in_(revision_ids),
                entity_mentions.c.workspace_id == workspace_id,
                entities.c.workspace_id == workspace_id,
            )
            .distinct()
            .order_by(entity_mentions.c.revision_id, entities.c.canonical_name)
        )
    ).all()
    grouped: dict[uuid.UUID, list[str]] = {}
    for row in rows:
        grouped.setdefault(row.revision_id, []).append(row.canonical_name)
    return {key: tuple(value) for key, value in grouped.items()}


def _to_summary(row: sa.Row[Any]) -> DocumentSummary:
    return DocumentSummary(
        id=row.id,
        kind=row.kind,
        stable_key=row.stable_key,
        title=row.title,
        revision_number=row.revision_number,
        change_summary=row.change_summary,
        updated_at=row.created_at,
    )

"""Resolves Khoj search-result filenames back to normalized ``SearchResult``s
(docs/DESIGN.md 7.5, 9.2).

Reuses ``search_reader.py``'s own thought/entity join helpers and structural
filter predicates rather than duplicating the query shape - both read the
same current-revision-only join (docs/DESIGN.md 8.3: only the current
revision is ever indexed, in Khoj or in this hydrator's own PostgreSQL read),
and both must enforce the same kind/source/date/time/entity/phrase/include/
exclude filters (docs/DESIGN.md 7.5 P1) so a semantic hit can never bypass a
filter its own channel has no way to evaluate. Only ``q`` is exempted from a
literal-match requirement here - that is what makes this search semantic
rather than exact.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import parse_khoj_filename
from tc_domain.search import SearchQuery, SearchResult
from tc_infrastructure.db.search_reader import (
    _entity_names_for,
    _filter_conditions,
    _thought_ids_for,
)
from tc_infrastructure.db.tables import document_revisions, documents, khoj_index_items

_SNIPPET_RADIUS = 160


class PostgresSemanticHydrator:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def hydrate(
        self, workspace_id: WorkspaceId, query: SearchQuery, filenames: tuple[str, ...]
    ) -> dict[str, SearchResult]:
        document_id_by_filename: dict[str, uuid.UUID] = {}
        for filename in filenames:
            parsed = parse_khoj_filename(filename)
            # A filename this hydrator did not itself understand, or one
            # belonging to a different workspace, is simply skipped - never
            # raised (SemanticHydrator's contract) - so one malformed or
            # cross-tenant Khoj result cannot fail an entire search request.
            if parsed is None or parsed.workspace_id != workspace_id:
                continue
            document_id_by_filename[filename] = parsed.document_id

        if not document_id_by_filename:
            return {}

        document_ids = list(set(document_id_by_filename.values()))
        conditions: list[sa.ColumnElement[bool]] = [
            documents.c.workspace_id == workspace_id,
            documents.c.id.in_(document_ids),
            # Khoj's own last-acknowledged sync must still name the current
            # revision - otherwise Khoj is still holding a superseded body
            # under this filename, and joining straight to
            # `documents.current_revision_id` (as below, for the columns this
            # query actually returns) would silently relabel that stale
            # content as if it were the current revision, carrying its own
            # semantic score (docs/DESIGN.md 7.5 P1). A document that has
            # never finished a sync at all has no `khoj_index_items` row and
            # is excluded by this inner join the same way.
            khoj_index_items.c.revision_id == documents.c.current_revision_id,
        ]
        conditions.extend(_filter_conditions(query, workspace_id, require_text_match=False))

        statement = (
            sa.select(
                documents.c.id.label("document_id"),
                documents.c.kind,
                documents.c.title,
                document_revisions.c.id.label("revision_id"),
                document_revisions.c.body_markdown,
                document_revisions.c.created_at,
            )
            .select_from(
                documents.join(
                    document_revisions, document_revisions.c.id == documents.c.current_revision_id
                ).join(khoj_index_items, khoj_index_items.c.document_id == documents.c.id)
            )
            .where(*conditions)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            revision_ids = [row.revision_id for row in rows]
            thought_ids = await _thought_ids_for(session, workspace_id, revision_ids)
            entity_names = await _entity_names_for(session, workspace_id, revision_ids)

        by_document_id = {
            row.document_id: SearchResult(
                result_id=str(row.revision_id),
                document_id=row.document_id,
                revision_id=row.revision_id,
                thought_ids=thought_ids.get(row.revision_id, ()),
                kind=row.kind,
                title=row.title,
                snippet=_snippet(row.body_markdown),
                updated_at=row.created_at,
                entities=entity_names.get(row.revision_id, ()),
                channels=("semantic",),
                rank=0.0,  # overwritten by the caller with Khoj's own score
            )
            for row in rows
        }
        return {
            filename: by_document_id[document_id]
            for filename, document_id in document_id_by_filename.items()
            if document_id in by_document_id
        }


def _snippet(body: str) -> str:
    """No phrase/term to highlight here (unlike ``search_reader._snippet``) -
    the match is semantic, not textual - so this is a plain lead truncation."""
    truncated = body[:_SNIPPET_RADIUS].strip()
    return truncated + ("…" if len(body) > _SNIPPET_RADIUS else "")

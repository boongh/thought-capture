"""PostgreSQL exact search over the current revision of each document.

docs/DESIGN.md 9.1. Historical revisions are excluded from every query here,
the same way Khoj's own semantic index only ever holds the current revision
(docs/DESIGN.md 8.3) - so neither search channel can cite a superseded fact,
and a later hybrid fusion (docs/DESIGN.md 7.5) compares like with like.

Ranking and title-word matching are scoped to what ``q`` and ``include``/
``exclude`` need: only ``document_revisions.body_markdown`` is indexed for
full-text search (migration 0006). A title-only match still surfaces through
``phrase`` (trigram/ILIKE, checked against both title and body), but a title
word alone does not currently contribute to ``q``'s rank - a documented
narrowing, not a silent gap.

TODO(docs/DESIGN.md 9.1): prefix matching and thresholded trigram fuzzy
spelling are documented as part of ``exact`` but are not implemented by this
slice. Tracked as deferred, not a silent gap - ``phrase``/``q``/``include``/
``exclude`` cover this slice's scope; a follow-up slice should add prefix and
opt-in fuzzy matching or update DESIGN.md if they are dropped.
"""

from __future__ import annotations

import base64
import binascii
import decimal
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.search import MAX_LIMIT, SearchPage, SearchQuery, SearchResult, text_query_string
from tc_infrastructure.db.tables import (
    document_revisions,
    documents,
    entities,
    entity_mentions,
    revision_sources,
    thoughts,
)

_SNIPPET_RADIUS = 160


class PostgresExactSearch:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def search(self, workspace_id: WorkspaceId, query: SearchQuery) -> SearchPage:
        limit = max(1, min(query.limit, MAX_LIMIT))
        rank_expr = _rank_expression(query)

        conditions = [documents.c.workspace_id == workspace_id]
        conditions.extend(_filter_conditions(query, workspace_id))

        if query.cursor:
            last_rank, last_id = _decode_cursor(query.cursor)
            conditions.append(
                sa.or_(
                    rank_expr < last_rank,
                    sa.and_(rank_expr == last_rank, document_revisions.c.id < last_id),
                )
            )

        statement = (
            sa.select(
                documents.c.id.label("document_id"),
                documents.c.kind,
                documents.c.title,
                document_revisions.c.id.label("revision_id"),
                document_revisions.c.body_markdown,
                document_revisions.c.created_at,
                rank_expr.label("rank"),
            )
            .select_from(
                documents.join(
                    document_revisions, document_revisions.c.id == documents.c.current_revision_id
                )
            )
            .where(*conditions)
            .order_by(rank_expr.desc(), document_revisions.c.id.desc())
            .limit(limit + 1)
        )

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            revision_ids = [row.revision_id for row in rows]
            thought_ids = await _thought_ids_for(session, workspace_id, revision_ids)
            entity_names = await _entity_names_for(session, workspace_id, revision_ids)

        items = tuple(
            _to_result(
                row,
                thought_ids.get(row.revision_id, ()),
                entity_names.get(row.revision_id, ()),
                query,
            )
            for row in rows
        )
        next_cursor = (
            _encode_cursor(rows[-1].rank, rows[-1].revision_id) if has_more and rows else None
        )
        return SearchPage(items=items, next_cursor=next_cursor, degraded=False)


_RANK_PRECISION = sa.Numeric(10, 6)


def _rank_expression(query: SearchQuery) -> sa.ColumnElement[Any]:
    """Cast to a fixed-precision ``numeric`` rather than leaving this as the
    ``real`` (float4) ``ts_rank_cd`` returns: a value read back from a
    ``real`` column and round-tripped through a cursor as Python ``float``
    does not compare bit-exactly equal to the same ``real`` widened directly
    to ``double precision`` inside a second query's ``WHERE`` clause - two
    different widening paths for the same on-disk bytes. A fixed-precision
    numeric compares exactly on both sides, which keyset pagination's tie
    handling (rank equal, break by id) depends on.
    """
    text = text_query_string(query)
    if text is None:
        return sa.cast(sa.literal(0.0), _RANK_PRECISION)
    tsquery = sa.func.websearch_to_tsquery("english", text)
    return sa.cast(sa.func.ts_rank_cd(document_revisions.c.body_tsv, tsquery), _RANK_PRECISION)


def _filter_conditions(
    query: SearchQuery, workspace_id: WorkspaceId, *, require_text_match: bool = True
) -> list[sa.ColumnElement[bool]]:
    """Structural filters shared by exact search and semantic hydration.

    ``require_text_match=True`` (exact search, the default) additionally
    requires the combined ``q``/``include``/``exclude`` text to literally
    match via full-text search - exact search has nothing else to go on.
    ``require_text_match=False`` (semantic hydration,
    ``PostgresSemanticHydrator``) skips that positive match, since a semantic
    hit is allowed to be relevant without literally containing ``q``/
    ``include``, but still enforces ``exclude`` as its own negative match:
    Khoj's embedding similarity gives no guarantee a forbidden word is
    actually absent, so a semantic channel must not be trusted to have
    honored it (docs/DESIGN.md 7.5 P1).
    """
    conditions: list[sa.ColumnElement[bool]] = []

    text = text_query_string(query)
    if require_text_match:
        if text is not None:
            conditions.append(
                document_revisions.c.body_tsv.op("@@")(
                    sa.func.websearch_to_tsquery("english", text)
                )
            )
    elif query.exclude:
        exclude_text = " ".join(f"-{word}" for word in query.exclude)
        conditions.append(
            document_revisions.c.body_tsv.op("@@")(
                sa.func.websearch_to_tsquery("english", exclude_text)
            )
        )

    if query.phrase:
        escaped = _escape_like(query.phrase)
        conditions.append(
            sa.or_(
                document_revisions.c.body_markdown.ilike(f"%{escaped}%", escape="\\"),
                documents.c.title.ilike(f"%{escaped}%", escape="\\"),
            )
        )

    if query.kind is not None:
        conditions.append(documents.c.kind == query.kind)

    if query.entity_id is not None:
        conditions.append(
            sa.exists(
                sa.select(1).where(
                    entity_mentions.c.revision_id == document_revisions.c.id,
                    entity_mentions.c.entity_id == query.entity_id,
                    entity_mentions.c.workspace_id == workspace_id,
                )
            )
        )

    # Source and date/time filters both scope "a thought that supports this
    # citation" - they must be joined into a single EXISTS over the same
    # thought row. Two separate EXISTS subqueries would let different cited
    # thoughts each satisfy one predicate (thought A matches the source,
    # thought B matches the date) with no single thought satisfying both,
    # which is not what "this document cites a discord thought from March"
    # means.
    provenance_conditions: list[sa.ColumnElement[bool]] = []
    if query.source is not None:
        provenance_conditions.append(thoughts.c.source == query.source)
    if query.date_from is not None:
        provenance_conditions.append(thoughts.c.client_local_date >= query.date_from)
    if query.date_to is not None:
        provenance_conditions.append(thoughts.c.client_local_date <= query.date_to)
    if query.local_time_from is not None:
        provenance_conditions.append(thoughts.c.client_local_time >= query.local_time_from)
    if query.local_time_to is not None:
        provenance_conditions.append(thoughts.c.client_local_time <= query.local_time_to)
    if provenance_conditions:
        conditions.append(
            sa.exists(
                sa.select(1)
                .select_from(
                    revision_sources.join(thoughts, thoughts.c.id == revision_sources.c.thought_id)
                )
                .where(
                    revision_sources.c.revision_id == document_revisions.c.id,
                    revision_sources.c.workspace_id == workspace_id,
                    *provenance_conditions,
                )
            )
        )

    return conditions


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _thought_ids_for(
    session: AsyncSession, workspace_id: WorkspaceId, revision_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[ThoughtId, ...]]:
    """One query for the whole page rather than one per row.

    ``revision_id`` alone identifies rows uniquely, but every ``revision_sources``
    row is still predicated on ``workspace_id`` - the caller's search already
    scoped ``revision_ids`` to one workspace, and this keeps that same
    boundary explicit at the point that reads citation rows, rather than
    relying on the caller having filtered correctly upstream.
    """
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


async def _entity_names_for(
    session: AsyncSession, workspace_id: WorkspaceId, revision_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, ...]]:
    """Scoped the same way as ``_thought_ids_for``: both ``entity_mentions``
    and the ``entities`` it joins to are predicated on ``workspace_id`` so a
    cross-workspace entity can never be hydrated onto another workspace's
    result."""
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


def _snippet(body: str, query: SearchQuery) -> str:
    needle = query.phrase or query.q
    if needle:
        idx = body.lower().find(needle.lower())
        if idx != -1:
            start = max(0, idx - _SNIPPET_RADIUS // 2)
            end = min(len(body), idx + len(needle) + _SNIPPET_RADIUS // 2)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(body) else ""
            return prefix + body[start:end].strip() + suffix
    truncated = body[:_SNIPPET_RADIUS].strip()
    return truncated + ("…" if len(body) > _SNIPPET_RADIUS else "")


def _to_result(
    row: sa.Row[Any],
    thought_ids: tuple[ThoughtId, ...],
    entity_names: tuple[str, ...],
    query: SearchQuery,
) -> SearchResult:
    return SearchResult(
        result_id=str(row.revision_id),
        document_id=row.document_id,
        revision_id=row.revision_id,
        thought_ids=thought_ids,
        kind=row.kind,
        title=row.title,
        snippet=_snippet(row.body_markdown, query),
        updated_at=row.created_at,
        entities=entity_names,
        channels=("exact",),
        rank=float(row.rank),
    )


def _encode_cursor(rank: decimal.Decimal, revision_id: uuid.UUID) -> str:
    """Opaque to callers, so the pagination key can change without breaking them.

    ``rank`` is carried as ``decimal.Decimal`` end to end (matching
    ``_RANK_PRECISION``), not ``float``: a value read back through Python
    ``float`` and re-encoded loses the guarantee that it compares bit-exactly
    equal to a fresh computation of the same ``numeric`` expression.
    """
    raw = f"{rank}:{revision_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[decimal.Decimal, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        rank_str, _, id_str = raw.partition(":")
        return decimal.Decimal(rank_str), uuid.UUID(id_str)
    except (ValueError, UnicodeDecodeError, binascii.Error, decimal.InvalidOperation) as exc:
        raise InvalidSearchCursorError(f"{cursor!r} is not a valid pagination cursor") from exc


class InvalidSearchCursorError(ValueError):
    """The supplied pagination cursor could not be interpreted."""

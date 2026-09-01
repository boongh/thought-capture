"""Read access to entities, aliases, and the alias-merge review queue.

Separate from ``entity_repository``'s write side for the same reason
``thought_reader`` is separate from ``thought_repository``: reads carry
filters and joins that have nothing to do with the write path (docs/DESIGN.md
5.3). Every query is workspace-scoped.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.entities import EntityResolutionThresholds, EntityType, document_stable_key
from tc_infrastructure.db.tables import entities, entity_aliases, entity_mentions


@dataclass(frozen=True, slots=True)
class EntityRecord:
    id: uuid.UUID
    entity_type: str
    canonical_name: str
    stable_key: str
    aliases: tuple[str, ...]
    mention_count: int
    last_mentioned_at: dt.datetime | None
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """Two entities close enough to have been merged, but not merged.

    Surfaces docs/DESIGN.md 6.4's "Ambiguous candidates remain separate and
    appear in a review queue" - computed at read time from trigram similarity
    rather than persisted, so there is nothing to keep in sync as entities and
    aliases keep changing.
    """

    entity_a: uuid.UUID
    name_a: str
    entity_b: uuid.UUID
    name_b: str
    entity_type: str
    similarity: float


class PostgresEntityReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, workspace_id: WorkspaceId, entity_id: uuid.UUID) -> EntityRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(entities).where(
                        entities.c.id == entity_id, entities.c.workspace_id == workspace_id
                    )
                )
            ).first()
            if row is None:
                return None
            details = await self._details_for(session, [entity_id])
        return _to_record(row, *details[entity_id])

    async def list_entities(
        self,
        workspace_id: WorkspaceId,
        *,
        entity_type: EntityType | None = None,
        limit: int = 200,
    ) -> list[EntityRecord]:
        """Every entity in the workspace, most recently created first.

        Unpaginated up to ``limit``: a personal-memory corpus's entity count is
        orders of magnitude smaller than its thought count (docs/DESIGN.md
        7.3.1 treats the whole index as cheap enough to send in full on every
        organize call), so keyset pagination is not worth the complexity yet.
        """
        conditions = [entities.c.workspace_id == workspace_id]
        if entity_type is not None:
            conditions.append(entities.c.entity_type == str(entity_type))

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(entities)
                    .where(*conditions)
                    .order_by(entities.c.created_at.desc())
                    .limit(limit)
                )
            ).all()
            details = await self._details_for(session, [row.id for row in rows])

        return [_to_record(row, *details[row.id]) for row in rows]

    async def review_queue(
        self,
        workspace_id: WorkspaceId,
        *,
        thresholds: EntityResolutionThresholds | None = None,
        limit: int = 50,
    ) -> list[ReviewCandidate]:
        """Entity pairs in the ambiguous band: close, but not auto-merged."""
        bounds = thresholds or EntityResolutionThresholds()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.text("""
                        SELECT
                            a.id AS entity_a, a.canonical_name AS name_a,
                            b.id AS entity_b, b.canonical_name AS name_b,
                            a.entity_type AS entity_type,
                            similarity(a.normalized_name, b.normalized_name) AS score
                        FROM entities a
                        JOIN entities b
                          ON a.workspace_id = b.workspace_id
                         AND a.entity_type = b.entity_type
                         AND a.id < b.id
                        WHERE a.workspace_id = :workspace_id
                          AND similarity(a.normalized_name, b.normalized_name)
                              >= :floor
                          AND similarity(a.normalized_name, b.normalized_name)
                              < :merge_at
                        ORDER BY score DESC
                        LIMIT :limit
                    """),
                    {
                        "workspace_id": workspace_id,
                        "floor": bounds.ambiguous_floor,
                        "merge_at": bounds.merge_at,
                        "limit": limit,
                    },
                )
            ).all()

        return [
            ReviewCandidate(
                entity_a=row.entity_a,
                name_a=row.name_a,
                entity_b=row.entity_b,
                name_b=row.name_b,
                entity_type=row.entity_type,
                similarity=float(row.score),
            )
            for row in rows
        ]

    async def _details_for(
        self, session: AsyncSession, entity_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[tuple[str, ...], int, dt.datetime | None]]:
        """Aliases, mention count, and last-mentioned time, one query each."""
        if not entity_ids:
            return {}

        alias_rows = (
            await session.execute(
                sa.select(entity_aliases.c.entity_id, entity_aliases.c.alias)
                .where(entity_aliases.c.entity_id.in_(entity_ids))
                .order_by(entity_aliases.c.entity_id, entity_aliases.c.alias)
            )
        ).all()
        aliases: dict[uuid.UUID, list[str]] = {}
        for row in alias_rows:
            aliases.setdefault(row.entity_id, []).append(row.alias)

        mention_rows = (
            await session.execute(
                sa.select(
                    entity_mentions.c.entity_id,
                    sa.func.count().label("mention_count"),
                )
                .where(entity_mentions.c.entity_id.in_(entity_ids))
                .group_by(entity_mentions.c.entity_id)
            )
        ).all()
        counts = {row.entity_id: int(row.mention_count) for row in mention_rows}

        last_mentioned = await self._last_mentioned_at(session, entity_ids)

        return {
            entity_id: (
                tuple(aliases.get(entity_id, ())),
                counts.get(entity_id, 0),
                last_mentioned.get(entity_id),
            )
            for entity_id in entity_ids
        }

    async def _last_mentioned_at(
        self, session: AsyncSession, entity_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dt.datetime]:
        """The most recent run each entity was mentioned in.

        ``entity_mentions`` carries no timestamp of its own; the mentioning
        run's ``created_at`` is the best available proxy, and is exactly what
        the ``recency`` context-assembly signal (docs/DESIGN.md 7.3.2) needs
        too.
        """
        rows = (
            await session.execute(
                sa.text("""
                    SELECT em.entity_id AS entity_id, MAX(r.created_at) AS last_mentioned_at
                    FROM entity_mentions em
                    JOIN runs r ON r.id = em.run_id
                    WHERE em.entity_id = ANY(:entity_ids)
                    GROUP BY em.entity_id
                """),
                {"entity_ids": entity_ids},
            )
        ).all()
        return {row.entity_id: row.last_mentioned_at for row in rows}


def _to_record(
    row: sa.Row[Any],
    aliases: tuple[str, ...],
    mention_count: int,
    last_mentioned_at: dt.datetime | None,
) -> EntityRecord:
    entity_type = EntityType(row.entity_type)
    return EntityRecord(
        id=row.id,
        entity_type=row.entity_type,
        canonical_name=row.canonical_name,
        stable_key=document_stable_key(entity_type, row.normalized_name),
        aliases=aliases,
        mention_count=mention_count,
        last_mentioned_at=last_mentioned_at,
        created_at=row.created_at,
    )

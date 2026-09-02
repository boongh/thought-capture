"""Entity resolution and alias merging (docs/DESIGN.md 6.4).

``resolve_mention`` is the write side the organize pipeline calls once per
detected ``EntityMention``. It does **not** own a transaction: entity and
alias rows must land in the same commit as the document revision that
mentioned them, because ``entity_mentions`` carries a foreign key to that
exact revision (or thought) and the CHECK constraint on the table requires
exactly one of the two. A caller supplies an ``AsyncSession`` that is already
inside its own ``session.begin()`` block - the organize pipeline's - so a
validation failure discovered later in that same transaction rolls every one
of these writes back too, matching docs/DESIGN.md 7.2 step 9's "never write a
partial document set after validation failure".

Matching is trigram similarity (`pg_trgm`, already indexed on
``entities.normalized_name`` and ``entity_aliases.normalized_alias`` by
migration 0004) against the best of an entity's canonical name and its known
aliases. An exact match scores 1.0, so no separate exact-match path is needed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.entities import (
    EntityResolutionThresholds,
    EntityType,
    MatchDecision,
    document_stable_key,
    normalize_entity_name,
)
from tc_infrastructure.db.tables import entities, entity_aliases, entity_mentions

_BEST_CANDIDATE = sa.text("""
    SELECT
        e.id AS entity_id,
        e.canonical_name AS canonical_name,
        e.normalized_name AS normalized_name,
        GREATEST(
            similarity(e.normalized_name, :incoming),
            COALESCE(
                (
                    SELECT MAX(similarity(a.normalized_alias, :incoming))
                    FROM entity_aliases a
                    WHERE a.entity_id = e.id
                ),
                0
            )
        ) AS score
    FROM entities e
    WHERE e.workspace_id = :workspace_id AND e.entity_type = :entity_type
    ORDER BY score DESC
    LIMIT 1
""")


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    """What resolving one mention decided."""

    entity_id: uuid.UUID
    entity_type: EntityType
    canonical_name: str
    stable_key: str
    decision: MatchDecision
    matched_similarity: float | None
    alias_added: bool


class PostgresEntityRepository:
    """Resolves an ``EntityMention`` against the workspace's known entities."""

    def __init__(self, *, thresholds: EntityResolutionThresholds | None = None) -> None:
        self._thresholds = thresholds or EntityResolutionThresholds()

    async def resolve_mention(
        self,
        session: AsyncSession,
        *,
        workspace_id: WorkspaceId,
        run_id: uuid.UUID,
        entity_type: EntityType,
        canonical_name: str,
        surface_form: str,
        confidence: float,
        thought_id: ThoughtId | None = None,
        revision_id: uuid.UUID | None = None,
    ) -> ResolvedEntity:
        """Resolve, merge or create, then record the mention.

        Exactly one of ``thought_id``/``revision_id`` must be given, matching
        ``entity_mentions``' CHECK constraint - a thought-level mention (raw
        text) or a revision-level mention (a written document), never both.
        """
        if (thought_id is None) == (revision_id is None):
            raise ValueError("resolve_mention needs exactly one of thought_id or revision_id")

        normalized = normalize_entity_name(canonical_name)
        candidate = (
            await session.execute(
                _BEST_CANDIDATE,
                {
                    "workspace_id": workspace_id,
                    "entity_type": str(entity_type),
                    "incoming": normalized,
                },
            )
        ).first()

        similarity = float(candidate.score) if candidate is not None else None
        decision = self._thresholds.classify(similarity)

        if decision is MatchDecision.MERGE and candidate is not None:
            entity_id = candidate.entity_id
            resolved_canonical = candidate.canonical_name
            resolved_normalized = candidate.normalized_name
            alias_added = await self._add_alias_if_new(
                session,
                workspace_id=workspace_id,
                entity_id=entity_id,
                candidate_normalized=resolved_normalized,
                names=(canonical_name, surface_form),
            )
        else:
            # AMBIGUOUS and NEW both create a separate entity. The difference
            # is only whether a later review query surfaces it next to the
            # close-but-not-merged candidate (docs/DESIGN.md 6.4).
            entity_id = await self._create_entity(
                session,
                workspace_id=workspace_id,
                entity_type=entity_type,
                canonical_name=canonical_name,
                normalized_name=normalized,
            )
            resolved_canonical = canonical_name
            resolved_normalized = normalized
            alias_added = await self._add_alias_if_new(
                session,
                workspace_id=workspace_id,
                entity_id=entity_id,
                candidate_normalized=resolved_normalized,
                names=(surface_form,),
            )

        await session.execute(
            sa.insert(entity_mentions).values(
                workspace_id=workspace_id,
                entity_id=entity_id,
                thought_id=thought_id,
                revision_id=revision_id,
                run_id=run_id,
                surface_form=surface_form,
                confidence=confidence,
            )
        )

        return ResolvedEntity(
            entity_id=entity_id,
            entity_type=entity_type,
            canonical_name=resolved_canonical,
            stable_key=document_stable_key(entity_type, resolved_normalized),
            decision=decision,
            matched_similarity=similarity,
            alias_added=alias_added,
        )

    async def _create_entity(
        self,
        session: AsyncSession,
        *,
        workspace_id: WorkspaceId,
        entity_type: EntityType,
        canonical_name: str,
        normalized_name: str,
    ) -> uuid.UUID:
        """Insert, or return the existing id on a concurrent duplicate.

        ``ON CONFLICT ... DO UPDATE`` rather than ``DO NOTHING``: only
        ``DO UPDATE`` supports ``RETURNING`` a row that already existed, and
        two thoughts in the same window mentioning a brand-new entity for the
        first time is the normal case this has to handle, not an edge case.
        The ``SET`` targets ``canonical_name`` specifically because migration
        0005 grants the application role column-level ``UPDATE`` on
        ``entities`` for that column only; any other column raises
        ``InsufficientPrivilege``. Re-assigning it to the value that lost the
        race is harmless - both writers were proposing the same
        ``normalized_name``, so this only decides whose display capitalization
        wins a concurrent first mention.
        """
        entity_id = uuid.uuid4()
        statement = (
            pg_insert(entities)
            .values(
                id=entity_id,
                workspace_id=workspace_id,
                entity_type=str(entity_type),
                canonical_name=canonical_name,
                normalized_name=normalized_name,
            )
            .on_conflict_do_update(
                index_elements=["workspace_id", "entity_type", "normalized_name"],
                set_={"canonical_name": canonical_name},
            )
            .returning(entities.c.id)
        )
        resolved = await session.scalar(statement)
        assert resolved is not None  # RETURNING always yields a row here
        return uuid.UUID(str(resolved))

    async def _add_alias_if_new(
        self,
        session: AsyncSession,
        *,
        workspace_id: WorkspaceId,
        entity_id: uuid.UUID,
        candidate_normalized: str,
        names: tuple[str, ...],
    ) -> bool:
        """Add whichever of ``names`` is not already the canonical name or a known alias.

        So that the *next* mention of this surface form resolves by exact
        match rather than needing the trigram search to find it again.
        """
        added = False
        for name in names:
            normalized_alias = normalize_entity_name(name)
            if not normalized_alias or normalized_alias == candidate_normalized:
                continue
            # ``RETURNING`` rather than ``rowcount``: the async psycopg dialect
            # does not reliably report an affected-row count for a plain
            # ``INSERT ... ON CONFLICT DO NOTHING`` with no ``RETURNING``
            # clause, so the row itself - present only when the insert
            # actually happened - is what ``added`` has to check instead.
            result = await session.execute(
                pg_insert(entity_aliases)
                .values(
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    alias=name,
                    normalized_alias=normalized_alias,
                )
                .on_conflict_do_nothing(index_elements=["entity_id", "normalized_alias"])
                .returning(entity_aliases.c.normalized_alias)
            )
            added = added or result.first() is not None
        return added

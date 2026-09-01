"""Read side of Tier 1 context assembly: the complete entity-document index.

Implements docs/DESIGN.md 7.3.1's "one index row for every entity document in
the workspace, with no selection applied" - every entity, whether or not it
has been mentioned recently, joined against its current document body (when
one exists yet) and its open-thread count.
"""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.context import Tier1Row
from tc_domain.entities import EntityType, document_stable_key

# The fixed section order document bodies use (organize_contract.REQUIRED_SECTIONS);
# duplicated here as a marker string rather than imported, because that
# contract lives in `tc_application` and this module must not depend upward
# on it (docs/DESIGN.md 5.3's dependency direction runs the other way).
_SUMMARY_START = "## Summary"
_NEXT_SECTION = "\n## "


class PostgresContextIndex:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def tier1_index(self, workspace_id: WorkspaceId) -> tuple[Tier1Row, ...]:
        async with self._session_factory() as session:
            entity_rows = (
                await session.execute(
                    sa.text("""
                        SELECT id, entity_type, canonical_name, normalized_name
                        FROM entities
                        WHERE workspace_id = :workspace_id
                    """),
                    {"workspace_id": workspace_id},
                )
            ).all()
            if not entity_rows:
                return ()

            entity_ids = [row.id for row in entity_rows]
            aliases = await self._aliases_for(session, entity_ids)
            last_mentioned = await self._last_mentioned_at(session, entity_ids)
            open_threads = await self._open_thread_counts(session, entity_ids)

            stable_keys = {
                row.id: document_stable_key(EntityType(row.entity_type), row.normalized_name)
                for row in entity_rows
            }
            bodies = await self._current_bodies(session, workspace_id, list(stable_keys.values()))

        return tuple(
            Tier1Row(
                stable_key=stable_keys[row.id],
                entity_type=row.entity_type,
                canonical_name=row.canonical_name,
                aliases=tuple(aliases.get(row.id, ())),
                summary=_extract_summary(bodies.get(stable_keys[row.id], "")),
                last_mentioned_at=last_mentioned.get(row.id),
                open_thread_count=open_threads.get(row.id, 0),
            )
            for row in entity_rows
        )

    async def _aliases_for(
        self, session: AsyncSession, entity_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[str]]:
        rows = (
            await session.execute(
                sa.text("""
                    SELECT entity_id, alias FROM entity_aliases
                    WHERE entity_id = ANY(:entity_ids)
                    ORDER BY entity_id, alias
                """),
                {"entity_ids": entity_ids},
            )
        ).all()
        grouped: dict[uuid.UUID, list[str]] = {}
        for row in rows:
            grouped.setdefault(row.entity_id, []).append(row.alias)
        return grouped

    async def _last_mentioned_at(
        self, session: AsyncSession, entity_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dt.datetime]:
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

    async def _open_thread_counts(
        self, session: AsyncSession, entity_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Distinct current `todo`/`decision` documents mentioning each entity.

        Scoped to the document's *current* revision only: a resolved todo
        whose current body no longer mentions the entity must stop counting,
        even though the mention row from the revision that first raised it is
        still there for history.
        """
        rows = (
            await session.execute(
                sa.text("""
                    SELECT em.entity_id AS entity_id,
                           COUNT(DISTINCT d.id) AS open_count
                    FROM entity_mentions em
                    JOIN documents d ON d.current_revision_id = em.revision_id
                    WHERE em.entity_id = ANY(:entity_ids)
                      AND d.kind IN ('todo', 'decision')
                    GROUP BY em.entity_id
                """),
                {"entity_ids": entity_ids},
            )
        ).all()
        return {row.entity_id: int(row.open_count) for row in rows}

    async def _current_bodies(
        self, session: AsyncSession, workspace_id: WorkspaceId, stable_keys: list[str]
    ) -> dict[str, str]:
        if not stable_keys:
            return {}
        rows = (
            await session.execute(
                sa.text("""
                    SELECT d.stable_key AS stable_key, rev.body_markdown AS body_markdown
                    FROM documents d
                    JOIN document_revisions rev ON rev.id = d.current_revision_id
                    WHERE d.workspace_id = :workspace_id
                      AND d.stable_key = ANY(:stable_keys)
                """),
                {"workspace_id": workspace_id, "stable_keys": stable_keys},
            )
        ).all()
        return {row.stable_key: row.body_markdown for row in rows}


def _extract_summary(body: str) -> str:
    """The bounded ``## Summary`` section - the Tier 1 index line's source."""
    start = body.find(_SUMMARY_START)
    if start == -1:
        return ""
    start += len(_SUMMARY_START)
    end = body.find(_NEXT_SECTION, start)
    section = body[start:] if end == -1 else body[start:end]
    return section.strip()

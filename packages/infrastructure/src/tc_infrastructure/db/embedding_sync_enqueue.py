"""Shared insert for ``embedding.sync_requested`` outbox events.

Used by ``PostgresOrganizeWriter`` (one event per document written this run,
inside the write transaction) and the embedding force-sync endpoint (one
event per current document, its own transaction) - both need the exact same
row shape, so it lives once here rather than twice. Mirrors
``khoj_sync_enqueue.py`` exactly (docs/adr/0010-self-hosted-embedding-search-ask.md).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tc_infrastructure.db.tables import outbox_events

EMBEDDING_SYNC_REQUESTED_EVENT = "embedding.sync_requested"


async def enqueue_embedding_sync(
    session: AsyncSession, *, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Payload carries ``document_id`` only, never ``revision_id`` - the sync
    consumer always re-reads the document's *current* revision at delivery
    time (``EmbeddingSource.get_revision``), so an event queued by an earlier
    write that is delivered after a later one's event cannot overwrite the
    stored embedding with stale content."""
    await session.execute(
        sa.insert(outbox_events).values(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            event_type=EMBEDDING_SYNC_REQUESTED_EVENT,
            aggregate_id=str(document_id),
            payload={"document_id": str(document_id)},
        )
    )

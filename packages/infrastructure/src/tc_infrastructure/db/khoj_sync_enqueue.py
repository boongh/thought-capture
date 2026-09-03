"""Shared insert for ``khoj.sync_requested`` outbox events.

Used by ``PostgresOrganizeWriter`` (one event per document written this run,
inside the write transaction) and the ``/v1/admin/khoj-sync`` force-sync
endpoint (one event per current document, its own transaction) - both need
the exact same row shape, so it lives once here rather than twice.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tc_infrastructure.db.tables import outbox_events

KHOJ_SYNC_REQUESTED_EVENT = "khoj.sync_requested"


async def enqueue_khoj_sync(
    session: AsyncSession, *, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Payload carries ``document_id`` only, never ``revision_id`` - the sync
    consumer always re-reads the document's *current* revision at delivery
    time (``KhojExportSource.get_export``), so an event queued by an earlier
    write that is delivered after a later one's event cannot overwrite Khoj
    with stale content."""
    await session.execute(
        sa.insert(outbox_events).values(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            event_type=KHOJ_SYNC_REQUESTED_EVENT,
            aggregate_id=str(document_id),
            payload={"document_id": str(document_id)},
        )
    )

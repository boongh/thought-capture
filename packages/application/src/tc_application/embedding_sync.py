"""Delivers due ``embedding.sync_requested`` events (docs/DESIGN.md 8.4,
docs/adr/0010-self-hosted-embedding-search-ask.md)."""

from __future__ import annotations

import logging

from tc_domain.capture import WorkspaceId
from tc_domain.embedding_ports import (
    EmbeddingForceSyncPort,
    EmbeddingPort,
    EmbeddingSource,
    EmbeddingSyncOutbox,
    EmbeddingWriter,
    PendingEmbeddingSync,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 20


class DeliverEmbeddingSync:
    """One poll cycle: claim due sync events and (re)compute each document's
    current-revision embedding into pgvector."""

    def __init__(
        self,
        *,
        outbox: EmbeddingSyncOutbox,
        source: EmbeddingSource,
        embed: EmbeddingPort,
        writer: EmbeddingWriter,
        batch_size: int = DEFAULT_BATCH,
    ) -> None:
        self._outbox = outbox
        self._source = source
        self._embed = embed
        self._writer = writer
        self._batch_size = batch_size

    async def __call__(self) -> int:
        """Claim due events and sync each. Return the count actually synced."""
        pending = await self._outbox.claim(self._batch_size)
        synced = 0
        for event in pending:
            if await self._sync_one(event):
                synced += 1
        return synced

    async def _sync_one(self, event: PendingEmbeddingSync) -> bool:
        # mark_delivered lives inside this same try, not after it - same
        # reasoning as DeliverKhojSync._sync_one: PostgresEmbeddingWriter.upsert
        # is idempotent for a given (document_id, revision_id), so a failure
        # in mark_delivered itself, after the vector is already durably
        # stored, is safe to retry the same way an upstream failure is.
        # Leaving it unprotected would instead let an exception there escape
        # _sync_one entirely: the event would be neither delivered nor
        # failed, and any other already-claimed event later in this batch
        # would sit leased until its lease expires rather than being retried
        # next poll.
        #
        # Unlike DeliverKhojSync, not every success path here is a genuine
        # write: ``writer.upsert`` returns False when a strictly-newer
        # revision is already stored (the out-of-order-redelivery guard,
        # docs/DESIGN.md 8.4). That is still a correctly handled event - there
        # was nothing newer to write - so both outcomes are "delivered",
        # never "failed".
        try:
            revision = await self._source.get_revision(
                workspace_id=event.workspace_id, document_id=event.document_id
            )
            (vector,) = await self._embed.embed((revision.body_markdown,))
            await self._writer.upsert(
                workspace_id=event.workspace_id,
                document_id=event.document_id,
                revision_id=revision.revision_id,
                revision_number=revision.revision_number,
                vector=vector,
            )
            await self._outbox.mark_delivered(event.event_id)
        except Exception as exc:
            # Sanitized: the exception class only, never the message (docs/DESIGN.md
            # 14.2) - an embedding-sidecar HTTP error can carry response
            # bodies, a database error can carry statement parameters.
            logger.warning(
                "embedding_sync.failed",
                extra={"event_id": str(event.event_id), "error_class": type(exc).__name__},
            )
            await self._outbox.mark_failed(
                event.event_id, attempts=event.attempts, error=type(exc).__name__
            )
            return False
        return True


class ForceEmbeddingSync:
    """``POST /v1/admin/embedding-sync`` (docs/DESIGN.md 10): enqueue sync for
    every current document. Delivery is the periodic ``DeliverEmbeddingSync``
    loop's job, not this call's."""

    def __init__(self, enqueuer: EmbeddingForceSyncPort) -> None:
        self._enqueuer = enqueuer

    async def __call__(self, workspace_id: WorkspaceId) -> int:
        return await self._enqueuer.enqueue_all(workspace_id)

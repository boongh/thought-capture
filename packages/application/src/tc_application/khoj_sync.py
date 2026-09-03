"""Delivers due ``khoj.sync_requested`` events (docs/DESIGN.md 6.5, 7.2 step 11)."""

from __future__ import annotations

import hashlib
import logging

from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import render_document_markdown
from tc_domain.khoj_ports import KhojIndexFile, KhojPort
from tc_domain.khoj_sync_ports import (
    KhojExportSource,
    KhojForceSyncPort,
    KhojIndexRecorder,
    KhojSyncOutbox,
    PendingKhojSync,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 20


class DeliverKhojSync:
    """One poll cycle: claim due sync events and push each document's current
    revision to Khoj."""

    def __init__(
        self,
        *,
        outbox: KhojSyncOutbox,
        source: KhojExportSource,
        khoj: KhojPort,
        recorder: KhojIndexRecorder,
        batch_size: int = DEFAULT_BATCH,
    ) -> None:
        self._outbox = outbox
        self._source = source
        self._khoj = khoj
        self._recorder = recorder
        self._batch_size = batch_size

    async def __call__(self) -> int:
        """Claim due events and sync each. Return the count actually synced."""
        pending = await self._outbox.claim(self._batch_size)
        synced = 0
        for event in pending:
            if await self._sync_one(event):
                synced += 1
        return synced

    async def _sync_one(self, event: PendingKhojSync) -> bool:
        # record_synced/mark_delivered live inside this same try, not after
        # it: HttpKhojClient.index() is proven idempotent for a given
        # filename (tests/contract/khoj/test_khoj_client.py), so a failure in
        # either of those two steps - after Khoj already has the content -
        # is safe to retry the same way an upstream failure is. Leaving them
        # unprotected would instead let an exception there escape _sync_one
        # entirely: the event would be neither delivered nor failed, and any
        # other already-claimed event later in this batch would sit leased
        # until its lease expires rather than being retried next poll.
        try:
            export = await self._source.get_export(
                workspace_id=event.workspace_id, document_id=event.document_id
            )
            body = render_document_markdown(export)
            await self._khoj.index((KhojIndexFile(filename=export.filename, content=body),))
            await self._recorder.record_synced(
                workspace_id=event.workspace_id,
                document_id=export.document_id,
                filename=export.filename,
                revision_id=export.revision_id,
                # Hashes the same bytes just sent to Khoj, front matter
                # included - not ``body_markdown`` alone - so
                # ``khoj_index_items.body_sha256`` reflects exactly what is
                # indexed.
                body_sha256=hashlib.sha256(body).hexdigest(),
            )
            await self._outbox.mark_delivered(event.event_id)
        except Exception as exc:
            # Sanitized: the exception class only, never the message (docs/DESIGN.md
            # 14.2) - a Khoj HTTP error can carry response bodies, a database
            # error can carry statement parameters.
            logger.warning(
                "khoj_sync.failed",
                extra={"event_id": str(event.event_id), "error_class": type(exc).__name__},
            )
            await self._outbox.mark_failed(
                event.event_id, attempts=event.attempts, error=type(exc).__name__
            )
            return False
        return True


class ForceKhojSync:
    """``POST /v1/admin/khoj-sync`` (docs/DESIGN.md 10): enqueue sync for
    every current document. Delivery is the periodic ``DeliverKhojSync``
    loop's job, not this call's."""

    def __init__(self, enqueuer: KhojForceSyncPort) -> None:
        self._enqueuer = enqueuer

    async def __call__(self, workspace_id: WorkspaceId) -> int:
        return await self._enqueuer.enqueue_all(workspace_id)

"""Background poller that delivers due ``embedding.sync_requested`` outbox
events.

Structurally identical to ``tc_worker.khoj_sync_loop.KhojSyncLoop`` - same
shape of problem (drive one outbox event type to completion on a fixed
interval), just for the pgvector embedding-sync path instead of Khoj
(docs/DESIGN.md 8.4, docs/adr/0010-self-hosted-embedding-search-ask.md).
"""

from __future__ import annotations

import asyncio
import logging

from tc_application.embedding_sync import DeliverEmbeddingSync
from tc_application.reembed import ReconcileReembedRuns

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30.0


class EmbeddingSyncLoop:
    """Polls ``DeliverEmbeddingSync`` on an interval until stopped.

    ``reconcile`` runs after each poll cycle's delivery, not before or on
    its own timer: a reembed run's completion depends on the very outbox
    delivery this loop just drove, so checking right after gives
    ``runs(kind='reembed')`` the freshest possible read without a second
    scheduled task (F15-A, docs/plans/embedding-sync-review-round-4.md).
    """

    def __init__(
        self,
        deliver: DeliverEmbeddingSync,
        *,
        reconcile: ReconcileReembedRuns,
        interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._deliver = deliver
        self._reconcile = reconcile
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                synced = await self._deliver()
                if synced:
                    logger.info("embedding_sync.synced_batch", extra={"count": synced})
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A poll-cycle failure (e.g. a database blip or an
                # unreachable embedding sidecar) must not kill the loop; the
                # next cycle tries again.
                logger.error("embedding_sync.loop_error", extra={"error_class": type(exc).__name__})
            await asyncio.sleep(self._interval)

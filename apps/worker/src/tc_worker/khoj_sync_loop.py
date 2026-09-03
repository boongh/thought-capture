"""Background poller that delivers due ``khoj.sync_requested`` outbox events.

Identical structure to ``tc_discord_bot.digest_loop.DigestDeliveryLoop`` - both
are the same shape of problem (drive one outbox event type to completion on a
fixed interval). Lives in the worker rather than the bot process because,
unlike digest delivery, it has no Discord dependency, and docs/DESIGN.md 5.1
assigns "Khoj sync" to the worker.
"""

from __future__ import annotations

import asyncio
import logging

from tc_application.khoj_sync import DeliverKhojSync

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30.0


class KhojSyncLoop:
    """Polls ``DeliverKhojSync`` on an interval until stopped."""

    def __init__(
        self, deliver: DeliverKhojSync, *, interval: float = POLL_INTERVAL_SECONDS
    ) -> None:
        self._deliver = deliver
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
                    logger.info("khoj_sync.synced_batch", extra={"count": synced})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A poll-cycle failure (e.g. a database blip) must not kill
                # the loop; the next cycle tries again.
                logger.error("khoj_sync.loop_error", extra={"error_class": type(exc).__name__})
            await asyncio.sleep(self._interval)

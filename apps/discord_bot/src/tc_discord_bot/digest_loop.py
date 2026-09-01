"""Background poller that delivers due ``digest.ready`` outbox events.

The persistent retry runtime for the outbox otherwise does not exist
(pre-existing gap, docs/DESIGN.md 6.5); this loop is the first consumer that
actually claims and drives events to completion, on a fixed interval rather
than a config knob, since there is no operator need yet to tune it.
"""

from __future__ import annotations

import asyncio
import logging

from tc_application.digest_delivery import DeliverDigests

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30.0


class DigestDeliveryLoop:
    """Polls ``DeliverDigests`` on an interval until stopped."""

    def __init__(self, deliver: DeliverDigests, *, interval: float = POLL_INTERVAL_SECONDS) -> None:
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
                delivered = await self._deliver()
                if delivered:
                    logger.info("digest.delivered_batch", extra={"count": delivered})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A poll-cycle failure (e.g. a database blip) must not kill
                # the loop; the next cycle tries again.
                logger.error("digest.loop_error", extra={"error_class": type(exc).__name__})
            await asyncio.sleep(self._interval)

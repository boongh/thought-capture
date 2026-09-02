"""Delivers due ``digest.ready`` events (docs/DESIGN.md 4.2, 6.5)."""

from __future__ import annotations

import logging

from tc_domain.digest import format_digest_messages
from tc_domain.digest_ports import DigestOutbox, DigestSender, DigestSource, PendingDigest

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 20


class DeliverDigests:
    """One poll cycle: claim due digest events and attempt each delivery."""

    def __init__(
        self,
        *,
        outbox: DigestOutbox,
        source: DigestSource,
        sender: DigestSender,
        batch_size: int = DEFAULT_BATCH,
    ) -> None:
        self._outbox = outbox
        self._source = source
        self._sender = sender
        self._batch_size = batch_size

    async def __call__(self) -> int:
        """Claim due events and deliver each. Return the count actually delivered."""
        pending = await self._outbox.claim(self._batch_size)
        delivered = 0
        for event in pending:
            if await self._deliver_one(event):
                delivered += 1
        return delivered

    async def _deliver_one(self, event: PendingDigest) -> bool:
        try:
            content = await self._source.get(
                workspace_id=event.workspace_id,
                run_id=event.run_id,
                document_id=event.document_id,
                revision_id=event.revision_id,
            )
            chunks = format_digest_messages(content)
            sent = await self._sender.send(chunks)
        except Exception as exc:
            # Sanitized: the exception class only, never the message or chain
            # (docs/DESIGN.md 14.2) - a delivery failure can carry Discord
            # response bodies or database statement parameters.
            logger.warning(
                "digest.delivery_failed",
                extra={"event_id": str(event.event_id), "error_class": type(exc).__name__},
            )
            await self._outbox.mark_failed(
                event.event_id, attempts=event.attempts, error=type(exc).__name__
            )
            return False

        if sent:
            await self._outbox.mark_delivered(event.event_id)
            return True

        await self._outbox.mark_failed(
            event.event_id, attempts=event.attempts, error="send_returned_false"
        )
        return False

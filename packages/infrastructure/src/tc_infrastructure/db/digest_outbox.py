"""A digest-only view over the generic outbox.

Restricting ``claim`` to ``digest.ready`` (via ``PostgresOutbox``'s
``event_types`` filter) keeps this consumer from ever leasing a
``thought.captured`` acknowledgement it has no logic to deliver.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.digest_ports import PendingDigest
from tc_infrastructure.db.outbox import PostgresOutbox

DIGEST_READY_EVENT = "digest.ready"


class PostgresDigestOutbox:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, lease_owner: str
    ) -> None:
        self._outbox = PostgresOutbox(session_factory, lease_owner=lease_owner)

    async def claim(self, limit: int) -> tuple[PendingDigest, ...]:
        events = await self._outbox.claim(limit, event_types=(DIGEST_READY_EVENT,))
        pending: list[PendingDigest] = []
        for event in events:
            try:
                pending.append(
                    PendingDigest(
                        event_id=event.id,
                        workspace_id=event.workspace_id,
                        run_id=uuid.UUID(str(event.payload["run_id"])),
                        document_id=uuid.UUID(str(event.payload["document_id"])),
                        revision_id=uuid.UUID(str(event.payload["revision_id"])),
                        attempts=event.attempts,
                    )
                )
            except (KeyError, ValueError) as exc:
                # A payload that will never parse must not be retried forever;
                # fail it immediately rather than returning it to the caller.
                await self._outbox.mark_failed(
                    event.id, event.attempts, f"malformed_payload:{type(exc).__name__}"
                )
        return tuple(pending)

    async def mark_delivered(self, event_id: uuid.UUID) -> None:
        await self._outbox.mark_delivered(event_id)

    async def mark_failed(self, event_id: uuid.UUID, *, attempts: int, error: str) -> None:
        await self._outbox.mark_failed(event_id, attempts, error)

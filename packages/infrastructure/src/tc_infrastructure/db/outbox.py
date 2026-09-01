"""Transactional outbox consumer.

Delivery is at-least-once, never exactly-once: a worker can succeed and then die
before recording that it succeeded. Consumers must therefore be idempotent. For
capture acknowledgements that is cheap - a duplicate reply is a cosmetic fault,
whereas a missing one is a broken promise (docs/DESIGN.md 6.5).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_LEASE = dt.timedelta(minutes=2)
DEFAULT_BATCH = 20

# Exponential backoff, capped. A Discord outage should not spin.
BASE_BACKOFF = dt.timedelta(seconds=10)
MAX_BACKOFF = dt.timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    id: uuid.UUID
    workspace_id: uuid.UUID
    event_type: str
    aggregate_id: str
    payload: dict[str, Any]
    attempts: int


def backoff_for(attempts: int) -> dt.timedelta:
    """Exponential backoff capped at ``MAX_BACKOFF``."""
    if attempts <= 0:
        return BASE_BACKOFF
    seconds = BASE_BACKOFF.total_seconds() * (2 ** min(attempts - 1, 16))
    return min(dt.timedelta(seconds=seconds), MAX_BACKOFF)


class PostgresOutbox:
    """Claims, completes, and reschedules outbox work."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        lease_owner: str,
        lease_duration: dt.timedelta = DEFAULT_LEASE,
    ) -> None:
        self._session_factory = session_factory
        self._lease_owner = lease_owner
        self._lease = lease_duration

    async def claim(self, limit: int = DEFAULT_BATCH) -> list[OutboxEvent]:
        """Lease up to ``limit`` due events.

        ``FOR UPDATE SKIP LOCKED`` inside a CTE so that concurrent workers take
        disjoint sets instead of blocking on each other. ``attempts`` is
        incremented at claim time, not at failure time: a worker that dies
        mid-delivery must still consume its budget, or a permanently poisonous
        event would be retried forever.
        """
        statement = sa.text("""
            WITH claimable AS (
                SELECT id
                FROM outbox_events
                WHERE delivered_at IS NULL
                  AND available_at <= now()
                  AND attempts < max_attempts
                  AND (leased_until IS NULL OR leased_until < now())
                ORDER BY available_at, id
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            UPDATE outbox_events AS o
               SET leased_until = now() + :lease,
                   lease_owner  = :owner,
                   attempts     = o.attempts + 1
              FROM claimable AS c
             WHERE o.id = c.id
         RETURNING o.id, o.workspace_id, o.event_type, o.aggregate_id, o.payload, o.attempts
        """)

        async with self._session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    statement,
                    {"limit": limit, "lease": self._lease, "owner": self._lease_owner},
                )
            ).all()

        return [
            OutboxEvent(
                id=row.id,
                workspace_id=row.workspace_id,
                event_type=row.event_type,
                aggregate_id=row.aggregate_id,
                payload=row.payload,
                attempts=row.attempts,
            )
            for row in rows
        ]

    async def mark_delivered(self, event_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.text("""
                    UPDATE outbox_events
                       SET delivered_at = now(), leased_until = NULL, lease_owner = NULL
                     WHERE id = :id AND delivered_at IS NULL
                """),
                {"id": event_id},
            )
        logger.info("outbox.delivered", extra={"event_id": str(event_id)})

    async def mark_failed(self, event_id: uuid.UUID, attempts: int, error: str) -> None:
        """Reschedule with backoff. ``error`` must not contain personal content."""
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.text("""
                    UPDATE outbox_events
                       SET available_at = now() + :backoff,
                           leased_until = NULL,
                           lease_owner = NULL,
                           last_error = :error
                     WHERE id = :id
                """),
                {"id": event_id, "backoff": backoff_for(attempts), "error": error[:500]},
            )
        logger.warning(
            "outbox.failed",
            extra={"event_id": str(event_id), "attempts": attempts, "error_class": error[:80]},
        )

    async def find_pending(self, event_type: str, aggregate_id: str) -> uuid.UUID | None:
        """The undelivered event for one aggregate, if there is one.

        Used by the fast acknowledgement path to settle the event it has just
        satisfied, so the consumer does not send a second copy.
        """
        async with self._session_factory() as session:
            found = await session.scalar(
                sa.text("""
                    SELECT id FROM outbox_events
                     WHERE event_type = :event_type
                       AND aggregate_id = :aggregate_id
                       AND delivered_at IS NULL
                     LIMIT 1
                """),
                {"event_type": event_type, "aggregate_id": aggregate_id},
            )
        return uuid.UUID(str(found)) if found is not None else None

    async def pending_count(self) -> int:
        """Undelivered events, for the readiness endpoint and metrics."""
        async with self._session_factory() as session:
            count = await session.scalar(
                sa.text("SELECT count(*) FROM outbox_events WHERE delivered_at IS NULL")
            )
        return int(count or 0)

"""Transactional outbox persistence and leasing primitives.

When a persistent consumer is wired, delivery is at-least-once, never
exactly-once: a worker can succeed and then die before recording that it
succeeded. Consumers must therefore be idempotent. For capture acknowledgements
that is cheap - a duplicate reply is a cosmetic fault, whereas a missing one is
a broken promise (docs/DESIGN.md 6.5).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
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
        # ``lease_owner`` as supplied is a per-service name ("worker",
        # "discord-bot"), not unique per process. Two replicas of the same
        # service would otherwise pass each other's lease fence. Append an
        # instance-unique suffix so the token actually identifies *this*
        # process for claim/mark_delivered/mark_failed fencing.
        self._lease_owner = lease_owner
        self._lease_token = f"{lease_owner}:{uuid.uuid4().hex[:12]}"
        self._lease = lease_duration

    @property
    def lease_token(self) -> str:
        """The instance-unique lease token used to claim and fence events."""
        return self._lease_token

    async def claim(
        self, limit: int = DEFAULT_BATCH, *, event_types: Sequence[str] | None = None
    ) -> list[OutboxEvent]:
        """Lease up to ``limit`` due events, optionally restricted to ``event_types``.

        ``FOR UPDATE SKIP LOCKED`` inside a CTE so that concurrent workers take
        disjoint sets instead of blocking on each other. ``attempts`` is
        incremented at claim time, not at failure time: a worker that dies
        mid-delivery must still consume its budget, or a permanently poisonous
        event would be retried forever.

        A consumer dedicated to one event type (e.g. digest delivery) must
        pass ``event_types`` so it never leases work it does not know how to
        handle, leaving other types for their own consumers.
        """
        type_filter = "AND event_type = ANY(:event_types)" if event_types is not None else ""
        statement = sa.text(f"""
            WITH claimable AS (
                SELECT id
                FROM outbox_events
                WHERE delivered_at IS NULL
                  AND available_at <= now()
                  AND attempts < max_attempts
                  AND (leased_until IS NULL OR leased_until < now())
                  {type_filter}
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

        params: dict[str, Any] = {"limit": limit, "lease": self._lease, "owner": self._lease_token}
        if event_types is not None:
            params["event_types"] = list(event_types)

        async with self._session_factory() as session, session.begin():
            rows = (await session.execute(statement, params)).all()

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
        """Complete a claimed event. Fenced on the lease this instance holds.

        A worker whose lease has already expired (e.g. it stalled past
        ``lease_duration``) may still be finishing delivery after a second
        worker has re-claimed the same event. Without the ``lease_owner`` /
        ``leased_until`` fence, the stale worker's ``mark_delivered`` would
        mark the event done out from under the worker actually processing it
        now. A lost lease means another worker owns the event, which is
        correct - it is logged, not raised.
        """
        # RETURNING rather than rowcount: the async psycopg dialect does not
        # reliably report an affected-row count for a plain UPDATE, so
        # whether a row actually came back is what the fence has to check.
        async with self._session_factory() as session, session.begin():
            fenced = await session.scalar(
                sa.text("""
                    UPDATE outbox_events
                       SET delivered_at = now(), leased_until = NULL, lease_owner = NULL
                     WHERE id = :id AND delivered_at IS NULL
                       AND lease_owner = :owner AND leased_until > now()
                 RETURNING id
                """),
                {"id": event_id, "owner": self._lease_token},
            )
        if fenced is None:
            logger.warning(
                "outbox.lease_lost",
                extra={"event_id": str(event_id), "lease_owner": self._lease_token},
            )
            return
        logger.info("outbox.delivered", extra={"event_id": str(event_id)})

    async def mark_failed(self, event_id: uuid.UUID, attempts: int, error: str) -> None:
        """Reschedule with backoff. ``error`` must not contain personal content.

        Fenced identically to ``mark_delivered``: without the lease check, a
        stale worker's failure handling would clear ``leased_until`` /
        ``lease_owner`` for whichever worker currently holds the event,
        inviting a third concurrent claim.
        """
        async with self._session_factory() as session, session.begin():
            fenced = await session.scalar(
                sa.text("""
                    UPDATE outbox_events
                       SET available_at = now() + :backoff,
                           leased_until = NULL,
                           lease_owner = NULL,
                           last_error = :error
                     WHERE id = :id
                       AND lease_owner = :owner AND leased_until > now()
                 RETURNING id
                """),
                {
                    "id": event_id,
                    "backoff": backoff_for(attempts),
                    "error": error[:500],
                    "owner": self._lease_token,
                },
            )
        if fenced is None:
            logger.warning(
                "outbox.lease_lost",
                extra={"event_id": str(event_id), "lease_owner": self._lease_token},
            )
            return
        logger.warning(
            "outbox.failed",
            extra={"event_id": str(event_id), "attempts": attempts, "error_class": error[:80]},
        )

    async def mark_dead_lettered(self, event_id: uuid.UUID, error: str) -> None:
        """Terminally fail an event that will never parse or succeed.

        Unlike ``mark_failed``, this does not reschedule: it forces
        ``attempts`` to ``max_attempts`` so ``claim``'s ``attempts <
        max_attempts`` predicate excludes the row forever, instead of
        re-leasing a poison payload every backoff interval until its retry
        budget happens to run out. ``delivered_at`` is deliberately left
        NULL - setting it would assert the event was delivered, which is
        false, and would hide the row from consumers (e.g.
        ``has_dead_lettered_sync_events``) that need to see a terminal
        failure to stop waiting on it.

        Fenced identically to ``mark_failed``: without the lease check, a
        stale worker could dead-letter an event a second worker has since
        re-claimed and is actively processing.
        """
        async with self._session_factory() as session, session.begin():
            fenced = await session.scalar(
                sa.text("""
                    UPDATE outbox_events
                       SET attempts = max_attempts,
                           leased_until = NULL,
                           lease_owner = NULL,
                           last_error = :error
                     WHERE id = :id
                       AND lease_owner = :owner AND leased_until > now()
                 RETURNING id
                """),
                {
                    "id": event_id,
                    "error": error[:500],
                    "owner": self._lease_token,
                },
            )
        if fenced is None:
            logger.warning(
                "outbox.lease_lost",
                extra={"event_id": str(event_id), "lease_owner": self._lease_token},
            )
            return
        logger.warning(
            "outbox.dead_lettered",
            extra={"event_id": str(event_id), "error_class": error[:80]},
        )

    async def settle_unclaimed(self, event_id: uuid.UUID) -> None:
        """Settle an event that was never leased through ``claim``.

        Used by the fast-acknowledgement path: it satisfies the event's
        intent synchronously (e.g. sends the Discord reply itself) and then
        settles the matching outbox row directly, without ever calling
        ``claim``. It therefore holds no lease and must not be fenced on one
        - unlike ``mark_delivered``, which fences to protect a lease held by
        a concurrent consumer. Do not change this back to ``mark_delivered``:
        this row's ``lease_owner`` is typically NULL, so a lease fence would
        make this path a silent no-op and produce a duplicate reply. Instead
        this only refuses to settle a row a live consumer currently holds.
        """
        async with self._session_factory() as session, session.begin():
            settled = await session.scalar(
                sa.text("""
                    UPDATE outbox_events
                       SET delivered_at = now(), leased_until = NULL, lease_owner = NULL
                     WHERE id = :id AND delivered_at IS NULL
                       AND (leased_until IS NULL OR leased_until < now())
                 RETURNING id
                """),
                {"id": event_id},
            )
        if settled is None:
            logger.warning(
                "outbox.settle_unclaimed_lease_held",
                extra={"event_id": str(event_id)},
            )
            return
        logger.info("outbox.delivered", extra={"event_id": str(event_id)})

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

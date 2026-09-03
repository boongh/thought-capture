"""Outbox claim, lease, retry, and delivery semantics.

Delivery is at-least-once. What must never happen is an event being lost, or two
workers delivering the same event concurrently (docs/DESIGN.md 6.5).

Every ``claim`` call below is scoped to ``event_types=("thought.captured",)`` -
the type this file's own ``enqueue`` helper defaults to - rather than left
unfiltered. This suite shares one database across every integration test file
in the run, and nothing in a plain ``pytest`` invocation ever drains
``khoj.sync_requested`` events (only the dedicated Khoj-sync tests do, and
only when a real Khoj is reachable): every other file's
``PostgresOrganizeWriter.write`` calls leave a growing, permanently-pending
backlog of that type for the rest of the session. An unfiltered
``claim(limit=N)`` here would silently start missing its own just-enqueued
event once that backlog exceeds ``N`` due, earlier-queued events - exactly
what broke this file before this scoping was added.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_infrastructure.db.outbox import (
    BASE_BACKOFF,
    MAX_BACKOFF,
    PostgresOutbox,
    backoff_for,
)
from tc_infrastructure.db.tables import outbox_events

pytestmark = pytest.mark.integration


async def enqueue(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    *,
    aggregate_id: str,
    event_type: str = "thought.captured",
    available_in: dt.timedelta = dt.timedelta(0),
    max_attempts: int = 10,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(outbox_events).values(
                id=event_id,
                workspace_id=workspace_id,
                event_type=event_type,
                aggregate_id=aggregate_id,
                payload={"thought_id": 1},
                available_at=sa.func.now() + available_in,
                max_attempts=max_attempts,
            )
        )
    return event_id


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_backoff_grows_and_is_capped() -> None:
    assert backoff_for(0) == BASE_BACKOFF
    assert backoff_for(1) == BASE_BACKOFF
    assert backoff_for(2) == BASE_BACKOFF * 2
    assert backoff_for(3) == BASE_BACKOFF * 4
    assert backoff_for(1000) == MAX_BACKOFF, "a long outage must not schedule into the far future"


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


async def test_claim_returns_due_events(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = seeded_identity
    aggregate = f"agg-{uuid.uuid4()}"
    event_id = await enqueue(app_session_factory, workspace_id, aggregate_id=aggregate)

    outbox = PostgresOutbox(app_session_factory, lease_owner="test")
    claimed = await outbox.claim(limit=50, event_types=("thought.captured",))

    assert event_id in {event.id for event in claimed}


async def test_claim_skips_events_that_are_not_due_yet(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The acknowledgement grace window depends on this."""
    workspace_id, _ = seeded_identity
    aggregate = f"agg-{uuid.uuid4()}"
    event_id = await enqueue(
        app_session_factory,
        workspace_id,
        aggregate_id=aggregate,
        available_in=dt.timedelta(hours=1),
    )

    outbox = PostgresOutbox(app_session_factory, lease_owner="test")
    claimed = await outbox.claim(limit=50, event_types=("thought.captured",))

    assert event_id not in {event.id for event in claimed}


async def test_a_leased_event_is_not_claimed_twice(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Two workers must take disjoint sets, not the same event."""
    workspace_id, _ = seeded_identity
    aggregate = f"agg-{uuid.uuid4()}"
    event_id = await enqueue(app_session_factory, workspace_id, aggregate_id=aggregate)

    first = PostgresOutbox(app_session_factory, lease_owner="worker-a")
    second = PostgresOutbox(app_session_factory, lease_owner="worker-b")

    claimed_by_first = {
        event.id for event in await first.claim(limit=50, event_types=("thought.captured",))
    }
    claimed_by_second = {
        event.id for event in await second.claim(limit=50, event_types=("thought.captured",))
    }

    assert event_id in claimed_by_first
    assert event_id not in claimed_by_second


async def test_claiming_increments_attempts(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A worker that dies mid-delivery must still consume its budget."""
    workspace_id, _ = seeded_identity
    aggregate = f"agg-{uuid.uuid4()}"
    event_id = await enqueue(app_session_factory, workspace_id, aggregate_id=aggregate)

    outbox = PostgresOutbox(app_session_factory, lease_owner="test")
    claimed = [
        e
        for e in await outbox.claim(limit=50, event_types=("thought.captured",))
        if e.id == event_id
    ]

    assert claimed[0].attempts == 1


async def test_claim_restricted_to_event_types_ignores_other_types(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A digest consumer must never lease a thought.captured acknowledgement."""
    workspace_id, _ = seeded_identity
    captured_id = await enqueue(
        app_session_factory,
        workspace_id,
        aggregate_id=f"agg-{uuid.uuid4()}",
        event_type="thought.captured",
    )
    digest_id = await enqueue(
        app_session_factory,
        workspace_id,
        aggregate_id=f"agg-{uuid.uuid4()}",
        event_type="digest.ready",
    )

    outbox = PostgresOutbox(app_session_factory, lease_owner="test")
    claimed = {event.id for event in await outbox.claim(limit=50, event_types=("digest.ready",))}

    assert digest_id in claimed
    assert captured_id not in claimed


async def test_an_exhausted_event_is_no_longer_claimed(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A permanently poisonous event must not be retried forever."""
    workspace_id, _ = seeded_identity
    aggregate = f"agg-{uuid.uuid4()}"
    event_id = await enqueue(
        app_session_factory, workspace_id, aggregate_id=aggregate, max_attempts=1
    )

    outbox = PostgresOutbox(app_session_factory, lease_owner="test")
    first = {e.id for e in await outbox.claim(limit=50, event_types=("thought.captured",))}
    await outbox.mark_failed(event_id, attempts=1, error="synthetic")
    second = {e.id for e in await outbox.claim(limit=50, event_types=("thought.captured",))}

    assert event_id in first
    assert event_id not in second


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


async def test_delivered_events_are_not_claimed_again(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = seeded_identity
    aggregate = f"agg-{uuid.uuid4()}"
    event_id = await enqueue(app_session_factory, workspace_id, aggregate_id=aggregate)

    outbox = PostgresOutbox(app_session_factory, lease_owner="test")
    await outbox.claim(limit=50, event_types=("thought.captured",))
    await outbox.mark_delivered(event_id)

    async with app_session_factory() as session:
        delivered_at = await session.scalar(
            sa.select(outbox_events.c.delivered_at).where(outbox_events.c.id == event_id)
        )
    assert delivered_at is not None
    assert event_id not in {
        e.id for e in await outbox.claim(limit=50, event_types=("thought.captured",))
    }


async def test_failure_reschedules_with_backoff_and_records_the_error(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = seeded_identity
    aggregate = f"agg-{uuid.uuid4()}"
    event_id = await enqueue(app_session_factory, workspace_id, aggregate_id=aggregate)

    outbox = PostgresOutbox(app_session_factory, lease_owner="test")
    await outbox.claim(limit=50, event_types=("thought.captured",))
    await outbox.mark_failed(event_id, attempts=3, error="HTTPStatusError")

    async with app_session_factory() as session:
        row = (
            await session.execute(
                sa.select(
                    outbox_events.c.last_error,
                    outbox_events.c.leased_until,
                    outbox_events.c.available_at,
                ).where(outbox_events.c.id == event_id)
            )
        ).one()

    assert row.last_error == "HTTPStatusError"
    assert row.leased_until is None, "a failed event must release its lease"
    assert event_id not in {
        e.id for e in await outbox.claim(limit=50, event_types=("thought.captured",))
    }


async def test_find_pending_locates_an_undelivered_acknowledgement(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The bot's fast path uses this to settle the event it just satisfied."""
    workspace_id, _ = seeded_identity
    aggregate = f"agg-{uuid.uuid4()}"
    event_id = await enqueue(app_session_factory, workspace_id, aggregate_id=aggregate)

    outbox = PostgresOutbox(app_session_factory, lease_owner="test")
    found = await outbox.find_pending("thought.captured", aggregate)
    assert found == event_id

    await outbox.mark_delivered(event_id)
    assert await outbox.find_pending("thought.captured", aggregate) is None

"""``PostgresEmbeddingSyncOutbox`` - the ``embedding.sync_requested``-only view
over the generic outbox (docs/DESIGN.md 8.4, ADR-0010). The generic
claim/lease/backoff mechanics are covered by ``tests/integration/test_outbox.py``;
this file covers the payload parsing into ``PendingEmbeddingSync`` and the
event-type scoping specific to this consumer - mirrors
``tests/integration/test_khoj_sync_outbox.py`` exactly.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_infrastructure.db.embedding_sync_enqueue import enqueue_embedding_sync
from tc_infrastructure.db.embedding_sync_outbox import PostgresEmbeddingSyncOutbox
from tc_infrastructure.db.khoj_sync_enqueue import enqueue_khoj_sync
from tc_infrastructure.db.tables import outbox_events

pytestmark = pytest.mark.integration


async def _enqueue(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    *,
    event_type: str = "embedding.sync_requested",
    payload: dict[str, object] | None = None,
) -> uuid.UUID:
    document_id = uuid.uuid4()
    event_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(outbox_events).values(
                id=event_id,
                workspace_id=workspace_id,
                event_type=event_type,
                aggregate_id=str(document_id),
                payload=payload if payload is not None else {"document_id": str(document_id)},
            )
        )
    return event_id


async def test_claim_returns_a_due_embedding_sync_event_as_pending_embedding_sync(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = seeded_identity
    document_id = uuid.uuid4()
    async with app_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(outbox_events).values(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                event_type="embedding.sync_requested",
                aggregate_id=str(document_id),
                payload={"document_id": str(document_id)},
            )
        )

    outbox = PostgresEmbeddingSyncOutbox(app_session_factory, lease_owner="test")
    claimed = await outbox.claim(limit=50)

    matching = [event for event in claimed if event.document_id == document_id]
    assert len(matching) == 1
    assert matching[0].workspace_id == workspace_id
    assert matching[0].attempts == 1


async def test_claim_never_leases_a_khoj_sync_requested_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The embedding-sync consumer must not steal a Khoj-sync-only event
    it has no logic to deliver, and vice versa (covered below)."""
    workspace_id, _ = seeded_identity
    document_id = uuid.uuid4()
    async with app_session_factory() as session, session.begin():
        await enqueue_khoj_sync(session, workspace_id=workspace_id, document_id=document_id)

    outbox = PostgresEmbeddingSyncOutbox(app_session_factory, lease_owner="test")
    claimed = await outbox.claim(limit=50)

    assert document_id not in {event.document_id for event in claimed}


async def test_khoj_sync_outbox_never_leases_an_embedding_sync_requested_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    from tc_infrastructure.db.khoj_sync_outbox import PostgresKhojSyncOutbox

    workspace_id, _ = seeded_identity
    document_id = uuid.uuid4()
    async with app_session_factory() as session, session.begin():
        await enqueue_embedding_sync(session, workspace_id=workspace_id, document_id=document_id)

    outbox = PostgresKhojSyncOutbox(app_session_factory, lease_owner="test")
    claimed = await outbox.claim(limit=50)

    assert document_id not in {event.document_id for event in claimed}


async def test_a_malformed_payload_is_failed_immediately_and_not_returned(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = seeded_identity
    bad_event_id = await _enqueue(app_session_factory, workspace_id, payload={"oops": "no id"})

    outbox = PostgresEmbeddingSyncOutbox(app_session_factory, lease_owner="test")
    claimed = await outbox.claim(limit=50)

    assert bad_event_id not in {event.event_id for event in claimed}
    async with app_session_factory() as session:
        row = (
            await session.execute(
                sa.select(outbox_events.c.last_error).where(outbox_events.c.id == bad_event_id)
            )
        ).one()
    assert row.last_error is not None
    assert "malformed_payload" in row.last_error


async def test_mark_delivered_then_claim_does_not_return_it_again(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = seeded_identity
    event_id = await _enqueue(app_session_factory, workspace_id)

    outbox = PostgresEmbeddingSyncOutbox(app_session_factory, lease_owner="test")
    await outbox.claim(limit=50)
    await outbox.mark_delivered(event_id)

    claimed_again = await outbox.claim(limit=50)
    assert event_id not in {event.event_id for event in claimed_again}


async def test_mark_failed_reschedules_with_backoff_and_releases_the_lease(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = seeded_identity
    event_id = await _enqueue(app_session_factory, workspace_id)

    outbox = PostgresEmbeddingSyncOutbox(app_session_factory, lease_owner="test")
    await outbox.claim(limit=50)
    await outbox.mark_failed(event_id, attempts=3, error="EmbeddingUnavailableError")

    async with app_session_factory() as session:
        row = (
            await session.execute(
                sa.select(
                    outbox_events.c.last_error,
                    outbox_events.c.leased_until,
                ).where(outbox_events.c.id == event_id)
            )
        ).one()

    assert row.last_error == "EmbeddingUnavailableError"
    assert row.leased_until is None, "a failed event must release its lease"
    assert event_id not in {event.event_id for event in await outbox.claim(limit=50)}

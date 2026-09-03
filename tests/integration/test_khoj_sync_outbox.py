"""``PostgresKhojSyncOutbox`` - the ``khoj.sync_requested``-only view over the
generic outbox (docs/DESIGN.md 6.5). The generic claim/lease/backoff mechanics
are covered by ``tests/integration/test_outbox.py``; this file covers the
payload parsing into ``PendingKhojSync`` and the event-type scoping that are
specific to this consumer.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_infrastructure.db.khoj_sync_outbox import PostgresKhojSyncOutbox
from tc_infrastructure.db.tables import outbox_events

pytestmark = pytest.mark.integration


async def _enqueue(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    *,
    event_type: str = "khoj.sync_requested",
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


async def test_claim_returns_a_due_khoj_sync_event_as_pending_khoj_sync(
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
                event_type="khoj.sync_requested",
                aggregate_id=str(document_id),
                payload={"document_id": str(document_id)},
            )
        )

    outbox = PostgresKhojSyncOutbox(app_session_factory, lease_owner="test")
    claimed = await outbox.claim(limit=50)

    matching = [event for event in claimed if event.document_id == document_id]
    assert len(matching) == 1
    assert matching[0].workspace_id == workspace_id
    assert matching[0].attempts == 1


async def test_claim_never_leases_a_digest_ready_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = seeded_identity
    digest_event_id = await _enqueue(
        app_session_factory, workspace_id, event_type="digest.ready", payload={"run_id": "x"}
    )

    outbox = PostgresKhojSyncOutbox(app_session_factory, lease_owner="test")
    claimed = await outbox.claim(limit=50)

    assert digest_event_id not in {event.event_id for event in claimed}


async def test_a_malformed_payload_is_failed_immediately_and_not_returned(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = seeded_identity
    bad_event_id = await _enqueue(app_session_factory, workspace_id, payload={"oops": "no id"})

    outbox = PostgresKhojSyncOutbox(app_session_factory, lease_owner="test")
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

    outbox = PostgresKhojSyncOutbox(app_session_factory, lease_owner="test")
    await outbox.claim(limit=50)
    await outbox.mark_delivered(event_id)

    claimed_again = await outbox.claim(limit=50)
    assert event_id not in {event.event_id for event in claimed_again}

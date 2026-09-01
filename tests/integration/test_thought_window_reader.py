"""``list_between`` and ``first_capture_at`` (docs/DESIGN.md 7.2, 4.2)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_infrastructure.db.tables import thoughts
from tc_infrastructure.db.thought_reader import PostgresThoughtReader

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace(seeded_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(seeded_identity[0])


@pytest.fixture
def user_id(seeded_identity: tuple[uuid.UUID, uuid.UUID]) -> uuid.UUID:
    return seeded_identity[1]


async def _thought(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: WorkspaceId,
    user_id: uuid.UUID,
    *,
    source_message_id: str,
    created_at: dt.datetime,
    body: str = "synthetic",
) -> int:
    async with factory() as session, session.begin():
        thought_id = await session.scalar(
            sa.insert(thoughts)
            .values(
                workspace_id=workspace_id,
                author_user_id=user_id,
                source="api",
                source_message_id=source_message_id,
                body=body,
                client_created_at=created_at,
                client_timezone="Asia/Bangkok",
                client_local_date=created_at.date(),
                client_local_time=created_at.time(),
                content_language="en",
            )
            .returning(thoughts.c.id)
        )
    assert thought_id is not None
    return int(thought_id)


async def test_list_between_is_half_open_start_closed_end(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique_message_id: str,
) -> None:
    start = dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC)
    end = dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC)

    on_start = await _thought(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique_message_id}-start",
        created_at=start,
    )
    inside = await _thought(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique_message_id}-inside",
        created_at=start + dt.timedelta(hours=1),
    )
    on_end = await _thought(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique_message_id}-end",
        created_at=end,
    )
    after = await _thought(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique_message_id}-after",
        created_at=end + dt.timedelta(hours=1),
    )

    reader = PostgresThoughtReader(app_session_factory)
    result = await reader.list_between(workspace, start, end)
    ids = [int(t.id) for t in result]

    assert on_start not in ids
    assert inside in ids
    assert on_end in ids
    assert after not in ids


async def test_list_between_is_ordered_by_time_then_id(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique_message_id: str,
) -> None:
    start = dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC)
    end = dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC)
    second = await _thought(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique_message_id}-b",
        created_at=start + dt.timedelta(hours=2),
    )
    first = await _thought(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique_message_id}-a",
        created_at=start + dt.timedelta(hours=1),
    )

    reader = PostgresThoughtReader(app_session_factory)
    result = await reader.list_between(workspace, start, end)
    ordered = [int(t.id) for t in result if int(t.id) in (first, second)]

    assert ordered == [first, second]


async def test_first_capture_at_is_the_earliest_thought(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique_message_id: str,
) -> None:
    earliest = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    await _thought(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique_message_id}-early",
        created_at=earliest,
    )
    await _thought(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique_message_id}-late",
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )

    reader = PostgresThoughtReader(app_session_factory)
    result = await reader.first_capture_at(workspace)

    assert result is not None
    assert result <= earliest


async def test_first_capture_at_is_none_for_an_untouched_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reader = PostgresThoughtReader(app_session_factory)
    result = await reader.first_capture_at(WorkspaceId(uuid.uuid4()))
    assert result is None

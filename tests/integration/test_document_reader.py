"""``PostgresDocumentReader`` against real organize-writer output."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.organize import DocumentWrite, OrganizeWriteRequest
from tc_infrastructure.db.document_reader import PostgresDocumentReader
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.tables import thoughts

pytestmark = pytest.mark.integration

BODY = "## Summary\n\nsomething"


@pytest.fixture
def workspace(fresh_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(fresh_identity[0])


@pytest.fixture
def user_id(fresh_identity: tuple[uuid.UUID, uuid.UUID]) -> uuid.UUID:
    return fresh_identity[1]


@pytest.fixture
def unique(unique_message_id: str) -> str:
    return unique_message_id.removeprefix("test-")[:8]


async def _thought_id(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    *,
    source_message_id: str,
) -> ThoughtId:
    now = dt.datetime.now(dt.UTC)
    async with app_session_factory() as session, session.begin():
        thought_id = await session.scalar(
            sa.insert(thoughts)
            .values(
                workspace_id=workspace,
                author_user_id=user_id,
                source="api",
                source_message_id=source_message_id,
                body="synthetic",
                client_created_at=now,
                client_timezone="Asia/Bangkok",
                client_local_date=now.date(),
                client_local_time=now.time(),
                content_language="en",
            )
            .returning(thoughts.c.id)
        )
    assert thought_id is not None
    return ThoughtId(thought_id)


async def _write_document(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    thought_id: ThoughtId,
    *,
    stable_key: str,
    kind: str,
    title: str,
) -> uuid.UUID:
    ledger = PostgresRunLedger(app_session_factory)
    run_id = await ledger.start(
        workspace,
        window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    )
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=stable_key,
                kind=kind,
                title=title,
                body_markdown=BODY,
                source_thought_ids=(thought_id,),
                mentioned_entities=(),
                change_summary="created",
            ),
        ),
        context_selections=(),
        unorganized_thought_ids=(),
    )
    writer = PostgresOrganizeWriter(app_session_factory)
    result = await writer.write(workspace_id=workspace, run_id=run_id, request=request)
    return result.document_ids[stable_key]


async def test_list_documents_returns_a_written_document(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
    )

    reader = PostgresDocumentReader(app_session_factory)
    items = await reader.list_documents(workspace)

    assert any(item.id == document_id for item in items)


async def test_list_documents_filters_by_kind(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    project_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
    )
    digest_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"daily_digest:{unique}",
        kind="daily_digest",
        title="A digest",
    )

    reader = PostgresDocumentReader(app_session_factory)
    digests = await reader.list_documents(workspace, kind="daily_digest")

    ids = {item.id for item in digests}
    assert digest_id in ids
    assert project_id not in ids


async def test_get_document_returns_the_body_and_sources(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
    )

    reader = PostgresDocumentReader(app_session_factory)
    detail = await reader.get_document(workspace, document_id)

    assert detail is not None
    assert detail.body_markdown == BODY
    assert detail.source_thought_ids == (thought_id,)


async def test_get_document_returns_none_for_an_unknown_id(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> None:
    reader = PostgresDocumentReader(app_session_factory)
    assert await reader.get_document(workspace, uuid.uuid4()) is None


async def test_get_document_is_workspace_scoped(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
    )

    other_workspace = WorkspaceId(uuid.uuid4())
    reader = PostgresDocumentReader(app_session_factory)
    assert await reader.get_document(other_workspace, document_id) is None

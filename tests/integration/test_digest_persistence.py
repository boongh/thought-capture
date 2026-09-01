"""Digest-only outbox view and document read, against a real Postgres (docs/DESIGN.md 4.2)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.errors import DigestNotFound
from tc_domain.organize import DocumentWrite, OrganizeWriteRequest
from tc_infrastructure.db.digest_outbox import PostgresDigestOutbox
from tc_infrastructure.db.digest_reader import PostgresDigestReader
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.tables import thoughts

pytestmark = pytest.mark.integration

BODY = (
    "## Summary\n\nsomething\n\n## Current state\n\n-\n\n## Open threads\n\n-\n\n## Timeline\n\n- x"
)
WINDOW_START = dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC)
WINDOW_END = dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC)


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


async def _write_digest(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    thought_id: ThoughtId,
    *,
    unique: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Write a real digest through the organize writer. Returns (run_id, document_id, revision_id)."""
    stable_key = f"daily_digest:{unique}"
    ledger = PostgresRunLedger(app_session_factory)
    run_id = await ledger.start(workspace, window_start=WINDOW_START, window_end=WINDOW_END)
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=stable_key,
                kind="daily_digest",
                title="Daily digest",
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
    assert result.digest_document_id is not None
    return run_id, result.digest_document_id, result.revision_ids[stable_key]


async def test_claim_returns_a_parsed_pending_digest(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    run_id, document_id, revision_id = await _write_digest(
        app_session_factory, workspace, thought_id, unique=unique
    )

    outbox = PostgresDigestOutbox(app_session_factory, lease_owner="test")
    pending = await outbox.claim(limit=50)

    matches = [p for p in pending if p.document_id == document_id]
    assert len(matches) == 1
    assert matches[0].run_id == run_id
    assert matches[0].revision_id == revision_id
    assert matches[0].attempts == 1


async def test_claim_ignores_non_digest_documents(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """A project document write enqueues no outbox event; the digest view sees nothing for it."""
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    ledger = PostgresRunLedger(app_session_factory)
    run_id = await ledger.start(workspace, window_start=WINDOW_START, window_end=WINDOW_END)
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=f"project:no-digest-{unique}",
                kind="project",
                title="No digest",
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
    await writer.write(workspace_id=workspace, run_id=run_id, request=request)

    outbox = PostgresDigestOutbox(app_session_factory, lease_owner="test")
    pending = await outbox.claim(limit=50)

    assert all(p.run_id != run_id for p in pending)


async def test_mark_delivered_settles_the_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    _run_id, document_id, _revision_id = await _write_digest(
        app_session_factory, workspace, thought_id, unique=unique
    )

    outbox = PostgresDigestOutbox(app_session_factory, lease_owner="test")
    pending = await outbox.claim(limit=50)
    event = next(p for p in pending if p.document_id == document_id)
    await outbox.mark_delivered(event.event_id)

    second_claim = await outbox.claim(limit=50)
    assert event.event_id not in {p.event_id for p in second_claim}


async def test_digest_reader_returns_the_written_content(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    _run_id, document_id, revision_id = await _write_digest(
        app_session_factory, workspace, thought_id, unique=unique
    )

    reader = PostgresDigestReader(app_session_factory)
    content = await reader.get(document_id, revision_id)

    assert content.title == "Daily digest"
    assert content.body_markdown == BODY
    assert content.window_start == WINDOW_START
    assert content.window_end == WINDOW_END


async def test_digest_reader_raises_for_an_unknown_revision(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reader = PostgresDigestReader(app_session_factory)
    with pytest.raises(DigestNotFound):
        await reader.get(uuid.uuid4(), uuid.uuid4())

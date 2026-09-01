"""Atomic multi-table write of one organize run (docs/DESIGN.md 7.2 step 9)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.entities import EntityType
from tc_domain.organize import (
    ContextSelectionWrite,
    DocumentWrite,
    EntityMentionWrite,
    OrganizeWriteRequest,
)
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.tables import (
    document_revisions,
    documents,
    entities,
    outbox_events,
    revision_sources,
    run_context_selections,
    thoughts,
)

pytestmark = pytest.mark.integration

BODY = (
    "## Summary\n\nsomething\n\n## Current state\n\n-\n\n## Open threads\n\n-\n\n## Timeline\n\n- x"
)


@pytest.fixture
def workspace(fresh_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(fresh_identity[0])


@pytest.fixture
def user_id(fresh_identity: tuple[uuid.UUID, uuid.UUID]) -> uuid.UUID:
    return fresh_identity[1]


@pytest.fixture
def unique(unique_message_id: str) -> str:
    return unique_message_id.removeprefix("test-")[:8]


async def _run_id(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> uuid.UUID:
    ledger = PostgresRunLedger(app_session_factory)
    return await ledger.start(
        workspace,
        window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    )


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


async def test_writing_a_new_document_creates_it_with_revision_one(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    run_id = await _run_id(app_session_factory, workspace)
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    stable_key = f"project:aurora-{unique}"
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=stable_key,
                kind="project",
                title="Aurora",
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

    async with app_session_factory() as session:
        doc = (
            await session.execute(
                sa.select(documents).where(documents.c.id == result.document_ids[stable_key])
            )
        ).one()
        revision = (
            await session.execute(
                sa.select(document_revisions).where(
                    document_revisions.c.id == result.revision_ids[stable_key]
                )
            )
        ).one()
        sources = (
            await session.execute(
                sa.select(revision_sources).where(revision_sources.c.revision_id == revision.id)
            )
        ).all()

    assert doc.current_revision_id == revision.id
    assert revision.revision_number == 1
    assert revision.parent_revision_id is None
    assert revision.change_kind == "create"
    assert [row.thought_id for row in sources] == [thought_id]


async def test_a_second_write_to_the_same_document_chains_the_revision(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    run_id = await _run_id(app_session_factory, workspace)
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    stable_key = f"project:chained-{unique}"
    writer = PostgresOrganizeWriter(app_session_factory)

    def request(summary: str) -> OrganizeWriteRequest:
        return OrganizeWriteRequest(
            documents=(
                DocumentWrite(
                    stable_key=stable_key,
                    kind="project",
                    title="Chained",
                    body_markdown=BODY,
                    source_thought_ids=(thought_id,),
                    mentioned_entities=(),
                    change_summary=summary,
                ),
            ),
            context_selections=(),
            unorganized_thought_ids=(),
        )

    first = await writer.write(workspace_id=workspace, run_id=run_id, request=request("first"))
    second_run = await _run_id(app_session_factory, workspace)
    second = await writer.write(
        workspace_id=workspace, run_id=second_run, request=request("second")
    )

    assert first.document_ids[stable_key] == second.document_ids[stable_key]
    async with app_session_factory() as session:
        revision = (
            await session.execute(
                sa.select(document_revisions).where(
                    document_revisions.c.id == second.revision_ids[stable_key]
                )
            )
        ).one()
        doc = (
            await session.execute(
                sa.select(documents).where(documents.c.id == second.document_ids[stable_key])
            )
        ).one()

    assert revision.revision_number == 2
    assert revision.parent_revision_id == first.revision_ids[stable_key]
    assert revision.change_kind == "organize"
    assert doc.current_revision_id == revision.id


async def test_mentioned_entities_are_resolved_and_linked_to_the_revision(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    run_id = await _run_id(app_session_factory, workspace)
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=f"daily_digest:{unique}",
                kind="daily_digest",
                title="Digest",
                body_markdown=BODY,
                source_thought_ids=(thought_id,),
                mentioned_entities=(
                    EntityMentionWrite(
                        entity_type=EntityType.PERSON,
                        canonical_name=f"Jane Rivera {unique}",
                        surface_form=f"Jane Rivera {unique}",
                        confidence=0.9,
                    ),
                ),
                change_summary="created",
            ),
        ),
        context_selections=(),
        unorganized_thought_ids=(),
    )

    writer = PostgresOrganizeWriter(app_session_factory)
    await writer.write(workspace_id=workspace, run_id=run_id, request=request)

    async with app_session_factory() as session:
        entity = (
            await session.execute(
                sa.select(entities).where(
                    entities.c.workspace_id == workspace,
                    entities.c.canonical_name == f"Jane Rivera {unique}",
                )
            )
        ).one()
    assert entity.entity_type == "person"


async def test_context_selections_are_written_only_for_documents_that_exist(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    run_id = await _run_id(app_session_factory, workspace)
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    existing_key = f"project:existing-{unique}"
    missing_key = f"project:phantom-{unique}"
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=existing_key,
                kind="project",
                title="Existing",
                body_markdown=BODY,
                source_thought_ids=(thought_id,),
                mentioned_entities=(),
                change_summary="created",
            ),
        ),
        context_selections=(
            ContextSelectionWrite(
                stable_key=existing_key,
                signals=("alias",),
                inclusion="full",
                referenced_in_output=True,
            ),
            ContextSelectionWrite(
                stable_key=missing_key,
                signals=("alias",),
                inclusion="full",
                referenced_in_output=False,
            ),
        ),
        unorganized_thought_ids=(),
    )

    writer = PostgresOrganizeWriter(app_session_factory)
    result = await writer.write(workspace_id=workspace, run_id=run_id, request=request)

    async with app_session_factory() as session:
        rows = (
            await session.execute(
                sa.select(run_context_selections).where(run_context_selections.c.run_id == run_id)
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].document_id == result.document_ids[existing_key]
    assert rows[0].referenced_in_output is True


async def test_a_daily_digest_document_enqueues_the_digest_outbox_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    run_id = await _run_id(app_session_factory, workspace)
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=f"daily_digest:{unique}",
                kind="daily_digest",
                title="Digest",
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

    async with app_session_factory() as session:
        event = (
            await session.execute(
                sa.select(outbox_events).where(
                    outbox_events.c.event_type == "digest.ready",
                    outbox_events.c.aggregate_id == str(run_id),
                )
            )
        ).one()
    assert event.payload["document_id"] == str(result.digest_document_id)


async def test_a_non_digest_only_write_enqueues_no_outbox_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    run_id = await _run_id(app_session_factory, workspace)
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
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
    result = await writer.write(workspace_id=workspace, run_id=run_id, request=request)

    assert result.digest_document_id is None
    async with app_session_factory() as session:
        events = (
            await session.execute(
                sa.select(outbox_events).where(outbox_events.c.aggregate_id == str(run_id))
            )
        ).all()
    assert events == []

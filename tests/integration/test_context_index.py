"""Tier 1 context index against real PostgreSQL (docs/DESIGN.md 7.3.1)."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.entities import EntityType
from tc_infrastructure.db.context_index import PostgresContextIndex
from tc_infrastructure.db.entity_repository import PostgresEntityRepository
from tc_infrastructure.db.tables import document_revisions, documents, runs

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace(seeded_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(seeded_identity[0])


@pytest.fixture
def unique(unique_message_id: str) -> str:
    return unique_message_id.removeprefix("test-")[:8]


async def _run_row(
    factory: async_sessionmaker[AsyncSession], workspace_id: WorkspaceId, *, created_at: dt.datetime
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=run_id,
                workspace_id=workspace_id,
                kind="organize",
                status="succeeded",
                created_at=created_at,
            )
        )
    return run_id


async def _document_with_revision(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: WorkspaceId,
    run_id: uuid.UUID,
    *,
    kind: str,
    stable_key: str,
    body: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(documents).values(
                id=document_id,
                workspace_id=workspace_id,
                kind=kind,
                stable_key=stable_key,
                title=stable_key,
            )
        )
        await session.execute(
            sa.insert(document_revisions).values(
                id=revision_id,
                workspace_id=workspace_id,
                document_id=document_id,
                run_id=run_id,
                revision_number=1,
                body_markdown=body,
                body_sha256=sha,
                change_summary="synthetic",
                change_kind="create",
            )
        )
        await session.execute(
            sa.update(documents)
            .where(documents.c.id == document_id)
            .values(current_revision_id=revision_id)
        )
    return document_id, revision_id


async def test_the_index_includes_an_entity_with_no_document_yet(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId, unique: str
) -> None:
    """A brand-new entity is mentioned in this run's digest before it has its own document."""
    run_id = await _run_row(app_session_factory, workspace, created_at=dt.datetime.now(dt.UTC))
    _, digest_revision_id = await _document_with_revision(
        app_session_factory,
        workspace,
        run_id,
        kind="daily_digest",
        stable_key=f"digest:{unique}",
        body="## Summary\n\nsynthetic digest\n",
    )
    repo = PostgresEntityRepository()
    async with app_session_factory() as session, session.begin():
        resolved = await repo.resolve_mention(
            session,
            workspace_id=workspace,
            run_id=run_id,
            entity_type=EntityType.TOPIC,
            canonical_name=f"Undocumented Topic {unique}",
            surface_form=f"Undocumented Topic {unique}",
            confidence=0.9,
            revision_id=digest_revision_id,
        )

    index = await PostgresContextIndex(app_session_factory).tier1_index(workspace)
    row = next(r for r in index if r.stable_key == resolved.stable_key)
    assert row.summary == ""
    assert row.open_thread_count == 0


async def test_the_index_extracts_the_summary_section_of_the_current_document(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId, unique: str
) -> None:
    run_id = await _run_row(app_session_factory, workspace, created_at=dt.datetime.now(dt.UTC))
    stable_key = f"project:documented-project-{unique}"
    body = (
        "## Summary\n\nShipping the launch.\n\n"
        "## Current state\n\nIn progress.\n\n"
        "## Open threads\n\nnone\n\n"
        "## Timeline\n\n- did a thing\n"
    )
    await _document_with_revision(
        app_session_factory, workspace, run_id, kind="project", stable_key=stable_key, body=body
    )
    async with app_session_factory() as session, session.begin():
        await session.execute(
            sa.text("""
                INSERT INTO entities (id, workspace_id, entity_type, canonical_name, normalized_name)
                VALUES (:id, :workspace_id, 'project', :name, :normalized)
            """),
            {
                "id": uuid.uuid4(),
                "workspace_id": workspace,
                "name": f"Documented Project {unique}",
                "normalized": f"documented project {unique}".lower(),
            },
        )

    index = await PostgresContextIndex(app_session_factory).tier1_index(workspace)
    row = next(r for r in index if r.stable_key == stable_key)
    assert row.summary == "Shipping the launch."


async def test_open_thread_count_reflects_current_todo_documents(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId, unique: str
) -> None:
    run_id = await _run_row(app_session_factory, workspace, created_at=dt.datetime.now(dt.UTC))
    _, revision_id = await _document_with_revision(
        app_session_factory,
        workspace,
        run_id,
        kind="todo",
        stable_key=f"todo:{unique}",
        body="## Summary\n\nBuy supplies\n\n## Current state\n\nopen\n\n## Open threads\n\n- buy\n\n## Timeline\n\n- created\n",
    )
    repo = PostgresEntityRepository()
    async with app_session_factory() as session, session.begin():
        resolved = await repo.resolve_mention(
            session,
            workspace_id=workspace,
            run_id=run_id,
            entity_type=EntityType.TOPIC,
            canonical_name=f"Supply Run {unique}",
            surface_form=f"Supply Run {unique}",
            confidence=0.9,
            revision_id=revision_id,
        )

    index = await PostgresContextIndex(app_session_factory).tier1_index(workspace)
    row = next(r for r in index if r.stable_key == resolved.stable_key)
    assert row.open_thread_count == 1


async def test_last_mentioned_at_is_the_most_recent_run(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId, unique: str
) -> None:
    early = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    late = dt.datetime(2026, 8, 30, tzinfo=dt.UTC)
    early_run = await _run_row(app_session_factory, workspace, created_at=early)
    late_run = await _run_row(app_session_factory, workspace, created_at=late)

    _, early_revision = await _document_with_revision(
        app_session_factory,
        workspace,
        early_run,
        kind="daily_digest",
        stable_key=f"digest:early-{unique}",
        body="## Summary\n\nearly\n",
    )
    _, late_revision = await _document_with_revision(
        app_session_factory,
        workspace,
        late_run,
        kind="daily_digest",
        stable_key=f"digest:late-{unique}",
        body="## Summary\n\nlate\n",
    )

    repo = PostgresEntityRepository()
    async with app_session_factory() as session, session.begin():
        resolved = await repo.resolve_mention(
            session,
            workspace_id=workspace,
            run_id=early_run,
            entity_type=EntityType.TOPIC,
            canonical_name=f"Recurring Topic {unique}",
            surface_form=f"Recurring Topic {unique}",
            confidence=0.9,
            revision_id=early_revision,
        )
    async with app_session_factory() as session, session.begin():
        await repo.resolve_mention(
            session,
            workspace_id=workspace,
            run_id=late_run,
            entity_type=EntityType.TOPIC,
            canonical_name=f"Recurring Topic {unique}",
            surface_form=f"Recurring Topic {unique}",
            confidence=0.9,
            revision_id=late_revision,
        )

    index = await PostgresContextIndex(app_session_factory).tier1_index(workspace)
    row = next(r for r in index if r.stable_key == resolved.stable_key)
    assert row.last_mentioned_at == late

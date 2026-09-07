"""``PostgresDocumentReader.list_all_current_for_export`` and
``PostgresExactSearch.hydrate`` (docs/adr/0010) against real organize-writer
output."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.organize import DocumentWrite, OrganizeWriteRequest, RunOutcome
from tc_domain.search import SearchQuery
from tc_infrastructure.db.document_reader import PostgresDocumentReader
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.search_reader import PostgresExactSearch
from tc_infrastructure.db.tables import entities, entity_mentions, thoughts

pytestmark = pytest.mark.integration


def _outcome() -> RunOutcome:
    return RunOutcome(
        model_provider="offline",
        model_id="offline-model",
        prompt_version="organize-v1",
        input_tokens=0,
        output_tokens=0,
        context_recall=None,
        context_degraded=False,
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
    body_markdown: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Returns ``(document_id, revision_id, run_id)``."""
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
                body_markdown=body_markdown,
                source_thought_ids=(thought_id,),
                mentioned_entities=(),
                change_summary="created",
            ),
        ),
        context_selections=(),
        unorganized_thought_ids=(),
    )
    writer = PostgresOrganizeWriter(app_session_factory)
    result = await writer.write(
        workspace_id=workspace, run_id=run_id, request=request, outcome=_outcome()
    )
    document_id = result.document_ids[stable_key]
    async with app_session_factory() as session:
        revision_id = await session.scalar(
            sa.text("SELECT current_revision_id FROM documents WHERE id = :id").bindparams(
                id=document_id
            )
        )
    assert revision_id is not None
    return document_id, revision_id, run_id


async def _second_workspace(
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[WorkspaceId, uuid.UUID]:
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.text("INSERT INTO users (id, display_name) VALUES (:id, :name)"),
            {"id": user_id, "name": "synthetic other owner"},
        )
        await session.execute(
            sa.text(
                "INSERT INTO workspaces (id, name, mode, timezone)"
                " VALUES (:id, :name, 'personal', 'Asia/Bangkok')"
            ),
            {"id": workspace_id, "name": "synthetic other workspace"},
        )
        await session.execute(
            sa.text(
                "INSERT INTO workspace_memberships (workspace_id, user_id, role)"
                " VALUES (:workspace_id, :user_id, 'owner')"
            ),
            {"workspace_id": workspace_id, "user_id": user_id},
        )
    return WorkspaceId(workspace_id), user_id


async def test_list_all_current_for_export_includes_window_entities_and_sources(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, revision_id, run_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\n{unique} content",
    )
    entity_id = uuid.uuid4()
    async with app_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(entities).values(
                id=entity_id,
                workspace_id=workspace,
                entity_type="person",
                canonical_name=f"Person {unique}",
                normalized_name=f"person {unique}",
                created_at=dt.datetime.now(dt.UTC),
            )
        )
        await session.execute(
            sa.insert(entity_mentions).values(
                workspace_id=workspace,
                entity_id=entity_id,
                revision_id=revision_id,
                run_id=run_id,
                surface_form=f"Person {unique}",
                confidence=1.0,
            )
        )

    reader = PostgresDocumentReader(app_session_factory)
    records = await reader.list_all_current_for_export(workspace)

    record = next(r for r in records if r.id == document_id)
    assert record.revision_id == revision_id
    assert record.kind == "project"
    assert record.stable_key == f"project:{unique}"
    assert f"{unique} content" in record.body_markdown
    assert record.window_start == dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC)
    assert record.window_end == dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC)
    assert record.entities == (f"Person {unique}",)
    assert record.source_thought_ids == (thought_id,)


async def test_list_all_current_for_export_excludes_another_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    other_workspace, other_user_id = await _second_workspace(admin_session_factory)
    other_thought = await _thought_id(
        app_session_factory, other_workspace, other_user_id, source_message_id=f"{unique}-other"
    )
    other_document_id, _, _ = await _write_document(
        app_session_factory,
        other_workspace,
        other_thought,
        stable_key=f"project:{unique}-other",
        kind="project",
        title="Other workspace project",
        body_markdown=f"## Summary\n\n{unique} other workspace",
    )

    reader = PostgresDocumentReader(app_session_factory)
    records = await reader.list_all_current_for_export(workspace)

    assert all(r.id != other_document_id for r in records)


async def test_hydrate_returns_full_rows_for_requested_ids_only(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, revision_id, _ = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\n{unique} hydrate me",
    )
    unrelated_thought = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-2"
    )
    unrelated_document_id, _, _ = await _write_document(
        app_session_factory,
        workspace,
        unrelated_thought,
        stable_key=f"other:{unique}",
        kind="project",
        title="Not requested",
        body_markdown=f"## Summary\n\n{unique} not requested",
    )

    reader = PostgresExactSearch(app_session_factory)
    hydrated = await reader.hydrate(workspace, (document_id,), SearchQuery())

    assert set(hydrated) == {document_id}
    result = hydrated[document_id]
    assert result.revision_id == revision_id
    assert result.thought_ids == (thought_id,)
    assert result.channels == ()
    assert result.rank == 0.0
    assert unrelated_document_id not in hydrated


async def test_hydrate_ignores_ids_from_another_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    unique: str,
) -> None:
    other_workspace, other_user_id = await _second_workspace(admin_session_factory)
    other_thought = await _thought_id(
        app_session_factory, other_workspace, other_user_id, source_message_id=f"{unique}-other"
    )
    other_document_id, _, _ = await _write_document(
        app_session_factory,
        other_workspace,
        other_thought,
        stable_key=f"project:{unique}-other",
        kind="project",
        title="Other workspace project",
        body_markdown=f"## Summary\n\n{unique} other workspace",
    )

    reader = PostgresExactSearch(app_session_factory)
    hydrated = await reader.hydrate(workspace, (other_document_id,), SearchQuery())

    assert hydrated == {}


async def test_hydrate_is_a_no_op_for_an_empty_id_tuple(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> None:
    reader = PostgresExactSearch(app_session_factory)
    assert await reader.hydrate(workspace, (), SearchQuery()) == {}

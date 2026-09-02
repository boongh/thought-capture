"""``PostgresExactSearch`` against real organize-writer output (docs/DESIGN.md 9.1)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.organize import DocumentWrite, OrganizeWriteRequest, RunOutcome
from tc_domain.search import SearchQuery
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.search_reader import (
    PostgresExactSearch,
    _entity_names_for,
    _thought_ids_for,
)
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
    source: str = "api",
    client_created_at: dt.datetime | None = None,
) -> ThoughtId:
    now = client_created_at or dt.datetime.now(dt.UTC)
    async with app_session_factory() as session, session.begin():
        thought_id = await session.scalar(
            sa.insert(thoughts)
            .values(
                workspace_id=workspace,
                author_user_id=user_id,
                source=source,
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
) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns ``(document_id, revision_id)``."""
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
    return document_id, revision_id


async def _write_document_with_sources(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    thought_ids: tuple[ThoughtId, ...],
    *,
    stable_key: str,
    kind: str,
    title: str,
    body_markdown: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Like ``_write_document`` but for a document cited by more than one
    thought - needed to exercise a filter combination where no single cited
    thought satisfies every provenance predicate on its own."""
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
                source_thought_ids=thought_ids,
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
    return document_id, revision_id


async def _second_workspace(
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[WorkspaceId, uuid.UUID]:
    """A second, independent workspace/owner - mirrors the ``fresh_identity``
    fixture's own seeding, done inline so a single test can hold both
    workspace identities at once for a cross-workspace isolation check."""
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


async def test_q_matches_body_text_via_full_text_search(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, _ = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\nplanning the {unique} rocket launch",
    )

    reader = PostgresExactSearch(app_session_factory)
    page = await reader.search(workspace, SearchQuery(q="rocket"))

    assert any(item.document_id == document_id for item in page.items)
    page_miss = await reader.search(workspace, SearchQuery(q="submarine"))
    assert all(item.document_id != document_id for item in page_miss.items)


async def test_phrase_matches_an_exact_substring_across_a_word_boundary(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, _ = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\nthe {unique} launch window opens at dawn",
    )

    reader = PostgresExactSearch(app_session_factory)
    page = await reader.search(workspace, SearchQuery(phrase=f"{unique} launch window"))

    assert any(item.document_id == document_id for item in page.items)


async def test_kind_filters_results(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    project_id, _ = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\n{unique} shared marker text",
    )
    digest_id, _ = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"{unique}",
        kind="daily_digest",
        title="Digest",
        body_markdown=f"## Summary\n\n{unique} shared marker text",
    )

    reader = PostgresExactSearch(app_session_factory)
    page = await reader.search(workspace, SearchQuery(q=unique, kind="daily_digest"))

    result_ids = {item.document_id for item in page.items}
    assert digest_id in result_ids
    assert project_id not in result_ids


async def test_source_filters_by_the_citing_thoughts_source(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    discord_thought = await _thought_id(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique}-discord",
        source="discord",
    )
    document_id, _ = await _write_document(
        app_session_factory,
        workspace,
        discord_thought,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\n{unique} from discord",
    )

    reader = PostgresExactSearch(app_session_factory)
    hit = await reader.search(workspace, SearchQuery(q=unique, source="discord"))
    miss = await reader.search(workspace, SearchQuery(q=unique, source="api"))

    assert any(item.document_id == document_id for item in hit.items)
    assert all(item.document_id != document_id for item in miss.items)


async def test_date_range_filters_by_the_citing_thoughts_local_date(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    old_thought = await _thought_id(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique}-old",
        client_created_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
    )
    document_id, _ = await _write_document(
        app_session_factory,
        workspace,
        old_thought,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\n{unique} archived",
    )

    reader = PostgresExactSearch(app_session_factory)
    in_range = await reader.search(
        workspace,
        SearchQuery(q=unique, date_from=dt.date(2019, 1, 1), date_to=dt.date(2021, 1, 1)),
    )
    out_of_range = await reader.search(
        workspace, SearchQuery(q=unique, date_from=dt.date(2025, 1, 1))
    )

    assert any(item.document_id == document_id for item in in_range.items)
    assert all(item.document_id != document_id for item in out_of_range.items)


async def test_entity_id_filters_to_revisions_that_mention_it(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, revision_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\n{unique} mentions someone",
    )
    other_document_id, _ = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"other:{unique}",
        kind="project",
        title="Other project",
        body_markdown=f"## Summary\n\n{unique} mentions nobody tracked",
    )

    entity_id = uuid.uuid4()
    async with app_session_factory() as session:
        run_id = await session.scalar(
            sa.text("SELECT run_id FROM document_revisions WHERE id = :rid").bindparams(
                rid=revision_id
            )
        )
    assert run_id is not None

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

    reader = PostgresExactSearch(app_session_factory)
    page = await reader.search(workspace, SearchQuery(entity_id=entity_id))

    result_ids = {item.document_id for item in page.items}
    assert document_id in result_ids
    assert other_document_id not in result_ids


async def test_only_the_current_revision_is_searched(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """Mirrors Khoj's own scope (docs/DESIGN.md 8.3): a superseded fact must
    not still be findable once a newer revision replaces it."""
    first_thought = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-1"
    )
    document_id, _ = await _write_document(
        app_session_factory,
        workspace,
        first_thought,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\nstatus is {unique}old",
    )
    second_thought = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-2"
    )
    await _write_document(
        app_session_factory,
        workspace,
        second_thought,
        stable_key=f"project:{unique}",
        kind="project",
        title="A project",
        body_markdown=f"## Summary\n\nstatus is {unique}new",
    )

    reader = PostgresExactSearch(app_session_factory)
    old_hit = await reader.search(workspace, SearchQuery(phrase=f"{unique}old"))
    new_hit = await reader.search(workspace, SearchQuery(phrase=f"{unique}new"))

    assert all(item.document_id != document_id for item in old_hit.items)
    assert any(item.document_id == document_id for item in new_hit.items)


async def test_pagination_cursor_returns_the_next_page_without_duplicates(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    document_ids: set[uuid.UUID] = set()
    for i in range(3):
        thought_id = await _thought_id(
            app_session_factory, workspace, user_id, source_message_id=f"{unique}-{i}"
        )
        document_id, _ = await _write_document(
            app_session_factory,
            workspace,
            thought_id,
            stable_key=f"project:{unique}-{i}",
            kind="project",
            title=f"Project {i}",
            body_markdown=f"## Summary\n\n{unique} entry number {i}",
        )
        document_ids.add(document_id)

    reader = PostgresExactSearch(app_session_factory)
    first_page = await reader.search(workspace, SearchQuery(q=unique, limit=2))
    assert len(first_page.items) == 2
    assert first_page.next_cursor is not None

    second_page = await reader.search(
        workspace, SearchQuery(q=unique, limit=2, cursor=first_page.next_cursor)
    )

    seen = {item.document_id for item in first_page.items} | {
        item.document_id for item in second_page.items
    }
    assert seen == document_ids
    first_ids = {item.document_id for item in first_page.items}
    second_ids = {item.document_id for item in second_page.items}
    assert first_ids.isdisjoint(second_ids)


async def test_source_and_date_filters_require_one_thought_to_satisfy_both(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """Source and date/time are both provenance predicates on "a thought that
    supports this citation" and must be checked against the *same* cited
    thought, not against the document's citations independently. A document
    cited by a discord thought from 2019 and an api thought from today must
    not match `source=discord AND date in [2025, 2027]` - no single cited
    thought satisfies both at once."""
    source_only_thought = await _thought_id(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique}-source-only",
        source="discord",
        client_created_at=dt.datetime(2019, 1, 1, tzinfo=dt.UTC),
    )
    date_only_thought = await _thought_id(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique}-date-only",
        source="api",
    )
    split_document_id, _ = await _write_document_with_sources(
        app_session_factory,
        workspace,
        (source_only_thought, date_only_thought),
        stable_key=f"split:{unique}",
        kind="project",
        title="Split provenance",
        body_markdown=f"## Summary\n\n{unique} split provenance across two thoughts",
    )

    both_thought = await _thought_id(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique}-both",
        source="discord",
    )
    combined_document_id, _ = await _write_document(
        app_session_factory,
        workspace,
        both_thought,
        stable_key=f"combined:{unique}",
        kind="project",
        title="Combined provenance",
        body_markdown=f"## Summary\n\n{unique} single thought satisfies both filters",
    )

    reader = PostgresExactSearch(app_session_factory)
    page = await reader.search(
        workspace,
        SearchQuery(
            q=unique,
            source="discord",
            date_from=dt.date(2025, 1, 1),
            date_to=dt.date(2027, 1, 1),
        ),
    )

    result_ids = {item.document_id for item in page.items}
    assert split_document_id not in result_ids
    assert combined_document_id in result_ids


async def test_hydration_never_leaks_another_workspaces_thoughts_or_entities(
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """docs/adr/0005-workspace-scoped-single-user-first-schema.md treats a
    cross-workspace leak as P0. Citation and entity-mention hydration must
    stay scoped to the searching workspace on their own, rather than relying
    entirely on the caller (``search()``'s own revision-id list) always being
    correctly scoped - the same way every other query in this module already
    predicates on ``workspace_id`` rather than trusting an id alone."""
    other_workspace, other_user_id = await _second_workspace(admin_session_factory)

    thought_a = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-a"
    )
    document_a, revision_a = await _write_document(
        app_session_factory,
        workspace,
        thought_a,
        stable_key=f"project:{unique}-a",
        kind="project",
        title="Workspace A project",
        body_markdown=f"## Summary\n\n{unique} workspace a content",
    )

    other_thought = await _thought_id(
        app_session_factory,
        other_workspace,
        other_user_id,
        source_message_id=f"{unique}-b",
    )
    _, revision_b = await _write_document(
        app_session_factory,
        other_workspace,
        other_thought,
        stable_key=f"project:{unique}-b",
        kind="project",
        title="Workspace B project",
        body_markdown=f"## Summary\n\n{unique} workspace b content",
    )

    # Overlapping entity names in each workspace: entities are unique per
    # (workspace_id, entity_type, normalized_name), so the same display name
    # can legitimately exist in both workspaces under different ids.
    entity_a = uuid.uuid4()
    entity_b = uuid.uuid4()
    async with app_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(entities).values(
                id=entity_a,
                workspace_id=workspace,
                entity_type="person",
                canonical_name=f"Person {unique}",
                normalized_name=f"person {unique}",
                created_at=dt.datetime.now(dt.UTC),
            )
        )
        await session.execute(
            sa.insert(entities).values(
                id=entity_b,
                workspace_id=other_workspace,
                entity_type="person",
                canonical_name=f"Person {unique}",
                normalized_name=f"person {unique}",
                created_at=dt.datetime.now(dt.UTC),
            )
        )

    async with app_session_factory() as session:
        run_a = await session.scalar(
            sa.text("SELECT run_id FROM document_revisions WHERE id = :rid").bindparams(
                rid=revision_a
            )
        )
        run_b = await session.scalar(
            sa.text("SELECT run_id FROM document_revisions WHERE id = :rid").bindparams(
                rid=revision_b
            )
        )
    assert run_a is not None
    assert run_b is not None

    async with app_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(entity_mentions).values(
                workspace_id=workspace,
                entity_id=entity_a,
                revision_id=revision_a,
                run_id=run_a,
                surface_form=f"Person {unique}",
                confidence=1.0,
            )
        )
        await session.execute(
            sa.insert(entity_mentions).values(
                workspace_id=other_workspace,
                entity_id=entity_b,
                revision_id=revision_b,
                run_id=run_b,
                surface_form=f"Person {unique}",
                confidence=1.0,
            )
        )

    # Direct helper-level check: this is exactly the scenario the fix guards
    # against - a revision belonging to workspace B is present in the
    # `revision_ids` list, but hydration scoped to workspace A must not
    # return workspace B's own, otherwise perfectly valid, citation or entity
    # rows for it.
    async with app_session_factory() as session:
        thought_ids = await _thought_ids_for(session, workspace, [revision_a, revision_b])
        entity_names = await _entity_names_for(session, workspace, [revision_a, revision_b])

    assert revision_b not in thought_ids
    assert thought_ids[revision_a] == (thought_a,)
    assert revision_b not in entity_names
    assert entity_names[revision_a] == (f"Person {unique}",)

    # End-to-end: workspace A's own search result never surfaces workspace
    # B's citation or entity data, even with overlapping entity display names.
    reader = PostgresExactSearch(app_session_factory)
    page = await reader.search(workspace, SearchQuery(q=unique))
    result = next(item for item in page.items if item.document_id == document_a)
    assert result.thought_ids == (thought_a,)
    assert result.entities == (f"Person {unique}",)

"""``PostgresSemanticHydrator`` against real organize-writer output
(docs/DESIGN.md 7.5, 9.2). Reuses ``test_search_reader.py``'s document-seeding
helpers and fixtures rather than duplicating them - both files build the same
kind of fixture data over the same schema.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_application.search import Search
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import khoj_filename
from tc_domain.khoj_ports import KhojSearchResult
from tc_domain.search import SearchQuery
from tc_infrastructure.db.khoj_index_recorder import PostgresKhojIndexRecorder
from tc_infrastructure.db.search_reader import PostgresExactSearch
from tc_infrastructure.db.semantic_hydrator import PostgresSemanticHydrator
from tc_infrastructure.db.tables import entities, entity_mentions
from tests.integration.test_search_reader import (
    _thought_id,
    _write_document,
    unique,
    user_id,
    workspace,
)
from tests.unit.fakes import FakeKhojPort

pytestmark = pytest.mark.integration

__all__ = ["unique", "user_id", "workspace"]  # re-exported fixtures


async def _mark_synced(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    *,
    document_id: uuid.UUID,
    filename: str,
    revision_id: uuid.UUID,
) -> None:
    """Records a real ``khoj_index_items`` row via the same recorder the
    production sync pipeline uses (``DeliverKhojSync`` /
    ``PostgresKhojIndexRecorder``), so hydration's "Khoj is caught up with
    the current revision" join has something to match against - every
    hydration test below needs this unless it is deliberately testing the
    unsynced/stale case."""
    recorder = PostgresKhojIndexRecorder(app_session_factory)
    await recorder.record_synced(
        workspace_id=workspace,
        document_id=document_id,
        filename=filename,
        revision_id=revision_id,
        body_sha256=hashlib.sha256(filename.encode()).hexdigest(),
    )


async def test_hydrate_resolves_a_filename_to_its_current_revision(
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
        stable_key=f"project:hydrate-{unique}",
        kind="project",
        title=f"Hydrate {unique}",
        body_markdown="## Summary\n\nsomething distinctive",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:hydrate-{unique}",
        document_id=document_id,
    )
    await _mark_synced(
        app_session_factory,
        workspace,
        document_id=document_id,
        filename=filename,
        revision_id=revision_id,
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, SearchQuery(), (filename,))

    assert filename in hydrated
    result = hydrated[filename]
    assert result.document_id == document_id
    assert result.revision_id == revision_id
    assert result.channels == ("semantic",)
    assert result.thought_ids == (thought_id,)


async def test_hydrate_skips_a_filename_from_a_different_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    foreign_filename = khoj_filename(
        workspace_id=uuid.uuid4(),
        kind="project",
        stable_key="project:foreign",
        document_id=uuid.uuid4(),
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, SearchQuery(), (foreign_filename,))

    assert hydrated == {}


async def test_hydrate_skips_an_unparseable_filename(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, SearchQuery(), ("not-a-khoj-filename.md",))

    assert hydrated == {}


async def test_hydrate_skips_a_document_id_that_does_not_resolve(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    """A well-formed filename for a document that was never written (or
    belongs to a workspace's stale export) must not raise."""
    phantom_filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key="project:phantom",
        document_id=uuid.uuid4(),
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, SearchQuery(), (phantom_filename,))

    assert hydrated == {}


async def test_hydrate_drops_a_result_khoj_has_not_finished_syncing(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """A document with no ``khoj_index_items`` row at all - Khoj has never
    acknowledged indexing it - must not be hydrated, even though the filename
    parses and the document exists (docs/DESIGN.md 7.5 P1)."""
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, _revision_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:unsynced-{unique}",
        kind="project",
        title=f"Unsynced {unique}",
        body_markdown="## Summary\n\nnever synced to khoj",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:unsynced-{unique}",
        document_id=document_id,
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, SearchQuery(), (filename,))

    assert hydrated == {}


async def test_hydrate_drops_a_result_whose_synced_revision_is_stale(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """Khoj acknowledged syncing an *earlier* revision, but the document has
    since moved on to a newer one. Resolving the filename straight to
    ``documents.current_revision_id`` would silently relabel the newer body
    as if Khoj's stale semantic hit were about it (docs/DESIGN.md 7.5 P1) -
    hydration must instead drop this result until a sync catches up."""
    first_thought = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-1"
    )
    document_id, old_revision_id = await _write_document(
        app_session_factory,
        workspace,
        first_thought,
        stable_key=f"project:stale-{unique}",
        kind="project",
        title=f"Stale {unique}",
        body_markdown="## Summary\n\noriginal body",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:stale-{unique}",
        document_id=document_id,
    )
    # Khoj acknowledged the *old* revision.
    await _mark_synced(
        app_session_factory,
        workspace,
        document_id=document_id,
        filename=filename,
        revision_id=old_revision_id,
    )

    # The document is revised again; PostgreSQL's current revision moves on,
    # but nothing has re-synced Khoj yet.
    second_thought = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-2"
    )
    document_id_again, new_revision_id = await _write_document(
        app_session_factory,
        workspace,
        second_thought,
        stable_key=f"project:stale-{unique}",
        kind="project",
        title=f"Stale {unique}",
        body_markdown="## Summary\n\nrevised body",
    )
    assert document_id_again == document_id
    assert new_revision_id != old_revision_id

    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, SearchQuery(), (filename,))

    assert hydrated == {}


async def test_hydrate_enforces_the_kind_filter(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """A semantic hit's relevance score never implies it satisfies a
    structured filter Khoj cannot itself evaluate - kind must still be
    enforced in trusted PostgreSQL (docs/DESIGN.md 7.5 P1)."""
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, revision_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:kind-{unique}",
        kind="project",
        title=f"Kind {unique}",
        body_markdown="## Summary\n\nsemantically relevant but the wrong kind",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:kind-{unique}",
        document_id=document_id,
    )
    await _mark_synced(
        app_session_factory,
        workspace,
        document_id=document_id,
        filename=filename,
        revision_id=revision_id,
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    matching = await hydrator.hydrate(workspace, SearchQuery(kind="project"), (filename,))
    mismatched = await hydrator.hydrate(workspace, SearchQuery(kind="daily_digest"), (filename,))

    assert filename in matching
    assert mismatched == {}


async def test_hydrate_enforces_the_phrase_filter(
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
        stable_key=f"project:phrase-{unique}",
        kind="project",
        title=f"Phrase {unique}",
        body_markdown=f"## Summary\n\nthe {unique} launch window opens at dawn",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:phrase-{unique}",
        document_id=document_id,
    )
    await _mark_synced(
        app_session_factory,
        workspace,
        document_id=document_id,
        filename=filename,
        revision_id=revision_id,
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    matching = await hydrator.hydrate(
        workspace, SearchQuery(phrase=f"{unique} launch window"), (filename,)
    )
    mismatched = await hydrator.hydrate(
        workspace, SearchQuery(phrase=f"{unique} submarine dive"), (filename,)
    )

    assert filename in matching
    assert mismatched == {}


async def test_hydrate_enforces_the_include_filter(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """``q`` is intentionally not required to literally match a semantic hit
    (that is the whole point of semantic search), but ``include`` is
    documented as a hard required-word constraint (docs/DESIGN.md 9.1), not a
    relevance hint - a semantically similar document that never mentions a
    required word must still be dropped, the same way exact search would
    never have matched it (docs/DESIGN.md 7.5 P1)."""
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, revision_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:include-{unique}",
        kind="project",
        title=f"Include {unique}",
        body_markdown=f"## Summary\n\n{unique} talks about the quarterly roadmap",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:include-{unique}",
        document_id=document_id,
    )
    await _mark_synced(
        app_session_factory,
        workspace,
        document_id=document_id,
        filename=filename,
        revision_id=revision_id,
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    # `q` alone (no `include`) is not required to literally match.
    semantic_q_only = await hydrator.hydrate(
        workspace, SearchQuery(q="a paraphrase of the topic"), (filename,)
    )
    matching = await hydrator.hydrate(workspace, SearchQuery(include=("roadmap",)), (filename,))
    mismatched = await hydrator.hydrate(workspace, SearchQuery(include=("budget",)), (filename,))

    assert filename in semantic_q_only
    assert filename in matching
    assert mismatched == {}


async def test_hydrate_enforces_the_exclude_filter(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """``exclude`` is a hard forbidden-word guarantee, not a soft relevance
    nudge - Khoj's embedding similarity gives no assurance a forbidden word
    is actually absent, so a document whose body contains it must still be
    dropped."""
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, revision_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:exclude-{unique}",
        kind="project",
        title=f"Exclude {unique}",
        body_markdown=f"## Summary\n\n{unique} mentions banana explicitly",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:exclude-{unique}",
        document_id=document_id,
    )
    await _mark_synced(
        app_session_factory,
        workspace,
        document_id=document_id,
        filename=filename,
        revision_id=revision_id,
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    without_exclusion = await hydrator.hydrate(workspace, SearchQuery(), (filename,))
    excluded = await hydrator.hydrate(workspace, SearchQuery(exclude=("banana",)), (filename,))

    assert filename in without_exclusion
    assert excluded == {}


async def test_hydrate_enforces_the_entity_id_filter(
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
        stable_key=f"project:entity-{unique}",
        kind="project",
        title=f"Entity {unique}",
        body_markdown=f"## Summary\n\n{unique} mentions someone",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:entity-{unique}",
        document_id=document_id,
    )
    await _mark_synced(
        app_session_factory,
        workspace,
        document_id=document_id,
        filename=filename,
        revision_id=revision_id,
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

    hydrator = PostgresSemanticHydrator(app_session_factory)
    matching = await hydrator.hydrate(workspace, SearchQuery(entity_id=entity_id), (filename,))
    mismatched = await hydrator.hydrate(workspace, SearchQuery(entity_id=uuid.uuid4()), (filename,))

    assert filename in matching
    assert mismatched == {}


async def test_hydrate_enforces_the_source_filter(
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
    document_id, revision_id = await _write_document(
        app_session_factory,
        workspace,
        discord_thought,
        stable_key=f"project:source-{unique}",
        kind="project",
        title=f"Source {unique}",
        body_markdown=f"## Summary\n\n{unique} from discord",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:source-{unique}",
        document_id=document_id,
    )
    await _mark_synced(
        app_session_factory,
        workspace,
        document_id=document_id,
        filename=filename,
        revision_id=revision_id,
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    matching = await hydrator.hydrate(workspace, SearchQuery(source="discord"), (filename,))
    mismatched = await hydrator.hydrate(workspace, SearchQuery(source="api"), (filename,))

    assert filename in matching
    assert mismatched == {}


# ---------------------------------------------------------------------------
# End-to-end through `Search`: a real Khoj hit for a document missing a
# required `include` word must not survive into either mode's own response
# (docs/DESIGN.md 7.5 P1) - `FakeKhojPort` stands in for Khoj itself (not
# available to the "integration" test tier; a real Khoj is only exercised by
# the "contract" tier), but everything downstream of it - hydration, filter
# enforcement, RRF fusion - is the real production code against a real
# database.
# ---------------------------------------------------------------------------


async def _seed_document_missing_a_required_word(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> tuple[uuid.UUID, str]:
    """Returns ``(document_id, filename)`` for a document Khoj has fully
    synced, whose body never mentions "budget"."""
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, revision_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:e2e-include-{unique}",
        kind="project",
        title=f"E2E include {unique}",
        body_markdown=f"## Summary\n\n{unique} talks about the quarterly roadmap",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:e2e-include-{unique}",
        document_id=document_id,
    )
    await _mark_synced(
        app_session_factory,
        workspace,
        document_id=document_id,
        filename=filename,
        revision_id=revision_id,
    )
    return document_id, filename


async def test_semantic_search_drops_a_khoj_hit_missing_a_required_word(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    document_id, filename = await _seed_document_missing_a_required_word(
        app_session_factory, workspace, user_id, unique
    )
    khoj = FakeKhojPort(
        search_results=(KhojSearchResult(entry="...", score=0.9, filename=filename),)
    )
    search = Search(
        PostgresExactSearch(app_session_factory),
        khoj,
        PostgresSemanticHydrator(app_session_factory),
    )

    page = await search(
        workspace, SearchQuery(q="a paraphrase of the topic", include=("budget",)), mode="semantic"
    )

    assert page.degraded is False
    assert all(item.document_id != document_id for item in page.items)


async def test_hybrid_search_drops_a_semantic_only_hit_missing_a_required_word(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """Exact search would never have surfaced this document either (it also
    requires ``include``), but before this fix the *semantic* channel alone
    could inject it into the fused hybrid result regardless."""
    document_id, filename = await _seed_document_missing_a_required_word(
        app_session_factory, workspace, user_id, unique
    )
    khoj = FakeKhojPort(
        search_results=(KhojSearchResult(entry="...", score=0.9, filename=filename),)
    )
    search = Search(
        PostgresExactSearch(app_session_factory),
        khoj,
        PostgresSemanticHydrator(app_session_factory),
    )

    page = await search(
        workspace, SearchQuery(q="a paraphrase of the topic", include=("budget",)), mode="hybrid"
    )

    assert page.degraded is False
    assert all(item.document_id != document_id for item in page.items)

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
    RunOutcome,
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
    runs,
    thoughts,
)

pytestmark = pytest.mark.integration

BODY = (
    "## Summary\n\nsomething\n\n## Current state\n\n-\n\n## Open threads\n\n-\n\n## Timeline\n\n- x"
)


def _outcome(**overrides: object) -> RunOutcome:
    defaults: dict[str, object] = {
        "model_provider": "offline",
        "model_id": "offline-model",
        "prompt_version": "organize-v1",
        "input_tokens": 10,
        "output_tokens": 20,
        "context_recall": None,
        "context_degraded": False,
    }
    defaults.update(overrides)
    return RunOutcome(**defaults)  # type: ignore[arg-type]


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
    result = await writer.write(
        workspace_id=workspace, run_id=run_id, request=request, outcome=_outcome()
    )

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

    first = await writer.write(
        workspace_id=workspace, run_id=run_id, request=request("first"), outcome=_outcome()
    )
    second_run = await _run_id(app_session_factory, workspace)
    second = await writer.write(
        workspace_id=workspace, run_id=second_run, request=request("second"), outcome=_outcome()
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
    await writer.write(workspace_id=workspace, run_id=run_id, request=request, outcome=_outcome())

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
    result = await writer.write(
        workspace_id=workspace, run_id=run_id, request=request, outcome=_outcome()
    )

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
    result = await writer.write(
        workspace_id=workspace, run_id=run_id, request=request, outcome=_outcome()
    )

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


async def test_a_non_digest_only_write_enqueues_no_digest_outbox_event(
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
    result = await writer.write(
        workspace_id=workspace, run_id=run_id, request=request, outcome=_outcome()
    )

    assert result.digest_document_id is None
    async with app_session_factory() as session:
        events = (
            await session.execute(
                sa.select(outbox_events).where(
                    outbox_events.c.aggregate_id == str(run_id),
                    outbox_events.c.event_type == "digest.ready",
                )
            )
        ).all()
    assert events == []


async def test_writing_a_document_enqueues_a_khoj_sync_event_for_it(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """docs/DESIGN.md 7.2 step 11: every document written this run gets an
    index-sync event, not just a daily digest."""
    run_id = await _run_id(app_session_factory, workspace)
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=f"project:khoj-sync-{unique}",
                kind="project",
                title="Khoj sync",
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
    result = await writer.write(
        workspace_id=workspace, run_id=run_id, request=request, outcome=_outcome()
    )

    document_id = result.document_ids[f"project:khoj-sync-{unique}"]
    async with app_session_factory() as session:
        event = (
            await session.execute(
                sa.select(outbox_events).where(
                    outbox_events.c.event_type == "khoj.sync_requested",
                    outbox_events.c.aggregate_id == str(document_id),
                )
            )
        ).one()
    assert event.payload == {"document_id": str(document_id)}


async def test_an_untouched_document_gets_no_khoj_sync_event_on_a_later_write(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """docs/DESIGN.md invariant 5: only touched documents are regenerated -
    an untouched one must not be re-synced either."""
    run_id = await _run_id(app_session_factory, workspace)
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    touched_key = f"project:touched-{unique}"
    untouched_key = f"project:untouched-{unique}"
    writer = PostgresOrganizeWriter(app_session_factory)

    first_request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=touched_key,
                kind="project",
                title="Touched",
                body_markdown=BODY,
                source_thought_ids=(thought_id,),
                mentioned_entities=(),
                change_summary="created",
            ),
            DocumentWrite(
                stable_key=untouched_key,
                kind="project",
                title="Untouched",
                body_markdown=BODY,
                source_thought_ids=(thought_id,),
                mentioned_entities=(),
                change_summary="created",
            ),
        ),
        context_selections=(),
        unorganized_thought_ids=(),
    )
    first = await writer.write(
        workspace_id=workspace, run_id=run_id, request=first_request, outcome=_outcome()
    )
    untouched_document_id = first.document_ids[untouched_key]

    second_run = await _run_id(app_session_factory, workspace)
    second_request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=touched_key,
                kind="project",
                title="Touched",
                body_markdown=BODY,
                source_thought_ids=(thought_id,),
                mentioned_entities=(),
                change_summary="updated again",
            ),
        ),
        context_selections=(),
        unorganized_thought_ids=(),
    )
    await writer.write(
        workspace_id=workspace, run_id=second_run, request=second_request, outcome=_outcome()
    )

    async with app_session_factory() as session:
        events = (
            await session.execute(
                sa.select(outbox_events).where(
                    outbox_events.c.event_type == "khoj.sync_requested",
                    outbox_events.c.aggregate_id == str(untouched_document_id),
                )
            )
        ).all()
    # Exactly one event: from the first write, none from the second (the
    # untouched document was not part of that request).
    assert len(events) == 1


async def test_write_marks_the_run_succeeded_in_the_same_transaction(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """The crash window this closes: a prior version called ``RunLedger.succeed``
    in a *separate* transaction after ``write`` committed, so a crash in
    between left durable output with a run stuck ``running`` forever -
    invisible to ``OrganizeScheduler``'s resume check, which only trusts
    ``status='succeeded'``. Retrying such a window would silently create a
    second run, a second digest document, and a second Discord delivery for
    the same day. Folding the status transition into ``write``'s own
    transaction means there is no longer a gap for a crash to land in: this
    call either produces both the documents *and* the succeeded status, or
    neither.
    """
    run_id = await _run_id(app_session_factory, workspace)
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=f"project:outcome-{unique}",
                kind="project",
                title="Outcome",
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
    await writer.write(
        workspace_id=workspace,
        run_id=run_id,
        request=request,
        outcome=_outcome(input_tokens=42, output_tokens=99, context_recall=0.75),
    )

    async with app_session_factory() as session:
        row = (await session.execute(sa.select(runs).where(runs.c.id == run_id))).one()

    assert row.status == "succeeded"
    assert row.finished_at is not None
    assert row.input_tokens == 42
    assert row.output_tokens == 99
    assert float(row.context_recall) == pytest.approx(0.75)

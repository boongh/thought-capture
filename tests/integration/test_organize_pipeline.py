"""End-to-end organize pipeline against real PostgreSQL (docs/DESIGN.md 7.2).

Wires every real adapter together - the same objects ``apps/worker`` wires -
behind the deterministic offline provider (``docs/model-evaluation-organize-select.md``
records that, today, safe mode has no reviewed model and every real
deployment runs organize on this same adapter). What this proves is
orchestration, persistence, and provenance, never generation quality
(``tc_infrastructure.llm.offline``'s own docstring).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_application.organize import OrganizeWindow
from tc_domain.capture import WorkspaceId
from tc_domain.llm import LLMRequest, LLMResponse
from tc_domain.windows import CaptureWindow
from tc_infrastructure.db.context_index import PostgresContextIndex
from tc_infrastructure.db.entity_repository import PostgresEntityRepository
from tc_infrastructure.db.llm_journal import PostgresLLMJournal
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.tables import documents, entities, outbox_events, runs, thoughts
from tc_infrastructure.db.thought_reader import PostgresThoughtReader
from tc_infrastructure.llm.offline import OfflineLLMProvider

pytestmark = pytest.mark.integration

DIGEST_BODY = (
    "## Summary\n\nCaught up with {name}.\n\n## Current state\n\n-\n\n"
    "## Open threads\n\n-\n\n## Timeline\n\n- caught up"
)
PROJECT_BODY = (
    "## Summary\n\nWorking on {name}.\n\n## Current state\n\nIn progress.\n\n"
    "## Open threads\n\n-\n\n## Timeline\n\n- made progress"
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


async def _capture(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    *,
    source_message_id: str,
    body: str,
    created_at: dt.datetime,
) -> None:
    async with app_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(thoughts).values(
                workspace_id=workspace,
                author_user_id=user_id,
                source="discord",
                source_message_id=source_message_id,
                body=body,
                client_created_at=created_at,
                client_timezone="Asia/Bangkok",
                client_local_date=created_at.date(),
                client_local_time=created_at.time(),
                content_language="en",
            )
        )


def build_pipeline(
    app_session_factory: async_sessionmaker[AsyncSession], provider: OfflineLLMProvider
) -> OrganizeWindow:
    journal = PostgresLLMJournal(app_session_factory)

    def journal_factory(workspace_id: WorkspaceId, run_id: uuid.UUID):
        async def write(request: LLMRequest, response: LLMResponse, attempt: int) -> None:
            await journal.record(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request,
                response=response,
                sequence=attempt,
            )

        return write

    return OrganizeWindow(
        thoughts=PostgresThoughtReader(app_session_factory),
        context_index=PostgresContextIndex(app_session_factory),
        provider=provider,
        journal_factory=journal_factory,
        writer=PostgresOrganizeWriter(app_session_factory, entities=PostgresEntityRepository()),
        run_ledger=PostgresRunLedger(app_session_factory),
        usage_for=journal.usage_for,
    )


async def test_a_window_is_organized_into_a_digest_and_a_project_document(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    start = dt.datetime(2031, 1, 2, 13, tzinfo=dt.UTC)
    end = dt.datetime(2031, 1, 3, 13, tzinfo=dt.UTC)
    project_name = f"Aurora {unique}"

    await _capture(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique}-1",
        body=f"Made progress on {project_name} today.",
        created_at=start + dt.timedelta(hours=1),
    )

    digest_key = f"daily_digest:{unique}"
    project_key = f"project:aurora-{unique}"

    async with app_session_factory() as session:
        real_thought_id = (
            await session.execute(
                sa.select(thoughts.c.id).where(
                    thoughts.c.workspace_id == workspace,
                    thoughts.c.source_message_id == f"{unique}-1",
                )
            )
        ).scalar_one()

    def responder(request: LLMRequest) -> str:
        if request.schema_name == "SelectedContext":
            return json.dumps({"stable_keys": [], "reasoning": ""})
        return json.dumps(
            {
                "documents": [
                    {
                        "stable_key": digest_key,
                        "kind": "daily_digest",
                        "title": "Daily digest",
                        "body_markdown": DIGEST_BODY.format(name=project_name),
                        "source_thought_ids": [real_thought_id],
                        "mentioned_entities": [
                            {
                                "entity_type": "project",
                                "canonical_name": project_name,
                                "surface_form": project_name,
                                "confidence": 0.95,
                            }
                        ],
                        "change_summary": "first digest",
                        "confidence": 1.0,
                    },
                    {
                        "stable_key": project_key,
                        "kind": "project",
                        "title": project_name,
                        "body_markdown": PROJECT_BODY.format(name=project_name),
                        "source_thought_ids": [real_thought_id],
                        "mentioned_entities": [
                            {
                                "entity_type": "project",
                                "canonical_name": project_name,
                                "surface_form": project_name,
                                "confidence": 0.95,
                            }
                        ],
                        "change_summary": "created",
                        "confidence": 1.0,
                    },
                ],
                "unorganized_thought_ids": [],
                "referenced_document_keys": [],
            }
        )

    provider = OfflineLLMProvider(responder=responder)
    pipeline = build_pipeline(app_session_factory, provider)
    window = CaptureWindow(start=start, end=end)

    run_id = await pipeline(workspace, window)

    async with app_session_factory() as session:
        run = (await session.execute(sa.select(runs).where(runs.c.id == run_id))).one()
        digest = (
            await session.execute(
                sa.select(documents).where(
                    documents.c.workspace_id == workspace, documents.c.stable_key == digest_key
                )
            )
        ).one()
        project = (
            await session.execute(
                sa.select(documents).where(
                    documents.c.workspace_id == workspace, documents.c.stable_key == project_key
                )
            )
        ).one()
        entity = (
            await session.execute(
                sa.select(entities).where(
                    entities.c.workspace_id == workspace, entities.c.canonical_name == project_name
                )
            )
        ).one()
        event = (
            await session.execute(
                sa.select(outbox_events).where(outbox_events.c.aggregate_id == str(run_id))
            )
        ).one()

    assert run.status == "succeeded"
    assert digest.current_revision_id is not None
    assert project.current_revision_id is not None
    assert entity.entity_type == "project"
    assert event.event_type == "digest.ready"


async def test_a_named_reference_is_found_by_the_alias_signal_on_a_later_run(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """Proves the loop: an entity created on day one is found by the alias
    signal via its own document index on day two, without the selector call
    needing to do anything (the window names it directly)."""
    project_name = f"Nebula {unique}"
    project_key = f"project:nebula-{unique}"

    day1_start = dt.datetime(2031, 2, 1, 13, tzinfo=dt.UTC)
    day1_end = dt.datetime(2031, 2, 2, 13, tzinfo=dt.UTC)
    await _capture(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique}-day1",
        body=f"Started {project_name}.",
        created_at=day1_start + dt.timedelta(hours=1),
    )

    async def thought_id_for(tag: str) -> int:
        async with app_session_factory() as session:
            return (
                await session.execute(
                    sa.select(thoughts.c.id).where(
                        thoughts.c.workspace_id == workspace,
                        thoughts.c.source_message_id == f"{unique}-{tag}",
                    )
                )
            ).scalar_one()

    day1_thought_id = await thought_id_for("day1")

    def day1_responder(request: LLMRequest) -> str:
        if request.schema_name == "SelectedContext":
            return json.dumps({"stable_keys": [], "reasoning": ""})
        return json.dumps(
            {
                "documents": [
                    {
                        "stable_key": project_key,
                        "kind": "project",
                        "title": project_name,
                        "body_markdown": PROJECT_BODY.format(name=project_name),
                        "source_thought_ids": [day1_thought_id],
                        "mentioned_entities": [
                            {
                                "entity_type": "project",
                                "canonical_name": project_name,
                                "surface_form": project_name,
                                "confidence": 0.95,
                            }
                        ],
                        "change_summary": "created",
                        "confidence": 1.0,
                    }
                ],
                "unorganized_thought_ids": [],
                "referenced_document_keys": [],
            }
        )

    day1_pipeline = build_pipeline(
        app_session_factory, OfflineLLMProvider(responder=day1_responder)
    )
    await day1_pipeline(workspace, CaptureWindow(start=day1_start, end=day1_end))

    # Day two: the window names the project directly, so the deterministic
    # alias signal (not the selector) must find it and send its full body.
    day2_start = day1_end
    day2_end = dt.datetime(2031, 2, 3, 13, tzinfo=dt.UTC)
    await _capture(
        app_session_factory,
        workspace,
        user_id,
        source_message_id=f"{unique}-day2",
        body=f"More progress on {project_name}.",
        created_at=day2_start + dt.timedelta(hours=1),
    )
    day2_thought_id = await thought_id_for("day2")

    seen_index_bodies: list[str] = []

    def day2_responder(request: LLMRequest) -> str:
        if request.schema_name == "SelectedContext":
            return json.dumps({"stable_keys": [], "reasoning": ""})
        seen_index_bodies.append(request.messages[-1].content)
        return json.dumps(
            {
                "documents": [
                    {
                        "stable_key": project_key,
                        "kind": "project",
                        "title": project_name,
                        "body_markdown": PROJECT_BODY.format(name=project_name),
                        "source_thought_ids": [day2_thought_id],
                        "mentioned_entities": [
                            {
                                "entity_type": "project",
                                "canonical_name": project_name,
                                "surface_form": project_name,
                                "confidence": 0.95,
                            }
                        ],
                        "change_summary": "updated",
                        "confidence": 1.0,
                    }
                ],
                "unorganized_thought_ids": [],
                "referenced_document_keys": [project_key],
            }
        )

    day2_pipeline = build_pipeline(
        app_session_factory, OfflineLLMProvider(responder=day2_responder)
    )
    run_id = await day2_pipeline(workspace, CaptureWindow(start=day2_start, end=day2_end))

    # The full body of the existing project document was sent to the organize
    # prompt - proof the alias signal picked it up from the Tier 1 index.
    assert any("Working on" in body or "In progress" in body for body in seen_index_bodies)

    async with app_session_factory() as session:
        run = (await session.execute(sa.select(runs).where(runs.c.id == run_id))).one()
    assert run.status == "succeeded"
    assert run.context_recall == pytest.approx(1.0)

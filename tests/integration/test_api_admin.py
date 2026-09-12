"""``POST /v1/admin/khoj-sync``, against a real database (docs/DESIGN.md 10)."""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.organize import DocumentWrite, OrganizeWriteRequest, RunOutcome
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.tables import outbox_events, thoughts
from tests.integration.conftest import API_TOKEN

pytestmark = pytest.mark.integration

AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
BODY = "## Summary\n\nsomething"


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


async def _seed_document(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    unique: str,
) -> uuid.UUID:
    now = dt.datetime.now(dt.UTC)
    async with app_session_factory() as session, session.begin():
        thought_id = await session.scalar(
            sa.insert(thoughts)
            .values(
                workspace_id=workspace_id,
                author_user_id=user_id,
                source="api",
                source_message_id=unique,
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

    ledger = PostgresRunLedger(app_session_factory)
    run_id = await ledger.start(
        WorkspaceId(workspace_id),
        window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    )
    stable_key = f"project:admin-khoj-sync-{unique}"
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=stable_key,
                kind="project",
                title="Admin khoj sync",
                body_markdown=BODY,
                source_thought_ids=(ThoughtId(thought_id),),
                mentioned_entities=(),
                change_summary="created",
            ),
        ),
        context_selections=(),
        unorganized_thought_ids=(),
    )
    writer = PostgresOrganizeWriter(app_session_factory)
    result = await writer.write(
        workspace_id=WorkspaceId(workspace_id), run_id=run_id, request=request, outcome=_outcome()
    )
    return result.document_ids[stable_key]


async def test_force_khoj_sync_requires_auth(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/admin/khoj-sync")
    assert response.status_code == 401


async def test_force_khoj_sync_enqueues_every_current_document(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = fresh_identity
    document_id = await _seed_document(
        app_session_factory, workspace_id, user_id, unique=unique_message_id
    )

    response = await api_fresh.post("/v1/admin/khoj-sync", headers=AUTH)

    assert response.status_code == 200
    # fresh_identity is a private workspace, so exactly this one document.
    assert response.json()["enqueued"] == 1
    async with app_session_factory() as session:
        events = (
            await session.execute(
                sa.select(outbox_events).where(
                    outbox_events.c.event_type == "khoj.sync_requested",
                    outbox_events.c.aggregate_id == str(document_id),
                )
            )
        ).all()
    # The organize write already enqueued one; force-sync adds a second,
    # independent event for the same document (both are valid, retried
    # independently - force-sync is a trigger, not a dedup mechanism).
    assert len(events) == 2


async def test_reembed_requires_auth(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/admin/reembed")
    assert response.status_code == 401


async def test_reembed_returns_503_when_the_embedding_sidecar_is_unreachable(
    api_fresh: httpx.AsyncClient,
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """F15-A (docs/plans/embedding-sync-review-round-4.md): the target model
    id must come from the sidecar's own ``/health``, never a guess - an
    unreachable sidecar (this test environment's default
    ``TC_EMBEDDING_SIDECAR_BASE_URL``, which resolves only inside the
    Compose network) must fail loudly rather than ever recording a
    ``runs(kind='reembed')`` row against a fabricated model id.
    """
    response = await api_fresh.post("/v1/admin/reembed", headers=AUTH)

    assert response.status_code == 503


async def test_reembed_accepts_no_workspace_controlling_input(api_fresh: httpx.AsyncClient) -> None:
    """docs/DESIGN.md 10: workspace comes from auth, never a client-supplied
    parameter. Asserted at the OpenAPI-schema level, not just by observing
    a response, since the endpoint takes no body either way - the
    regression this guards is someone later adding a ``workspace_id`` query
    parameter or request-body field and reading it instead of
    ``context.workspace_id``."""
    schema = api_fresh.get("/openapi.json")
    response = await schema
    operation = response.json()["paths"]["/v1/admin/reembed"]["post"]

    assert "requestBody" not in operation
    assert "parameters" not in operation or all(
        param.get("in") != "query" for param in operation.get("parameters", [])
    )


async def test_force_embedding_sync_requires_auth(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/admin/embedding-sync")
    assert response.status_code == 401


async def test_force_embedding_sync_enqueues_every_current_document(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = fresh_identity
    document_id = await _seed_document(
        app_session_factory, workspace_id, user_id, unique=unique_message_id
    )

    response = await api_fresh.post("/v1/admin/embedding-sync", headers=AUTH)

    assert response.status_code == 200
    # fresh_identity is a private workspace, so exactly this one document.
    assert response.json()["enqueued"] == 1
    async with app_session_factory() as session:
        events = (
            await session.execute(
                sa.select(outbox_events).where(
                    outbox_events.c.event_type == "embedding.sync_requested",
                    outbox_events.c.aggregate_id == str(document_id),
                )
            )
        ).all()
    # The organize write already enqueued one; force-sync adds a second,
    # independent event for the same document (both are valid, retried
    # independently - force-sync is a trigger, not a dedup mechanism).
    assert len(events) == 2

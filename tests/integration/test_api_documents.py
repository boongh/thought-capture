"""``GET /v1/documents`` and ``/v1/documents/{id}``, against a real database."""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.organize import DocumentWrite, OrganizeWriteRequest
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.tables import thoughts
from tests.integration.conftest import API_TOKEN

pytestmark = pytest.mark.integration

AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
BODY = "## Summary\n\nsomething"


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
    stable_key = f"project:api-{unique}"
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=stable_key,
                kind="project",
                title=f"Project {unique}",
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
        workspace_id=WorkspaceId(workspace_id), run_id=run_id, request=request
    )
    return result.document_ids[stable_key]


async def test_list_documents_requires_auth(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.get("/v1/documents")
    assert response.status_code == 401


async def test_list_documents_includes_a_written_document(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = fresh_identity
    document_id = await _seed_document(
        app_session_factory, workspace_id, user_id, unique=unique_message_id
    )

    response = await api_fresh.get("/v1/documents", headers=AUTH)

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert str(document_id) in ids


async def test_list_documents_filters_by_kind(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = fresh_identity
    document_id = await _seed_document(
        app_session_factory, workspace_id, user_id, unique=unique_message_id
    )

    matching = await api_fresh.get("/v1/documents", headers=AUTH, params={"kind": "project"})
    assert str(document_id) in {item["id"] for item in matching.json()["items"]}

    non_matching = await api_fresh.get(
        "/v1/documents", headers=AUTH, params={"kind": "daily_digest"}
    )
    assert str(document_id) not in {item["id"] for item in non_matching.json()["items"]}


async def test_get_document_returns_the_body(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = fresh_identity
    document_id = await _seed_document(
        app_session_factory, workspace_id, user_id, unique=unique_message_id
    )

    response = await api_fresh.get(f"/v1/documents/{document_id}", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["body_markdown"] == BODY


async def test_get_document_404s_for_an_unknown_id(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.get(f"/v1/documents/{uuid.uuid4()}", headers=AUTH)
    assert response.status_code == 404

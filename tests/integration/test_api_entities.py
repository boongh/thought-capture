"""``GET /v1/entities`` and ``/v1/entities/{id}``, against a real database."""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.entities import EntityType
from tc_infrastructure.db.entity_repository import PostgresEntityRepository, ResolvedEntity
from tc_infrastructure.db.tables import runs, thoughts
from tests.integration.conftest import API_TOKEN

pytestmark = pytest.mark.integration

AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


async def _run_row(
    factory: async_sessionmaker[AsyncSession], workspace_id: WorkspaceId
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=run_id, workspace_id=workspace_id, kind="organize", status="running"
            )
        )
    return run_id


async def _thought_row(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: WorkspaceId,
    user_id: uuid.UUID,
    *,
    source_message_id: str,
) -> ThoughtId:
    now = dt.datetime.now(dt.UTC)
    async with factory() as session, session.begin():
        thought_id = await session.scalar(
            sa.insert(thoughts)
            .values(
                workspace_id=workspace_id,
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


async def _seed_entity(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    unique: str,
) -> ResolvedEntity:
    workspace = WorkspaceId(workspace_id)
    run_id = await _run_row(app_session_factory, workspace)
    thought_id = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    repository = PostgresEntityRepository()
    async with app_session_factory() as session, session.begin():
        return await repository.resolve_mention(
            session,
            workspace_id=workspace,
            run_id=run_id,
            entity_type=EntityType.PERSON,
            canonical_name=f"Person {unique}",
            surface_form=f"Person {unique}",
            confidence=0.9,
            thought_id=thought_id,
        )


async def test_list_entities_requires_auth(api: httpx.AsyncClient) -> None:
    response = await api.get("/v1/entities")
    assert response.status_code == 401


async def test_list_entities_includes_a_resolved_entity(
    api: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = seeded_identity
    resolved = await _seed_entity(
        app_session_factory, workspace_id, user_id, unique=unique_message_id
    )

    response = await api.get("/v1/entities", headers=AUTH)

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert str(resolved.entity_id) in ids


async def test_get_entity_returns_its_detail(
    api: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = seeded_identity
    resolved = await _seed_entity(
        app_session_factory, workspace_id, user_id, unique=unique_message_id
    )

    response = await api.get(f"/v1/entities/{resolved.entity_id}", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["canonical_name"] == f"Person {unique_message_id}"


async def test_get_entity_404s_for_an_unknown_id(api: httpx.AsyncClient) -> None:
    response = await api.get(f"/v1/entities/{uuid.uuid4()}", headers=AUTH)
    assert response.status_code == 404

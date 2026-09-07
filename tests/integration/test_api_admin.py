"""``POST /v1/admin/khoj-sync`` (docs/DESIGN.md 7.2, docs/adr/0010)."""

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
from tc_infrastructure.db.tables import thoughts
from tests.integration.conftest import API_TOKEN

pytestmark = pytest.mark.integration

AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


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
) -> None:
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
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=f"project:api-{unique}",
                kind="project",
                title=f"Project {unique}",
                body_markdown=f"## Summary\n\n{unique} needs a working khoj sync",
                source_thought_ids=(ThoughtId(thought_id),),
                mentioned_entities=(),
                change_summary="created",
            ),
        ),
        context_selections=(),
        unorganized_thought_ids=(),
    )
    writer = PostgresOrganizeWriter(app_session_factory)
    await writer.write(
        workspace_id=WorkspaceId(workspace_id), run_id=run_id, request=request, outcome=_outcome()
    )


async def test_khoj_sync_requires_auth(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/admin/khoj-sync")
    assert response.status_code == 401


async def test_khoj_sync_is_a_no_op_for_an_empty_workspace(api_fresh: httpx.AsyncClient) -> None:
    """A real ``HttpKhojClient.index(())`` never attempts the connection at
    all, so this must succeed even though the test stack has no Khoj running
    (docs/adr/0010)."""
    response = await api_fresh.post("/v1/admin/khoj-sync", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"documents_indexed": 0}


async def test_khoj_sync_returns_503_when_khoj_is_unreachable(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = fresh_identity
    await _seed_document(app_session_factory, workspace_id, user_id, unique=unique_message_id)

    response = await api_fresh.post("/v1/admin/khoj-sync", headers=AUTH)

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")

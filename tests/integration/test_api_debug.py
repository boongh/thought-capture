"""The ``/debug/*`` HTML pages: dual auth (header or ``?token=``) and content."""

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


async def _thought_row(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    body: str,
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
                body=body,
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


async def test_thoughts_page_requires_a_token(api: httpx.AsyncClient) -> None:
    response = await api.get("/debug/thoughts")
    assert response.status_code == 401


async def test_thoughts_page_accepts_the_header_token(
    api: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = seeded_identity
    await _thought_row(
        app_session_factory,
        workspace_id,
        user_id,
        body=f"synthetic {unique_message_id}",
        source_message_id=unique_message_id,
    )

    response = await api.get("/debug/thoughts", headers=AUTH)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert f"synthetic {unique_message_id}" in response.text


async def test_thoughts_page_accepts_the_query_token(api: httpx.AsyncClient) -> None:
    response = await api.get("/debug/thoughts", params={"token": API_TOKEN})
    assert response.status_code == 200


async def test_thoughts_page_rejects_a_wrong_token(api: httpx.AsyncClient) -> None:
    response = await api.get("/debug/thoughts", params={"token": "wrong"})
    assert response.status_code == 401


async def test_a_thought_body_with_html_is_escaped_on_the_rendered_page(
    api: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = seeded_identity
    payload = f"<script>alert('{unique_message_id}')</script>"
    await _thought_row(
        app_session_factory,
        workspace_id,
        user_id,
        body=payload,
        source_message_id=unique_message_id,
    )

    response = await api.get("/debug/thoughts", headers=AUTH)

    assert payload not in response.text
    assert f"&lt;script&gt;alert(&#x27;{unique_message_id}&#x27;)&lt;/script&gt;" in response.text


async def test_entities_page_renders(api: httpx.AsyncClient) -> None:
    response = await api.get("/debug/entities", headers=AUTH)
    assert response.status_code == 200
    assert "Entities" in response.text


async def test_digests_page_renders_a_written_digest(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = fresh_identity
    thought_id = await _thought_row(
        app_session_factory,
        workspace_id,
        user_id,
        body="synthetic",
        source_message_id=unique_message_id,
    )

    ledger = PostgresRunLedger(app_session_factory)
    run_id = await ledger.start(
        WorkspaceId(workspace_id),
        window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    )
    stable_key = f"daily_digest:{unique_message_id}"
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=stable_key,
                kind="daily_digest",
                title=f"Digest {unique_message_id}",
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
    await writer.write(workspace_id=WorkspaceId(workspace_id), run_id=run_id, request=request)

    response = await api_fresh.get("/debug/digests", headers=AUTH)

    assert response.status_code == 200
    assert f"Digest {unique_message_id}" in response.text
    assert "## Summary" in response.text

"""``GET /v1/search``, against a real database (docs/DESIGN.md 10)."""

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
                body_markdown=f"## Summary\n\n{unique} needs a working search endpoint",
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
        workspace_id=WorkspaceId(workspace_id),
        run_id=run_id,
        request=request,
        outcome=_outcome(),
    )
    return result.document_ids[stable_key]


async def test_search_requires_auth(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.get("/v1/search", params={"q": "anything"})
    assert response.status_code == 401


async def test_search_finds_a_written_document_by_q(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = fresh_identity
    document_id = await _seed_document(
        app_session_factory, workspace_id, user_id, unique=unique_message_id
    )

    response = await api_fresh.get("/v1/search", headers=AUTH, params={"q": unique_message_id})

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is False
    match = next(item for item in body["items"] if item["document_id"] == str(document_id))
    assert match["result_id"] == match["revision_id"]
    assert match["channels"] == ["exact"]


async def test_search_rejects_an_unknown_mode(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.get(
        "/v1/search", headers=AUTH, params={"q": "anything", "mode": "quantum"}
    )
    assert response.status_code == 501


async def test_semantic_mode_degrades_explicitly_when_khoj_is_unreachable(
    api_fresh: httpx.AsyncClient,
) -> None:
    """No ``--profile ai`` is running in this test environment, so this
    exercises the real production degrade path (docs/DESIGN.md 7.5, 14.1),
    not a mocked one - see conftest.py's ``_api_client`` docstring."""
    response = await api_fresh.get(
        "/v1/search", headers=AUTH, params={"q": "anything", "mode": "semantic"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["items"] == []


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
async def test_a_cursor_is_rejected_for_a_mode_that_cannot_honor_it(
    api_fresh: httpx.AsyncClient, mode: str
) -> None:
    response = await api_fresh.get(
        "/v1/search",
        headers=AUTH,
        params={"q": "anything", "mode": mode, "cursor": "opaque"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["type"].endswith("cursor-unsupported-for-mode")


async def test_hybrid_mode_degrades_to_exact_only_when_khoj_is_unreachable(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = fresh_identity
    document_id = await _seed_document(
        app_session_factory, workspace_id, user_id, unique=unique_message_id
    )

    response = await api_fresh.get(
        "/v1/search", headers=AUTH, params={"q": unique_message_id, "mode": "hybrid"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    match = next(item for item in body["items"] if item["document_id"] == str(document_id))
    assert match["channels"] == ["exact"]

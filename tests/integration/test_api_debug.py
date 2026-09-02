"""The ``/debug/*`` HTML pages: Basic Auth, no-store caching, and content."""

from __future__ import annotations

import base64
import datetime as dt
import uuid

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.llm import LLMRequest, LLMResponse, LLMStep, Message
from tc_domain.organize import DocumentWrite, OrganizeWriteRequest, RunOutcome
from tc_infrastructure.db.llm_journal import PostgresLLMJournal
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.tables import thoughts
from tests.integration.conftest import API_TOKEN

pytestmark = pytest.mark.integration

AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
BODY = "## Summary\n\nsomething"


def _basic_auth(password: str, *, username: str = "operator") -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


BASIC_AUTH = _basic_auth(API_TOKEN)


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


async def test_thoughts_page_requires_credentials(api: httpx.AsyncClient) -> None:
    response = await api.get("/debug/thoughts")
    assert response.status_code == 401


async def test_debug_index_requires_credentials(api: httpx.AsyncClient) -> None:
    response = await api.get("/debug")
    assert response.status_code == 401


async def test_debug_index_redirects_to_the_default_page(api: httpx.AsyncClient) -> None:
    response = await api.get("/debug", headers=BASIC_AUTH)
    assert response.status_code == 302
    assert response.headers["location"] == "/debug/thoughts"
    assert response.headers["cache-control"] == "no-store"


async def test_a_missing_credential_prompts_the_browser_for_basic_auth(
    api: httpx.AsyncClient,
) -> None:
    """The whole point of Basic Auth here: a browser only shows its native
    login prompt when it sees ``WWW-Authenticate: Basic`` on a 401.
    """
    response = await api.get("/debug/thoughts")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].lower().startswith("basic")


async def test_thoughts_page_accepts_basic_auth_credentials(
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

    response = await api.get("/debug/thoughts", headers=BASIC_AUTH)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert f"synthetic {unique_message_id}" in response.text


async def test_the_username_is_ignored_only_the_password_is_checked(
    api: httpx.AsyncClient,
) -> None:
    response = await api.get(
        "/debug/thoughts", headers=_basic_auth(API_TOKEN, username="anything-at-all")
    )
    assert response.status_code == 200


async def test_a_bearer_header_alone_is_no_longer_accepted(api: httpx.AsyncClient) -> None:
    """/v1's Bearer scheme and /debug's Basic Auth are deliberately distinct
    (docs/adr/0009) - a Bearer header must not satisfy the debug pages.
    """
    response = await api.get("/debug/thoughts", headers=AUTH)
    assert response.status_code == 401


async def test_the_query_token_scheme_no_longer_authenticates(api: httpx.AsyncClient) -> None:
    """Superseded (docs/adr/0009): a query-param token was written into
    uvicorn's access log on every request, which no loopback-only bind
    prevents. It must not still be a valid way in.
    """
    response = await api.get("/debug/thoughts", params={"token": API_TOKEN})
    assert response.status_code == 401


async def test_thoughts_page_rejects_a_wrong_password(api: httpx.AsyncClient) -> None:
    response = await api.get("/debug/thoughts", headers=_basic_auth("wrong"))
    assert response.status_code == 401


async def test_debug_pages_are_never_cached(api: httpx.AsyncClient) -> None:
    """Personal memory content must not be retained by a browser or
    intermediary cache, the same way it must not be logged.
    """
    response = await api.get("/debug/thoughts", headers=BASIC_AUTH)
    assert response.headers["cache-control"] == "no-store"


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

    response = await api.get("/debug/thoughts", headers=BASIC_AUTH)

    assert payload not in response.text
    assert f"&lt;script&gt;alert(&#x27;{unique_message_id}&#x27;)&lt;/script&gt;" in response.text


async def test_entities_page_renders(api: httpx.AsyncClient) -> None:
    response = await api.get("/debug/entities", headers=BASIC_AUTH)
    assert response.status_code == 200
    assert "Entities" in response.text


async def test_runs_page_renders_a_journaled_call_with_its_reasoning_effort(
    api_fresh: httpx.AsyncClient,
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
) -> None:
    workspace_id, _user_id = fresh_identity
    ledger = PostgresRunLedger(app_session_factory)
    run_id = await ledger.start(
        WorkspaceId(workspace_id),
        window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    )
    model_id = f"vendor/{unique_message_id}"
    request = LLMRequest(
        step=LLMStep.ORGANIZE,
        messages=(Message(role="user", content="organize these"),),
        schema_name="OrganizationResult",
        json_schema={"type": "object"},
        prompt_version="organize-v1",
        schema_version="organize-v1",
        reasoning_effort="none",
    )
    response = LLMResponse(
        content='{"documents": []}',
        raw={"id": "gen-1", "model": model_id, "choices": [{"message": {"content": "{}"}}]},
        latency_ms=250,
        model_requested=model_id,
        model_served=model_id,
        provider="synthetic-provider",
        input_tokens=10,
        output_tokens=5,
        request_params={"reasoning_effort": "none"},
    )
    journal = PostgresLLMJournal(app_session_factory)
    await journal.record(
        workspace_id=WorkspaceId(workspace_id),
        run_id=run_id,
        request=request,
        response=response,
        sequence=1,
    )

    page_response = await api_fresh.get("/debug/runs", headers=BASIC_AUTH)

    assert page_response.status_code == 200
    assert model_id in page_response.text
    assert "<td>none</td>" in page_response.text


async def test_runs_page_shows_a_placeholder_when_there_are_no_calls(
    api_fresh: httpx.AsyncClient,
) -> None:
    response = await api_fresh.get("/debug/runs", headers=BASIC_AUTH)
    assert response.status_code == 200
    assert "No LLM calls journaled yet." in response.text


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
    await writer.write(
        workspace_id=WorkspaceId(workspace_id),
        run_id=run_id,
        request=request,
        outcome=_outcome(),
    )

    response = await api_fresh.get("/debug/digests", headers=BASIC_AUTH)

    assert response.status_code == 200
    assert f"Digest {unique_message_id}" in response.text
    assert "## Summary" in response.text

"""End-to-end semantic search: write a document -> index sync -> real Khoj
search -> hydrated back to a ``SearchResult`` (docs/DESIGN.md 7.5, 9.2).

Same rationale and placement as ``test_khoj_index_sync_contract.py``: lives
under ``tests/integration`` (needs the disposable-database fixtures) but
carries ``pytest.mark.contract`` too, so a full run's ``TC_REQUIRE_CONTRACT=1``
gate still treats a skip here as a failure. Requires both a live PostgreSQL
(``--profile core``) and a live pinned Khoj (``--profile ai``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_application.khoj_sync import DeliverKhojSync
from tc_application.search import Search
from tc_domain.capture import WorkspaceId
from tc_domain.search import SearchQuery
from tc_infrastructure.db.document_reader import PostgresDocumentReader
from tc_infrastructure.db.khoj_index_recorder import PostgresKhojIndexRecorder
from tc_infrastructure.db.khoj_sync_outbox import PostgresKhojSyncOutbox
from tc_infrastructure.db.search_reader import PostgresExactSearch
from tc_infrastructure.db.semantic_hydrator import PostgresSemanticHydrator
from tc_infrastructure.khoj.client import HttpKhojClient
from tests.integration.test_search_reader import (
    _thought_id,
    _write_document,
    unique,
    user_id,
    workspace,
)

pytestmark = [pytest.mark.integration, pytest.mark.contract]

__all__ = ["unique", "user_id", "workspace"]  # re-exported fixtures

KHOJ_BASE_URL = os.environ.get("TC_KHOJ_BASE_URL", "http://127.0.0.1:42110")


@pytest.fixture
async def khoj_http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


async def test_a_synced_document_is_returned_by_semantic_search(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
    khoj_http: httpx.AsyncClient,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, _revision_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:semantic-contract-{unique}",
        kind="project",
        title=f"Semantic contract {unique}",
        body_markdown=f"## Summary\n\nA distinctive marker: {unique}",
    )

    khoj_client = HttpKhojClient(khoj_http, KHOJ_BASE_URL)
    deliver = DeliverKhojSync(
        outbox=PostgresKhojSyncOutbox(app_session_factory, lease_owner="test"),
        source=PostgresDocumentReader(app_session_factory),
        khoj=khoj_client,
        recorder=PostgresKhojIndexRecorder(app_session_factory),
    )
    # See test_khoj_index_sync_contract.py's identical comment: the shared
    # test database accumulates sync events from every other test in the
    # session, so drain rather than assume one poll clears this document's.
    for _ in range(10):
        if await deliver() == 0:
            break

    search = Search(
        PostgresExactSearch(app_session_factory),
        khoj_client,
        PostgresSemanticHydrator(app_session_factory),
    )
    page = await search(workspace, SearchQuery(q=unique), mode="semantic")

    assert page.degraded is False
    assert any(r.document_id == document_id for r in page.items)

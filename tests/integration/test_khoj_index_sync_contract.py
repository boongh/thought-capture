"""End-to-end index sync: write a document -> outbox event -> real Khoj
(docs/DESIGN.md 6.5, 7.2 step 11). Neither ``tests/contract/khoj`` (no
PostgreSQL) nor ``test_organize_writer.py``/``test_khoj_sync_outbox.py``
(fakes/raw rows only) alone proves the full pipeline actually indexes
something searchable - this is the "integration of integrations" test the
`tests/contract/khoj/test_khoj_client.py` docstring calls "the index-sync use
case (slice 18)" depends on.

Lives under ``tests/integration`` (not ``tests/contract/khoj``) because it
needs the disposable-database fixtures from ``tests/integration/conftest.py``,
which are not shared across that directory boundary; it still carries
``pytest.mark.contract`` (not ``pytest.mark.integration`` - CI's "Integration
tests" step runs ``pytest -m integration`` *before* Khoj is started at all,
so a test needing both must opt out of that step and run only in "Contract
tests", the later step that actually starts Khoj first) so a full run's
``TC_REQUIRE_CONTRACT=1`` gate (a session-wide hook, loaded once
``tests/contract/khoj/conftest.py`` is collected at all) treats a skip here
as a failure too, same as any other contract test. Requires both a live
PostgreSQL (``--profile core``) and a live pinned Khoj (``--profile ai``,
`docs/adr/0003`).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_application.khoj_sync import DeliverKhojSync
from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.organize import DocumentWrite, OrganizeWriteRequest, RunOutcome
from tc_infrastructure.db.document_reader import PostgresDocumentReader
from tc_infrastructure.db.khoj_index_recorder import PostgresKhojIndexRecorder
from tc_infrastructure.db.khoj_sync_outbox import PostgresKhojSyncOutbox
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.tables import khoj_index_items, thoughts
from tc_infrastructure.khoj.client import HttpKhojClient

pytestmark = pytest.mark.contract

KHOJ_BASE_URL = os.environ.get("TC_KHOJ_BASE_URL", "http://127.0.0.1:42110")
BODY = (
    "## Summary\n\nsomething distinctive\n\n## Current state\n\n-\n\n"
    "## Open threads\n\n-\n\n## Timeline\n\n- x"
)


@pytest.fixture
async def khoj_http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


async def _write_one_document(
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
    request = OrganizeWriteRequest(
        documents=(
            DocumentWrite(
                stable_key=f"project:khoj-sync-contract-{unique}",
                kind="project",
                title=f"Khoj sync contract {unique}",
                body_markdown=f"{BODY} {unique}",
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
        outcome=RunOutcome(
            model_provider="offline",
            model_id="offline-model",
            prompt_version="organize-v1",
            input_tokens=0,
            output_tokens=0,
            context_recall=None,
            context_degraded=False,
        ),
    )
    return result.document_ids[f"project:khoj-sync-contract-{unique}"]


async def test_a_written_document_ends_up_searchable_in_khoj(
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    unique_message_id: str,
    khoj_http: httpx.AsyncClient,
) -> None:
    workspace_id, user_id = fresh_identity
    unique = unique_message_id.removeprefix("test-")[:12]
    document_id = await _write_one_document(
        app_session_factory, workspace_id, user_id, unique=unique
    )

    deliver = DeliverKhojSync(
        outbox=PostgresKhojSyncOutbox(app_session_factory, lease_owner="test"),
        source=PostgresDocumentReader(app_session_factory),
        khoj=HttpKhojClient(khoj_http, KHOJ_BASE_URL),
        recorder=PostgresKhojIndexRecorder(app_session_factory),
    )
    # The shared test database also accumulates khoj.sync_requested events from
    # every other test that writes a document through PostgresOrganizeWriter
    # in the same session, so this document's own event may not be in the
    # first poll's batch. Drain until the outbox reports nothing left rather
    # than assuming one call clears it - the assertions below check this
    # document specifically, so extra unrelated events being synced too is
    # harmless.
    for _ in range(10):
        if await deliver() == 0:
            break

    results = await HttpKhojClient(khoj_http, KHOJ_BASE_URL).search(unique)
    assert any(unique in r.entry for r in results)

    async with app_session_factory() as session:
        row = (
            await session.execute(
                sa.select(khoj_index_items).where(khoj_index_items.c.document_id == document_id)
            )
        ).one()
    assert row.filename.endswith(f"--{document_id}.md")
    assert len(row.body_sha256) == 64

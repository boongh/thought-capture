"""``PostgresRunReader``: read access to the ``runs`` row, for ``/status``."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.run_reader import PostgresRunReader

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace(fresh_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(fresh_identity[0])


async def test_get_returns_none_for_an_unknown_run(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> None:
    reader = PostgresRunReader(app_session_factory)
    assert await reader.get(workspace, uuid.uuid4()) is None


async def test_get_returns_the_matching_run(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> None:
    ledger = PostgresRunLedger(app_session_factory)
    reader = PostgresRunReader(app_session_factory)
    start = dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC)
    end = dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC)
    run_id = await ledger.start(workspace, window_start=start, window_end=end)
    await ledger.succeed(
        run_id,
        model_provider="offline",
        model_id="offline/deterministic",
        prompt_version="organize-v1",
        input_tokens=42,
        output_tokens=17,
        context_recall=0.75,
        context_degraded=False,
    )

    record = await reader.get(workspace, run_id)

    assert record is not None
    assert record.id == run_id
    assert record.status == "succeeded"
    assert record.kind == "organize"
    assert record.window_start == start
    assert record.window_end == end
    assert record.model_id == "offline/deterministic"
    assert record.input_tokens == 42
    assert record.output_tokens == 17
    assert record.context_recall is not None
    assert float(record.context_recall) == pytest.approx(0.75)


async def test_get_never_returns_another_workspaces_run(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    other_workspace = WorkspaceId(seeded_identity[0])
    ledger = PostgresRunLedger(app_session_factory)
    reader = PostgresRunReader(app_session_factory)
    run_id = await ledger.start(
        workspace,
        window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    )

    assert await reader.get(other_workspace, run_id) is None


async def test_most_recent_returns_the_latest_run(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> None:
    ledger = PostgresRunLedger(app_session_factory)
    reader = PostgresRunReader(app_session_factory)
    first = await ledger.start(
        workspace,
        window_start=dt.datetime(2026, 8, 29, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
    )
    await ledger.fail(first, error_code="LLMError", error_detail="provider call failed")
    second = await ledger.start(
        workspace,
        window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    )

    record = await reader.most_recent(workspace)

    assert record is not None
    assert record.id == second
    assert record.status == "running"


async def test_most_recent_returns_none_when_the_workspace_has_no_runs(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    # A workspace with existing seed data but no organize runs yet.
    reader = PostgresRunReader(app_session_factory)
    fresh = WorkspaceId(uuid.uuid4())
    assert await reader.most_recent(fresh) is None

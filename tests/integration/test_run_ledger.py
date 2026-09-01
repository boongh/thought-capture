"""The ``runs`` row lifecycle, as the least-privilege application role."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.tables import runs

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace(fresh_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(fresh_identity[0])


async def test_start_creates_a_running_row(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> None:
    ledger = PostgresRunLedger(app_session_factory)
    start = dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC)
    end = dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC)

    run_id = await ledger.start(workspace, window_start=start, window_end=end)

    async with app_session_factory() as session:
        row = (await session.execute(sa.select(runs).where(runs.c.id == run_id))).one()
    assert row.status == "running"
    assert row.kind == "organize"
    assert row.window_start == start
    assert row.window_end == end
    assert row.started_at is not None


async def test_succeed_records_usage_and_recall(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> None:
    ledger = PostgresRunLedger(app_session_factory)
    run_id = await ledger.start(
        workspace,
        window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    )

    await ledger.succeed(
        run_id,
        model_provider="offline",
        model_id="offline/deterministic",
        prompt_version="organize-v1",
        input_tokens=42,
        output_tokens=17,
        context_recall=0.75,
        context_degraded=True,
    )

    async with app_session_factory() as session:
        row = (await session.execute(sa.select(runs).where(runs.c.id == run_id))).one()
    assert row.status == "succeeded"
    assert row.finished_at is not None
    assert row.model_provider == "offline"
    assert row.input_tokens == 42
    assert row.output_tokens == 17
    assert float(row.context_recall) == pytest.approx(0.75)
    assert row.context_degraded is True


async def test_fail_records_a_sanitized_error(
    app_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> None:
    ledger = PostgresRunLedger(app_session_factory)
    run_id = await ledger.start(
        workspace,
        window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    )

    await ledger.fail(run_id, error_code="OrganizeCoverageError", error_detail="1 thought missing")

    async with app_session_factory() as session:
        row = (await session.execute(sa.select(runs).where(runs.c.id == run_id))).one()
    assert row.status == "failed"
    assert row.error_code == "OrganizeCoverageError"
    assert row.finished_at is not None

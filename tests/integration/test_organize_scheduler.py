"""Scheduling and catch-up behaviour.

The behaviours under test are the ones that decide whether a day's thoughts ever
become a digest: catch-up must process every missed window oldest-first, a
failure must not abandon the remaining days, and two workers must not both
organize the same window (docs/DESIGN.md 4.2, 13).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.windows import CaptureWindow
from tc_infrastructure.db.tables import capture_windows, runs
from tc_infrastructure.db.windows import PostgresCaptureWindows
from tc_worker.scheduler import OrganizeScheduler

pytestmark = pytest.mark.integration

CUTOFF = dt.time(20, 0)
BANGKOK = "Asia/Bangkok"


def utc(year: int, month: int, day: int, hour: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, tzinfo=dt.UTC)


@pytest.fixture
def workspace(seeded_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(seeded_identity[0])


@pytest.fixture
async def clean_windows(
    admin_session_factory: async_sessionmaker[AsyncSession], workspace: WorkspaceId
) -> None:
    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.delete(capture_windows).where(capture_windows.c.workspace_id == workspace)
        )


class RecordingPipeline:
    """Stands in for the organize pipeline, which arrives in a later slice."""

    def __init__(self, *, fail_on: dt.datetime | None = None) -> None:
        self.organized: list[CaptureWindow] = []
        self.fail_on = fail_on

    async def __call__(self, workspace_id: WorkspaceId, window: CaptureWindow) -> None:
        if self.fail_on is not None and window.end == self.fail_on:
            raise RuntimeError("synthetic pipeline failure")
        self.organized.append(window)


def make_scheduler(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    pipeline: RecordingPipeline,
    *,
    now: dt.datetime,
) -> OrganizeScheduler:
    return OrganizeScheduler(
        windows=PostgresCaptureWindows(app_session_factory),
        organize=pipeline,
        workspace_id=workspace,
        digest_local_time=CUTOFF,
        timezone=BANGKOK,
        clock=lambda: now,
    )


# ---------------------------------------------------------------------------
# Catch-up
# ---------------------------------------------------------------------------


async def test_catch_up_organizes_every_missed_window_oldest_first(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """Three days offline produces three digests, in the order they were lived."""
    pipeline = RecordingPipeline()
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))

    organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 28, 13))

    assert organized == 3
    assert [w.end for w in pipeline.organized] == [
        utc(2026, 8, 29, 13),
        utc(2026, 8, 30, 13),
        utc(2026, 8, 31, 13),
    ]


async def test_catch_up_does_nothing_when_no_window_has_closed(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    pipeline = RecordingPipeline()
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 12))

    organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 31, 6))

    assert organized == 0
    assert pipeline.organized == []


async def test_a_failing_window_does_not_abandon_the_others(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """A provider outage on one day must not cost the other days their digests."""
    pipeline = RecordingPipeline(fail_on=utc(2026, 8, 30, 13))
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))

    organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 28, 13))

    assert organized == 2
    assert [w.end for w in pipeline.organized] == [
        utc(2026, 8, 29, 13),
        utc(2026, 8, 31, 13),
    ]


async def test_a_failed_window_is_retried_on_the_next_catch_up(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """It stays unorganized, which is what makes the retry automatic."""
    failing = RecordingPipeline(fail_on=utc(2026, 8, 30, 13))
    scheduler = make_scheduler(app_session_factory, workspace, failing, now=utc(2026, 8, 31, 14))
    await scheduler.catch_up(first_capture_at=utc(2026, 8, 28, 13))

    recovered = RecordingPipeline()
    retry = make_scheduler(app_session_factory, workspace, recovered, now=utc(2026, 8, 31, 14))
    organized = await retry.catch_up(first_capture_at=utc(2026, 8, 28, 13))

    assert utc(2026, 8, 30, 13) in [w.end for w in recovered.organized]
    assert organized >= 1


async def test_an_already_organized_window_is_not_organized_twice(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """Restarting the worker must not resend yesterday's digest."""
    pipeline = RecordingPipeline()
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))
    await scheduler.catch_up(first_capture_at=utc(2026, 8, 30, 13))
    first_pass = len(pipeline.organized)

    # Mark them organized the way the real pipeline would on success.
    run_id = uuid.uuid4()
    async with app_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=run_id, workspace_id=workspace, kind="organize", status="succeeded"
            )
        )
    store = PostgresCaptureWindows(app_session_factory)
    for window in pipeline.organized:
        await store.mark_organized(workspace, window, run_id)

    again = RecordingPipeline()
    restarted = make_scheduler(app_session_factory, workspace, again, now=utc(2026, 8, 31, 14))
    organized = await restarted.catch_up(first_capture_at=utc(2026, 8, 30, 13))

    assert first_pass >= 1
    assert organized == 0
    assert again.organized == []


async def test_a_contended_window_is_skipped_rather_than_waited_on(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """Two replicas after a restart must not both organize the same day."""
    window = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))
    holder = PostgresCaptureWindows(app_session_factory)
    pipeline = RecordingPipeline()
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))

    async with holder.locked(workspace, window) as held:
        assert held is True
        organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 30, 13))

    assert organized == 0
    assert pipeline.organized == []


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


async def test_the_next_run_is_armed_for_the_next_cutoff(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    pipeline = RecordingPipeline()
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))

    fire_at = scheduler.schedule_next()

    assert fire_at == utc(2026, 9, 1, 13)


async def test_arming_before_the_cutoff_targets_today(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    """A worker started at breakfast must fire this evening, not tomorrow."""
    pipeline = RecordingPipeline()
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 6))

    assert scheduler.schedule_next() == utc(2026, 8, 31, 13)

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
    """Clears this workspace's windows, and the runs this file's own tests attach to them.

    ``workspace`` is a session-scoped fixture shared by every integration test,
    and this file's tests all reuse the same handful of synthetic dates. A run
    left behind by one test - with the exact window bounds another test also
    uses - would otherwise be picked up by ``succeeded_run_for``'s resume
    check, silently skipping that later test's own pipeline invocation.
    Scoped to ``window_start IS NOT NULL`` because that is what only this
    file's own runs (and ``test_capture_window_store.py``'s) ever set; other
    files' leftover runs for this workspace carry no window bounds and must
    not be touched here, since some already have journal rows attached.
    """
    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.delete(capture_windows).where(capture_windows.c.workspace_id == workspace)
        )
        await session.execute(
            sa.delete(runs).where(
                runs.c.workspace_id == workspace, runs.c.window_start.is_not(None)
            )
        )


class RecordingPipeline:
    """Stands in for the organize pipeline, which arrives in a later slice.

    It creates a real ``runs`` row, because that is the pipeline's job: the
    scheduler records window completion against the run the pipeline returns,
    and the composite foreign key requires it to exist in the same workspace.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        fail_on: dt.datetime | None = None,
    ) -> None:
        self._factory = factory
        self.organized: list[CaptureWindow] = []
        self.run_ids: list[uuid.UUID] = []
        self.fail_on = fail_on

    async def __call__(self, workspace_id: WorkspaceId, window: CaptureWindow) -> uuid.UUID:
        if self.fail_on is not None and window.end == self.fail_on:
            raise RuntimeError("synthetic pipeline failure")

        run_id = uuid.uuid4()
        async with self._factory() as session, session.begin():
            await session.execute(
                sa.insert(runs).values(
                    id=run_id,
                    workspace_id=workspace_id,
                    kind="organize",
                    status="succeeded",
                    window_start=window.start,
                    window_end=window.end,
                )
            )
        self.organized.append(window)
        self.run_ids.append(run_id)
        return run_id


def make_scheduler(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    pipeline: RecordingPipeline,
    *,
    now: dt.datetime,
    first_capture_at: dt.datetime | None = None,
) -> OrganizeScheduler:
    async def first_capture(_: WorkspaceId) -> dt.datetime | None:
        return first_capture_at

    return OrganizeScheduler(
        windows=PostgresCaptureWindows(app_session_factory),
        organize=pipeline,
        first_capture=first_capture,
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
    pipeline = RecordingPipeline(app_session_factory)
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))

    organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 29, 6))

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
    pipeline = RecordingPipeline(app_session_factory)
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
    pipeline = RecordingPipeline(app_session_factory, fail_on=utc(2026, 8, 30, 13))
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))

    organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 29, 6))

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
    failing = RecordingPipeline(app_session_factory, fail_on=utc(2026, 8, 30, 13))
    scheduler = make_scheduler(app_session_factory, workspace, failing, now=utc(2026, 8, 31, 14))
    await scheduler.catch_up(first_capture_at=utc(2026, 8, 29, 6))

    recovered = RecordingPipeline(app_session_factory)
    retry = make_scheduler(app_session_factory, workspace, recovered, now=utc(2026, 8, 31, 14))
    organized = await retry.catch_up(first_capture_at=utc(2026, 8, 29, 6))

    assert utc(2026, 8, 30, 13) in [w.end for w in recovered.organized]
    assert organized >= 1


async def test_a_failed_window_is_still_offered_after_a_later_one_succeeds(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """The bug this replaces silently lost a day's digest forever.

    ``MAX(window_end)`` jumps past a failure: with 30 August failed and 31
    August organized, the anchor became 31 August and 30 August was never
    offered again. The contiguous frontier stops at the gap instead.

    The earlier retry test could not catch this, because its pipeline never
    persisted a successful window.
    """
    failing = RecordingPipeline(app_session_factory, fail_on=utc(2026, 8, 30, 13))
    first_pass = make_scheduler(app_session_factory, workspace, failing, now=utc(2026, 8, 31, 14))
    organized = await first_pass.catch_up(first_capture_at=utc(2026, 8, 29, 6))

    # 29 and 31 succeeded and are durably marked; 30 failed.
    assert organized == 2
    assert utc(2026, 8, 31, 13) in [w.end for w in failing.organized]

    recovered = RecordingPipeline(app_session_factory)
    second_pass = make_scheduler(
        app_session_factory, workspace, recovered, now=utc(2026, 8, 31, 14)
    )
    await second_pass.catch_up(first_capture_at=utc(2026, 8, 29, 6))

    assert utc(2026, 8, 30, 13) in [w.end for w in recovered.organized], (
        "the failed middle window was skipped permanently"
    )


async def test_a_failed_first_window_is_still_offered_after_a_later_one_succeeds(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """The gap the earlier fix could not catch: no organized window exists yet.

    ``test_a_failed_window_is_still_offered_after_a_later_one_succeeds`` fails a
    *middle* window with an earlier organized one already on the frontier. Here
    the very *first* due window is the one that fails, so ``organized_frontier``
    has no earlier row to compare the next success against - the bug this
    guards against returned the later success's own end as the frontier,
    skipping the first window forever.
    """
    failing = RecordingPipeline(app_session_factory, fail_on=utc(2026, 8, 29, 13))
    first_pass = make_scheduler(app_session_factory, workspace, failing, now=utc(2026, 8, 31, 14))
    organized = await first_pass.catch_up(first_capture_at=utc(2026, 8, 29, 6))

    # 30 and 31 succeeded; 29, the first due window, failed.
    assert organized == 2
    assert utc(2026, 8, 29, 13) not in [w.end for w in failing.organized]

    recovered = RecordingPipeline(app_session_factory)
    second_pass = make_scheduler(
        app_session_factory, workspace, recovered, now=utc(2026, 8, 31, 14)
    )
    await second_pass.catch_up(first_capture_at=utc(2026, 8, 29, 6))

    assert utc(2026, 8, 29, 13) in [w.end for w in recovered.organized], (
        "the failed first window was skipped permanently"
    )


async def test_a_window_completed_by_another_worker_is_not_redone(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """The stale due-list race: compute work, then lose it before locking.

    A worker can build its due list, wait on the advisory lock while another
    worker finishes the same window, then acquire the lock. Claiming
    unconditionally would regress the organized row and produce a second
    digest.
    """
    first = RecordingPipeline(app_session_factory)
    winner = make_scheduler(app_session_factory, workspace, first, now=utc(2026, 8, 31, 14))
    await winner.catch_up(first_capture_at=utc(2026, 8, 31, 6))
    assert len(first.organized) == 1

    # A second worker whose due list was computed before the first finished.
    second = RecordingPipeline(app_session_factory)
    loser = make_scheduler(app_session_factory, workspace, second, now=utc(2026, 8, 31, 14))
    organized = await loser.catch_up(first_capture_at=utc(2026, 8, 31, 6))

    assert organized == 0
    assert second.organized == []


async def test_a_committed_run_is_resumed_rather_than_redone(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """A crash between the callback committing and completion being marked
    must resume, not re-invoke the callback and duplicate its output.

    Simulates the crash directly: a ``runs`` row for this exact window is
    already committed as ``succeeded`` - exactly what the callback's contract
    requires before returning - but the window was never marked organized. A
    callback that raises if invoked proves the scheduler resumed instead of
    redoing the work.
    """
    window = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))
    existing_run_id = uuid.uuid4()
    async with app_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=existing_run_id,
                workspace_id=workspace,
                kind="organize",
                status="succeeded",
                window_start=window.start,
                window_end=window.end,
            )
        )

    async def exploding_pipeline(workspace_id: WorkspaceId, w: CaptureWindow) -> uuid.UUID:
        raise AssertionError("the callback must not be re-invoked for an already-completed window")

    scheduler = make_scheduler(
        app_session_factory,
        workspace,
        exploding_pipeline,
        now=utc(2026, 8, 31, 14),  # type: ignore[arg-type]
    )

    organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 31, 6))

    assert organized == 1
    store = PostgresCaptureWindows(app_session_factory)
    assert await store.organized_frontier(workspace, floor=window.start) == window.end

    async with app_session_factory() as session:
        run_ids = (
            (
                await session.execute(
                    sa.select(runs.c.id).where(
                        runs.c.workspace_id == workspace,
                        runs.c.window_start == window.start,
                        runs.c.window_end == window.end,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert run_ids == [existing_run_id], "resuming must not create a duplicate run"


async def test_a_scheduled_run_finds_work_without_being_told_the_first_capture(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """A new workspace's first scheduled digest used to never run.

    The scheduled path passed ``first_capture_at=None``, and with no previous
    success there was nothing to anchor on, so every cutoff did nothing.
    """
    pipeline = RecordingPipeline(app_session_factory)
    scheduler = make_scheduler(
        app_session_factory,
        workspace,
        pipeline,
        now=utc(2026, 8, 31, 14),
        first_capture_at=utc(2026, 8, 31, 6),
    )

    organized = await scheduler.catch_up()

    assert organized == 1
    assert [w.end for w in pipeline.organized] == [utc(2026, 8, 31, 13)]


async def test_a_capture_before_its_own_cutoff_is_not_skipped(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """A window is (start, end], so anchoring on the capture excluded it.

    A first capture at 13:00 Bangkok on 31 August, with a run at 21:00 local,
    used to return no windows at all - the thought that started the workspace
    belonged to none.
    """
    pipeline = RecordingPipeline(app_session_factory)
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))

    organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 31, 6))

    assert organized == 1
    assert pipeline.organized[0].contains(utc(2026, 8, 31, 6))


async def test_an_already_organized_window_is_not_organized_twice(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """Restarting the worker must not resend yesterday's digest."""
    pipeline = RecordingPipeline(app_session_factory)
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))
    await scheduler.catch_up(first_capture_at=utc(2026, 8, 31, 6))
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

    again = RecordingPipeline(app_session_factory)
    restarted = make_scheduler(app_session_factory, workspace, again, now=utc(2026, 8, 31, 14))
    organized = await restarted.catch_up(first_capture_at=utc(2026, 8, 31, 6))

    assert first_pass >= 1
    assert organized == 0
    assert again.organized == []


async def test_a_completion_marking_failure_does_not_abandon_the_rest_of_the_batch(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB hiccup marking one window organized must not cost the other days.

    The exception used to be caught only around the pipeline callback; a
    failure in ``claim`` or ``mark_organized`` propagated out of
    ``_organize_window`` and aborted whatever remained of the due list for
    that ``catch_up`` call.
    """
    pipeline = RecordingPipeline(app_session_factory)
    windows = PostgresCaptureWindows(app_session_factory)
    real_mark_organized = windows.mark_organized

    async def flaky_mark_organized(
        workspace_id: WorkspaceId, window: CaptureWindow, run_id: uuid.UUID
    ) -> None:
        if window.end == utc(2026, 8, 30, 13):
            raise RuntimeError("synthetic completion-marking failure")
        await real_mark_organized(workspace_id, window, run_id)

    monkeypatch.setattr(windows, "mark_organized", flaky_mark_organized)

    async def first_capture(_: WorkspaceId) -> dt.datetime | None:
        return utc(2026, 8, 29, 6)

    scheduler = OrganizeScheduler(
        windows=windows,
        organize=pipeline,
        first_capture=first_capture,
        workspace_id=workspace,
        digest_local_time=CUTOFF,
        timezone=BANGKOK,
        clock=lambda: utc(2026, 8, 31, 14),
    )

    organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 29, 6))

    assert organized == 2, "the window whose completion could not be marked must not count"
    assert [w.end for w in pipeline.organized] == [
        utc(2026, 8, 29, 13),
        utc(2026, 8, 30, 13),
        utc(2026, 8, 31, 13),
    ], "31 August must still be attempted even though 30 August's marking failed"


async def test_a_contended_window_is_skipped_rather_than_waited_on(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    clean_windows: None,
) -> None:
    """Two replicas after a restart must not both organize the same day."""
    window = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))
    holder = PostgresCaptureWindows(app_session_factory)
    pipeline = RecordingPipeline(app_session_factory)
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))

    async with holder.locked(workspace, window) as held:
        assert held is True
        organized = await scheduler.catch_up(first_capture_at=utc(2026, 8, 31, 6))

    assert organized == 0
    assert pipeline.organized == []


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


async def test_the_next_run_is_armed_for_the_next_cutoff(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    pipeline = RecordingPipeline(app_session_factory)
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 14))

    fire_at = scheduler.schedule_next()

    assert fire_at == utc(2026, 9, 1, 13)


async def test_arming_before_the_cutoff_targets_today(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    """A worker started at breakfast must fire this evening, not tomorrow."""
    pipeline = RecordingPipeline(app_session_factory)
    scheduler = make_scheduler(app_session_factory, workspace, pipeline, now=utc(2026, 8, 31, 6))

    assert scheduler.schedule_next() == utc(2026, 8, 31, 13)


async def test_re_arming_happens_even_when_catch_up_blows_up(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due-list failure must not permanently stop the scheduler.

    ``catch_up`` is already resilient to any single window's failure, but a
    failure outside any one window - the first-capture lookup, say - used to
    propagate out of ``_run_scheduled`` before ``schedule_next`` ran, leaving
    the one-shot scheduler unarmed until the process restarted.
    """
    pipeline = RecordingPipeline(app_session_factory)

    async def exploding_first_capture(_: WorkspaceId) -> dt.datetime | None:
        raise RuntimeError("synthetic first-capture lookup failure")

    scheduler = OrganizeScheduler(
        windows=PostgresCaptureWindows(app_session_factory),
        organize=pipeline,
        first_capture=exploding_first_capture,
        workspace_id=workspace,
        digest_local_time=CUTOFF,
        timezone=BANGKOK,
        clock=lambda: utc(2026, 8, 31, 14),
    )
    rearmed = []
    monkeypatch.setattr(scheduler, "schedule_next", lambda: rearmed.append(True))

    await scheduler._run_scheduled()

    assert rearmed == [True], "the scheduler must re-arm even after catch_up fails"

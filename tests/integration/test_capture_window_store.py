"""Capture window persistence, catch-up selection, and the advisory lock.

The acceptance checklist requires that catch-up produces exactly one canonical
result per closed window (docs/DESIGN.md Appendix A). Two properties deliver
that: an organized window is never offered again, and two workers cannot hold
the same window at once.
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
from tc_infrastructure.db.windows import PostgresCaptureWindows, advisory_key

pytestmark = pytest.mark.integration

CUTOFF = dt.time(20, 0)
BANGKOK = "Asia/Bangkok"


def utc(year: int, month: int, day: int, hour: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, tzinfo=dt.UTC)


@pytest.fixture
def workspace(seeded_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(seeded_identity[0])


@pytest.fixture
def store(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> PostgresCaptureWindows:
    return PostgresCaptureWindows(app_session_factory)


async def a_run(factory: async_sessionmaker[AsyncSession], workspace_id: WorkspaceId) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=run_id, workspace_id=workspace_id, kind="organize", status="succeeded"
            )
        )
    return run_id


async def clear_windows(
    factory: async_sessionmaker[AsyncSession], workspace_id: WorkspaceId
) -> None:
    """Reset window state between tests.

    Uses the *migration* role: since migration 0004 the application role holds
    SELECT, INSERT, and UPDATE on capture_windows but deliberately not DELETE,
    because the running system never removes a window.
    """
    async with factory() as session, session.begin():
        await session.execute(
            sa.delete(capture_windows).where(capture_windows.c.workspace_id == workspace_id)
        )


# ---------------------------------------------------------------------------
# Advisory key
# ---------------------------------------------------------------------------


def test_the_advisory_key_is_deterministic_and_specific() -> None:
    workspace = WorkspaceId(uuid.uuid4())
    other = WorkspaceId(uuid.uuid4())
    end = utc(2026, 8, 31, 13)

    assert advisory_key(workspace, end) == advisory_key(workspace, end)
    assert advisory_key(workspace, end) != advisory_key(other, end)
    assert advisory_key(workspace, end) != advisory_key(workspace, utc(2026, 8, 30, 13))


def test_the_advisory_key_fits_a_signed_bigint() -> None:
    """Postgres advisory keys are signed; a negative or oversized key errors."""
    key = advisory_key(WorkspaceId(uuid.uuid4()), utc(2026, 8, 31, 13))
    assert 0 <= key <= 0x7FFF_FFFF_FFFF_FFFF


def test_the_advisory_key_ignores_how_the_instant_is_expressed() -> None:
    """The same moment written in two zones must lock the same window."""
    from zoneinfo import ZoneInfo

    workspace = WorkspaceId(uuid.uuid4())
    as_utc = utc(2026, 8, 31, 13)
    as_bangkok = as_utc.astimezone(ZoneInfo(BANGKOK))

    assert advisory_key(workspace, as_utc) == advisory_key(workspace, as_bangkok)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


async def test_recording_a_window_is_idempotent(
    store: PostgresCaptureWindows,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await clear_windows(admin_session_factory, workspace)
    window = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))

    await store.record(workspace, window, status="closed")
    await store.record(workspace, window, status="closed")

    async with app_session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(capture_windows)
            .where(capture_windows.c.workspace_id == workspace)
        )
    assert count == 1


async def test_marking_organized_records_the_run(
    store: PostgresCaptureWindows,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await clear_windows(admin_session_factory, workspace)
    window = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))
    run_id = await a_run(app_session_factory, workspace)

    await store.record(workspace, window, status="closed")
    await store.mark_organized(workspace, window, run_id)

    assert await store.organized_frontier(workspace) == window.end


# ---------------------------------------------------------------------------
# Catch-up selection
# ---------------------------------------------------------------------------


async def test_a_new_workspace_with_no_captures_has_nothing_due(
    store: PostgresCaptureWindows,
    workspace: WorkspaceId,
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without this, an empty workspace would enumerate windows back to the epoch."""
    await clear_windows(admin_session_factory, workspace)

    due = await store.due_windows(
        workspace,
        now=utc(2026, 8, 31, 14),
        digest_local_time=CUTOFF,
        timezone=BANGKOK,
        first_capture_at=None,
    )
    assert due == []


async def test_catch_up_offers_every_missed_window_oldest_first(
    store: PostgresCaptureWindows,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await clear_windows(admin_session_factory, workspace)

    due = await store.due_windows(
        workspace,
        now=utc(2026, 8, 31, 14),
        digest_local_time=CUTOFF,
        timezone=BANGKOK,
        first_capture_at=utc(2026, 8, 29, 6),
    )

    assert [w.end for w in due] == [
        utc(2026, 8, 29, 13),
        utc(2026, 8, 30, 13),
        utc(2026, 8, 31, 13),
    ]


async def test_an_organized_window_is_never_offered_again(
    store: PostgresCaptureWindows,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ "Exactly one canonical result per closed window" depends on this."""
    await clear_windows(admin_session_factory, workspace)
    run_id = await a_run(app_session_factory, workspace)
    first = CaptureWindow(start=utc(2026, 8, 28, 13), end=utc(2026, 8, 29, 13))

    await store.mark_organized(workspace, first, run_id)

    due = await store.due_windows(
        workspace,
        now=utc(2026, 8, 31, 14),
        digest_local_time=CUTOFF,
        timezone=BANGKOK,
        first_capture_at=utc(2026, 8, 29, 6),
    )

    assert first.end not in [w.end for w in due]
    assert [w.end for w in due] == [utc(2026, 8, 30, 13), utc(2026, 8, 31, 13)]


async def test_nothing_is_due_before_the_next_cutoff(
    store: PostgresCaptureWindows,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await clear_windows(admin_session_factory, workspace)
    run_id = await a_run(app_session_factory, workspace)
    window = CaptureWindow(start=utc(2026, 8, 29, 13), end=utc(2026, 8, 30, 13))
    await store.mark_organized(workspace, window, run_id)

    due = await store.due_windows(
        workspace,
        now=utc(2026, 8, 31, 12),  # before today's 13:00 UTC cutoff
        digest_local_time=CUTOFF,
        timezone=BANGKOK,
        first_capture_at=utc(2026, 8, 29, 6),
    )
    assert due == []


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


async def test_one_worker_holds_a_window_at_a_time(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    """Two replicas after a restart must not both organize the same day."""
    window = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))
    first = PostgresCaptureWindows(app_session_factory)
    second = PostgresCaptureWindows(app_session_factory)

    async with first.locked(workspace, window) as got_first:
        assert got_first is True
        async with second.locked(workspace, window) as got_second:
            assert got_second is False, "a second worker acquired the same window"


async def test_the_lock_is_released_when_the_block_exits(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    """Transaction-scoped, so a crash cannot strand the window forever."""
    window = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))
    store = PostgresCaptureWindows(app_session_factory)

    async with store.locked(workspace, window) as acquired:
        assert acquired is True

    async with store.locked(workspace, window) as reacquired:
        assert reacquired is True


async def test_different_windows_do_not_contend(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    """Catch-up processes several windows; they must not serialise on each other."""
    earlier = CaptureWindow(start=utc(2026, 8, 29, 13), end=utc(2026, 8, 30, 13))
    later = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))
    store = PostgresCaptureWindows(app_session_factory)
    other = PostgresCaptureWindows(app_session_factory)

    async with store.locked(workspace, earlier) as first:
        assert first is True
        async with other.locked(workspace, later) as second:
            assert second is True

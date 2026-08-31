"""Capture window persistence and the advisory lock that serialises organizing.

The lock matters: "The worker uses PostgreSQL advisory locks so multiple
replicas cannot organize the same window" (docs/DESIGN.md 13). Without it, two
workers starting after a restart would each produce a digest for the same day.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.windows import CaptureWindow, previous_cutoff_before, windows_since
from tc_infrastructure.db.tables import capture_windows, runs

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StoredWindow:
    window: CaptureWindow
    status: str
    organized_by_run_id: uuid.UUID | None


def advisory_key(workspace_id: WorkspaceId, window_end: dt.datetime) -> int:
    """A stable 63-bit key for ``(workspace, window)``.

    Advisory locks share one global bigint namespace across the whole database,
    so a key must be derived deterministically and specifically enough that two
    unrelated features cannot collide on it.
    """
    material = f"organize:{workspace_id}:{window_end.astimezone(dt.UTC).isoformat()}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    # Postgres advisory keys are signed bigints; mask to 63 bits to stay positive.
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


class PostgresCaptureWindows:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def organized_frontier(
        self, workspace_id: WorkspaceId, *, floor: dt.datetime
    ) -> dt.datetime:
        """The end of the newest *contiguously* organized window, anchored at ``floor``.

        ``floor`` is the cutoff the very first window must start at - the cutoff
        before this workspace's first capture. Deliberately not ``MAX(window_end)``
        and deliberately not "whatever the earliest *organized* row happens to
        be": if 30 August fails and 31 August then succeeds, the maximum jumps
        past the failure and 30 August is never offered again - a day's thoughts
        silently never become a digest. The walk starts at ``floor`` and stops at
        the first gap, so a failed window keeps being retried until it succeeds
        - including when the very *first* window is the one that failed, which a
        walk anchored on the first organized row (rather than on ``floor``)
        cannot detect, because there is nothing earlier to compare it against.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(capture_windows.c.window_start, capture_windows.c.window_end)
                    .where(
                        capture_windows.c.workspace_id == workspace_id,
                        capture_windows.c.status == "organized",
                        capture_windows.c.window_start >= floor,
                    )
                    .order_by(capture_windows.c.window_end)
                )
            ).all()

        frontier = floor
        for row in rows:
            if row.window_start != frontier:
                # A gap: an earlier window is still unorganized.
                break
            frontier = row.window_end
        return frontier

    async def due_windows(
        self,
        workspace_id: WorkspaceId,
        *,
        now: dt.datetime,
        digest_local_time: dt.time,
        timezone: str,
        first_capture_at: dt.datetime | None,
    ) -> list[CaptureWindow]:
        """Closed windows that still need organizing, oldest first.

        Anchored on the contiguously organized frontier, which itself starts at
        the cutoff before the first capture - so that a brand-new workspace does
        not enumerate windows reaching back to the epoch, and so that a first
        window which failed to organize is never silently skipped.
        """
        if first_capture_at is None:
            return []
        # The *cutoff before* the first capture, not the capture instant. A
        # window is (start, end], so anchoring on the capture itself would
        # exclude that very thought - and starting mid-day would skip its own
        # cutoff entirely, because enumeration steps to the next local date.
        floor = previous_cutoff_before(first_capture_at, digest_local_time, timezone)
        anchor = await self.organized_frontier(workspace_id, floor=floor)

        already = await self._organized_ends(workspace_id)
        return [
            window
            for window in windows_since(anchor, now, digest_local_time, timezone)
            if window.end not in already
        ]

    async def _organized_ends(self, workspace_id: WorkspaceId) -> set[dt.datetime]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(capture_windows.c.window_end).where(
                    capture_windows.c.workspace_id == workspace_id,
                    capture_windows.c.status == "organized",
                )
            )
        return {row[0] for row in rows}

    async def claim(self, workspace_id: WorkspaceId, window: CaptureWindow) -> bool:
        """Mark the window in progress, unless it is already organized.

        Returns False when another worker completed it first. The check happens
        *inside* the write rather than before it: a worker can compute its due
        list, wait on the advisory lock while another worker finishes the same
        window, and then acquire the lock afterwards. Overwriting the status
        unconditionally would regress an organized row back to closed and run
        the pipeline a second time, producing a duplicate digest.
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                pg_insert(capture_windows)
                .values(
                    workspace_id=workspace_id,
                    window_start=window.start,
                    window_end=window.end,
                    status="closed",
                )
                .on_conflict_do_update(
                    index_elements=["workspace_id", "window_end"],
                    set_={"status": "closed"},
                    where=capture_windows.c.status != "organized",
                )
                .returning(capture_windows.c.window_end)
            )
            claimed = result.scalar_one_or_none() is not None

        if not claimed:
            logger.info("window.already_organized", extra={"window_end": window.end.isoformat()})
        return claimed

    async def record(
        self, workspace_id: WorkspaceId, window: CaptureWindow, *, status: str
    ) -> None:
        """Upsert the window row. Never downgrades an organized window."""
        async with self._session_factory() as session, session.begin():
            await session.execute(
                pg_insert(capture_windows)
                .values(
                    workspace_id=workspace_id,
                    window_start=window.start,
                    window_end=window.end,
                    status=status,
                )
                .on_conflict_do_update(
                    index_elements=["workspace_id", "window_end"],
                    set_={"status": status},
                    where=capture_windows.c.status != "organized",
                )
            )

    async def succeeded_run_for(
        self, workspace_id: WorkspaceId, window: CaptureWindow
    ) -> uuid.UUID | None:
        """A prior succeeded organize run already committed for this exact window.

        The resume half of a durable window-keyed protocol. The pipeline
        callback commits its ``runs`` row as its very last step, and
        ``mark_organized`` commits the window's completion separately - a
        crash between the two leaves the window ``closed`` even though the
        run, and everything the callback wrote under it, already exists and
        is complete. Without this lookup, catch-up would call the callback
        again for the same window and produce a second, duplicate set of
        derived output. Finding the existing run here lets the caller resume
        by marking completion instead of redoing the work.
        """
        async with self._session_factory() as session:
            run_id = await session.scalar(
                sa.select(runs.c.id)
                .where(
                    runs.c.workspace_id == workspace_id,
                    runs.c.kind == "organize",
                    runs.c.window_start == window.start,
                    runs.c.window_end == window.end,
                    runs.c.status == "succeeded",
                )
                .order_by(runs.c.created_at.desc())
                .limit(1)
            )
        return run_id

    async def mark_organized(
        self, workspace_id: WorkspaceId, window: CaptureWindow, run_id: uuid.UUID
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                pg_insert(capture_windows)
                .values(
                    workspace_id=workspace_id,
                    window_start=window.start,
                    window_end=window.end,
                    status="organized",
                    organized_by_run_id=run_id,
                )
                .on_conflict_do_update(
                    index_elements=["workspace_id", "window_end"],
                    set_={"status": "organized", "organized_by_run_id": run_id},
                )
            )
        logger.info(
            "window.organized",
            extra={"window_end": window.end.isoformat(), "run_id": str(run_id)},
        )

    @asynccontextmanager
    async def locked(self, workspace_id: WorkspaceId, window: CaptureWindow) -> AsyncIterator[bool]:
        """Hold a transaction-scoped advisory lock for one window.

        Yields False when another worker already holds it, so the caller skips
        rather than blocks: a second worker waiting to redo work that is already
        being done is worse than a second worker doing nothing.

        Transaction-scoped (``pg_try_advisory_xact_lock``) rather than
        session-scoped, because a pooled connection can be returned to the pool
        while a session-scoped lock is still held, leaking it until the
        connection is recycled.
        """
        key = advisory_key(workspace_id, window.end)
        async with self._session_factory() as session, session.begin():
            acquired = await session.scalar(sa.select(sa.func.pg_try_advisory_xact_lock(key)))
            if not acquired:
                logger.info("window.lock_contended", extra={"window_end": window.end.isoformat()})
                yield False
                return
            yield True

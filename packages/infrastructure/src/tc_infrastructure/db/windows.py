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
from tc_domain.windows import CaptureWindow, windows_since
from tc_infrastructure.db.tables import capture_windows

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

    async def last_organized_cutoff(self, workspace_id: WorkspaceId) -> dt.datetime | None:
        """The end of the most recent successfully organized window."""
        async with self._session_factory() as session:
            latest = await session.scalar(
                sa.select(sa.func.max(capture_windows.c.window_end)).where(
                    capture_windows.c.workspace_id == workspace_id,
                    capture_windows.c.status == "organized",
                )
            )
        return latest if latest is None else dt.datetime.fromisoformat(latest.isoformat())

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

        Anchored on the last organized cutoff, falling back to the first capture
        so that a brand-new workspace does not enumerate windows reaching back
        to the epoch.
        """
        anchor = await self.last_organized_cutoff(workspace_id)
        if anchor is None:
            if first_capture_at is None:
                return []
            anchor = first_capture_at

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

    async def record(
        self, workspace_id: WorkspaceId, window: CaptureWindow, *, status: str
    ) -> None:
        """Upsert the window row. Idempotent on ``(workspace_id, window_end)``."""
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
                )
            )

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

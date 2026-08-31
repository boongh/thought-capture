"""Scheduling and catch-up for the organize pipeline.

Two behaviours, both required by docs/DESIGN.md 4.2:

- At each cutoff, organize the window that just closed.
- On startup, process *every* closed, unprocessed window oldest-first, so that
  a machine which was off for three days produces three digests rather than
  losing two.

The pipeline itself is injected, so this module can be tested without one and
so that replacing the pipeline does not touch scheduling.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from tc_domain.capture import WorkspaceId
from tc_domain.windows import CaptureWindow, next_cutoff_after
from tc_infrastructure.db.windows import PostgresCaptureWindows

logger = logging.getLogger(__name__)

# An organize run that fails must not stop the scheduler; the next cutoff still
# needs to fire, and the failed window remains unorganized so catch-up retries.
OrganizeCallback = Callable[[WorkspaceId, CaptureWindow], Awaitable[None]]


class OrganizeScheduler:
    """Fires the organize pipeline at each cutoff, and catches up on start."""

    def __init__(
        self,
        *,
        windows: PostgresCaptureWindows,
        organize: OrganizeCallback,
        workspace_id: WorkspaceId,
        digest_local_time: dt.time,
        timezone: str,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._windows = windows
        self._organize = organize
        self._workspace_id = workspace_id
        self._digest_local_time = digest_local_time
        self._timezone = timezone
        self._clock = clock
        self._scheduler = AsyncIOScheduler(timezone=dt.UTC)

    async def catch_up(self, *, first_capture_at: dt.datetime | None) -> int:
        """Organize every closed, unprocessed window. Returns how many succeeded.

        Oldest first, so each day's digest is produced in the order it was
        lived. One window failing does not abandon the rest: a transient
        provider outage during one day should not block the others.
        """
        now = self._clock()
        due = await self._windows.due_windows(
            self._workspace_id,
            now=now,
            digest_local_time=self._digest_local_time,
            timezone=self._timezone,
            first_capture_at=first_capture_at,
        )

        if not due:
            logger.info("scheduler.nothing_due")
            return 0

        logger.info("scheduler.catch_up_started", extra={"window_count": len(due)})
        organized = 0
        for window in due:
            if await self._organize_window(window):
                organized += 1
        logger.info(
            "scheduler.catch_up_finished",
            extra={"organized": organized, "attempted": len(due)},
        )
        return organized

    async def _organize_window(self, window: CaptureWindow) -> bool:
        """Organize one window under its advisory lock. Never raises."""
        async with self._windows.locked(self._workspace_id, window) as acquired:
            if not acquired:
                # Another worker has it. Skipping is correct: waiting would only
                # queue up to redo work that is already being done.
                return False
            try:
                await self._windows.record(self._workspace_id, window, status="closed")
                await self._organize(self._workspace_id, window)
            except Exception:
                # The window stays unorganized, so catch-up will retry it. A
                # failing day must not stop the scheduler or the other days.
                logger.exception(
                    "scheduler.organize_failed",
                    extra={"window_end": window.end.isoformat()},
                )
                return False
        return True

    def schedule_next(self) -> dt.datetime:
        """Arm a one-shot job for the next cutoff and return when it will fire.

        Re-armed after each run rather than using a recurring trigger, because
        the interval between cutoffs is 23, 24, or 25 hours depending on
        daylight saving, and a fixed interval would drift.
        """
        fire_at = next_cutoff_after(self._clock(), self._digest_local_time, self._timezone)
        self._scheduler.add_job(
            self._run_scheduled,
            trigger=DateTrigger(run_date=fire_at),
            id="organize-next-cutoff",
            replace_existing=True,
            misfire_grace_time=None,
        )
        logger.info("scheduler.armed", extra={"fire_at": fire_at.isoformat()})
        return fire_at

    async def _run_scheduled(self) -> None:
        await self.catch_up(first_capture_at=None)
        self.schedule_next()

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

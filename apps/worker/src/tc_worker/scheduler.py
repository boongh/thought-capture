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
import uuid
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from tc_domain.capture import WorkspaceId
from tc_domain.windows import CaptureWindow, next_cutoff_after
from tc_infrastructure.db.windows import PostgresCaptureWindows

logger = logging.getLogger(__name__)

# An organize run that fails must not stop the scheduler; the next cutoff still
# needs to fire, and the failed window remains unorganized so catch-up retries.
#
# The pipeline returns the id of the run row it created, which the scheduler
# records against the window. The pipeline owns the run; the scheduler owns the
# window's completion - and marking it complete is not atomic with whatever the
# callback already committed: a crash between the callback returning and
# ``mark_organized`` committing leaves the window ``closed`` even though the
# callback's own writes, including its ``runs`` row, are already durable.
#
# Holding one database transaction open across the callback is not the fix:
# the callback calls an LLM provider (a network round trip that can legitimately
# take up to the adapter's own timeout), and a transaction held open that long
# causes lock contention and bloat independent of this feature. Instead,
# ``_organize_window`` resumes rather than redoes: before invoking this
# callback it looks for a ``runs`` row already marked ``succeeded`` for this
# exact window (``PostgresCaptureWindows.succeeded_run_for``), and if one
# exists it skips straight to ``mark_organized`` with that run's id.
#
# **This callback's contract, to make that resume safe**: its ``runs`` row for
# a window must carry that window's exact ``window_start``/``window_end``, and
# the row must not be committed with ``status='succeeded'`` until every other
# write the callback makes for that window (documents, revisions, entities,
# outbox events, ...) has already committed. "A succeeded run exists for this
# window" is the signal the resume check trusts to mean "this window's output
# is completely and correctly written" - if that is not true, resuming would
# skip work rather than skip a duplicate.
OrganizeCallback = Callable[[WorkspaceId, CaptureWindow], Awaitable[uuid.UUID]]

# How the scheduler learns when this workspace first captured anything, so a
# brand-new workspace's first cutoff is not skipped.
FirstCaptureLookup = Callable[[WorkspaceId], Awaitable[dt.datetime | None]]


class OrganizeScheduler:
    """Fires the organize pipeline at each cutoff, and catches up on start."""

    def __init__(
        self,
        *,
        windows: PostgresCaptureWindows,
        organize: OrganizeCallback,
        first_capture: FirstCaptureLookup,
        workspace_id: WorkspaceId,
        digest_local_time: dt.time,
        timezone: str,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._windows = windows
        self._organize = organize
        self._first_capture = first_capture
        self._workspace_id = workspace_id
        self._digest_local_time = digest_local_time
        self._timezone = timezone
        self._clock = clock
        self._scheduler = AsyncIOScheduler(timezone=dt.UTC)

    async def catch_up(self, *, first_capture_at: dt.datetime | None = None) -> int:
        """Organize every closed, unprocessed window. Returns how many succeeded.

        Oldest first, so each day's digest is produced in the order it was
        lived. One window failing does not abandon the rest: a transient
        provider outage during one day should not block the others.

        ``first_capture_at`` is looked up when not supplied, so a scheduled run
        in a workspace that has never been organized still finds its first
        window instead of silently doing nothing.
        """
        now = self._clock()
        if first_capture_at is None:
            first_capture_at = await self._first_capture(self._workspace_id)

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
        """Organize one window under its advisory lock, and mark it done.

        Owns the whole transition: claim, run (or resume), mark organized. The
        pipeline callback does not mark completion, so a successful callback
        always produces durable completion and cannot be silently forgotten.

        Never raises: a failing day must not stop the scheduler or the other
        days, and the window stays unorganized so catch-up retries it. The
        catch spans the whole transition, not just the callback - a
        ``claim``/``mark_organized`` failure (a DB hiccup, say) must not abort
        the rest of this batch or, via ``catch_up``, stop the caller from
        re-arming the next cutoff.
        """
        try:
            async with self._windows.locked(self._workspace_id, window) as acquired:
                if not acquired:
                    # Another worker has it. Skipping is correct: waiting would
                    # only queue up to redo work that is already being done.
                    return False

                if not await self._windows.claim(self._workspace_id, window):
                    # Completed by another worker between building the due list
                    # and acquiring the lock.
                    return False

                # Resume rather than redo: a prior attempt may have committed
                # the callback's run and everything under it, then crashed
                # before `mark_organized` committed. See the contract note on
                # `OrganizeCallback` above.
                run_id = await self._windows.succeeded_run_for(self._workspace_id, window)
                if run_id is not None:
                    logger.info(
                        "scheduler.resumed_completed_run",
                        extra={"window_end": window.end.isoformat(), "run_id": str(run_id)},
                    )
                else:
                    run_id = await self._organize(self._workspace_id, window)

                await self._windows.mark_organized(self._workspace_id, window, run_id)
        except Exception as exc:
            # Sanitized: an exception chain here can carry connection strings,
            # signed URLs, or captured text (docs/DESIGN.md 14.2).
            logger.error(
                "scheduler.organize_failed",
                extra={
                    "window_end": window.end.isoformat(),
                    "error_class": type(exc).__name__,
                },
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
        """Catch up, then always re-arm - even when catching up blew up.

        ``catch_up`` itself never raises for a single window's failure, but a
        failure outside any one window (the due-list query, say) still can. A
        one-shot scheduler that does not re-arm after that stops organizing
        entirely until the process restarts, so re-arming happens in
        ``finally`` rather than after a bare ``await``.
        """
        try:
            await self.catch_up()
        except Exception as exc:
            logger.error("scheduler.catch_up_failed", extra={"error_class": type(exc).__name__})
        finally:
            self.schedule_next()

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

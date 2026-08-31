"""Capture windows and the daily cutoff.

A capture window is ``(previous_successful_cutoff, current_cutoff]`` - not a
calendar day (docs/DESIGN.md 4.2). That distinction is the whole point: a
thought sent at 23:00 belongs to the *next* day's digest rather than being lost
between two calendar days, and a machine that was off for three days produces
three windows rather than one enormous one.

Pure computation. No clock, no database: the caller supplies ``now``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# A machine offline for longer than this produces a lot of windows. Catch-up is
# still correct, but the bound stops one command from queueing years of work.
MAX_CATCHUP_WINDOWS = 400


@dataclass(frozen=True, slots=True)
class CaptureWindow:
    """Half-open in the past, closed at the cutoff: ``(start, end]``.

    A thought whose ``client_created_at`` equals ``start`` belongs to the
    previous window; one that equals ``end`` belongs to this one.

    **Bounds are normalized to UTC on construction, deliberately.** Python
    compares and subtracts two aware datetimes that share a ``tzinfo`` object by
    wall clock, ignoring any offset change between them. Across a daylight-saving
    transition that is wrong in both directions: ``20:00`` on one London day and
    ``20:00`` on the next are 23 hours apart, but subtracting them while both
    carry ``ZoneInfo("Europe/London")`` reports 24. Storing UTC makes every
    comparison and every duration absolute, so a thought can never be judged
    into the wrong window by an hour.
    """

    start: dt.datetime
    end: dt.datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("capture window bounds must be timezone-aware")
        object.__setattr__(self, "start", self.start.astimezone(dt.UTC))
        object.__setattr__(self, "end", self.end.astimezone(dt.UTC))
        if self.end <= self.start:
            raise ValueError("a capture window must end after it starts")

    @property
    def duration(self) -> dt.timedelta:
        """True elapsed time, which is 23 or 25 hours across a DST transition."""
        return self.end - self.start

    def contains(self, instant: dt.datetime) -> bool:
        if instant.tzinfo is None:
            raise ValueError("cannot place a naive instant in a capture window")
        return self.start < instant.astimezone(dt.UTC) <= self.end


def cutoff_on(local_date: dt.date, local_time: dt.time, timezone: str) -> dt.datetime:
    """The instant at which the cutoff falls on a given local date.

    Two daylight-saving edge cases are resolved deterministically rather than
    left to chance, because a scheduler that behaves differently on two runs of
    the same day is worse than one that picks a defensible answer:

    - **Ambiguous** wall-clock time, when the hour repeats in autumn: the first
      occurrence is used (``fold=0``).
    - **Non-existent** wall-clock time, when the hour is skipped in spring:
      Python maps it through the pre-transition offset, which yields a real
      instant just after the gap. The window is a few minutes short that day and
      never skipped.
    """
    zone = ZoneInfo(timezone)
    return dt.datetime.combine(local_date, local_time, tzinfo=zone)


def most_recent_cutoff(now: dt.datetime, local_time: dt.time, timezone: str) -> dt.datetime:
    """The latest cutoff at or before ``now``."""
    _require_aware(now)
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)

    today = cutoff_on(local_now.date(), local_time, timezone)
    if today <= now:
        return today
    return cutoff_on(local_now.date() - dt.timedelta(days=1), local_time, timezone)


def next_cutoff_after(now: dt.datetime, local_time: dt.time, timezone: str) -> dt.datetime:
    """The first cutoff strictly after ``now``. Used to schedule the next run."""
    _require_aware(now)
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)

    today = cutoff_on(local_now.date(), local_time, timezone)
    if today > now:
        return today
    return cutoff_on(local_now.date() + dt.timedelta(days=1), local_time, timezone)


def windows_since(
    previous_cutoff: dt.datetime,
    now: dt.datetime,
    local_time: dt.time,
    timezone: str,
    *,
    limit: int = MAX_CATCHUP_WINDOWS,
) -> list[CaptureWindow]:
    """Every *closed* window between ``previous_cutoff`` and ``now``, oldest first.

    Closed means the cutoff has passed. The window currently accumulating
    thoughts is never returned, because organizing it would produce a digest
    that is missing the rest of the day.

    Consecutive windows are contiguous: each one starts exactly where the
    previous ended, so no thought can fall between two windows and none can
    appear in two.
    """
    _require_aware(previous_cutoff)
    _require_aware(now)

    latest = most_recent_cutoff(now, local_time, timezone)
    if latest <= previous_cutoff:
        return []

    windows: list[CaptureWindow] = []
    start = previous_cutoff
    zone = ZoneInfo(timezone)

    # Walk forward one local day at a time. Stepping in local dates rather than
    # adding 24 hours is what keeps this correct across a DST transition, where
    # one "day" is 23 or 25 hours long.
    cursor_date = start.astimezone(zone).date()
    while len(windows) < limit:
        cursor_date += dt.timedelta(days=1)
        end = cutoff_on(cursor_date, local_time, timezone)
        if end <= start:
            # Can happen on the first step when `start` is itself a cutoff on a
            # later local date than the cursor implies.
            continue
        if end > latest:
            break
        windows.append(CaptureWindow(start=start, end=end))
        start = end

    return windows


def _require_aware(instant: dt.datetime) -> None:
    if instant.tzinfo is None:
        raise ValueError("instants must be timezone-aware; a naive one has no cutoff")

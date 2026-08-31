"""Cutoff and capture-window computation.

This is the logic that decides which day a thought belongs to. Getting it wrong
does not crash anything - it silently files thoughts under the wrong digest, or
loses a window entirely when the machine was off. The acceptance checklist
requires DST correctness for Bangkok and London, and exactly one canonical
result per closed window (docs/DESIGN.md Appendix A).
"""

from __future__ import annotations

import datetime as dt
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tc_domain.windows import (
    CaptureWindow,
    cutoff_on,
    most_recent_cutoff,
    next_cutoff_after,
    windows_since,
)

BANGKOK = "Asia/Bangkok"
LONDON = "Europe/London"
CUTOFF = dt.time(20, 0)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# The window value
# ---------------------------------------------------------------------------


def test_a_window_is_half_open_at_the_start_and_closed_at_the_end() -> None:
    """A thought exactly on a cutoff belongs to the window that ends there.

    Without this rule a thought landing precisely at 20:00:00 would either be
    counted twice or dropped.
    """
    window = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))

    assert window.contains(window.start) is False
    assert window.contains(window.end) is True
    assert window.contains(utc(2026, 8, 31)) is True


def test_window_bounds_are_normalized_to_utc() -> None:
    """Guards a subtle Python behaviour that would misplace thoughts by an hour.

    Python compares and subtracts two aware datetimes sharing a ``tzinfo``
    object by *wall clock*, ignoring any offset change between them. 20:00 on
    2026-03-28 and 20:00 on 2026-03-29 in London are genuinely 23 hours apart,
    but subtracting them while both carry ``ZoneInfo("Europe/London")`` reports
    24. Normalizing to UTC on construction makes every comparison absolute.
    """
    london = ZoneInfo(LONDON)
    before = dt.datetime(2026, 3, 28, 20, tzinfo=london)
    after = dt.datetime(2026, 3, 29, 20, tzinfo=london)

    # The hazard itself, asserted so its removal is visible.
    assert after - before == dt.timedelta(hours=24)

    window = CaptureWindow(start=before, end=after)
    assert window.start.tzinfo is dt.UTC
    assert window.end.tzinfo is dt.UTC
    assert window.duration == dt.timedelta(hours=23)


def test_membership_is_absolute_not_wall_clock() -> None:
    """A thought an hour before the cutoff is inside, whatever zone expresses it."""
    window = CaptureWindow(
        start=dt.datetime(2026, 3, 28, 20, tzinfo=ZoneInfo(LONDON)),
        end=dt.datetime(2026, 3, 29, 20, tzinfo=ZoneInfo(LONDON)),
    )
    an_hour_before_the_cutoff = utc(2026, 3, 29, 18)

    assert window.contains(an_hour_before_the_cutoff) is True
    assert window.contains(utc(2026, 3, 29, 20)) is False


def test_a_naive_instant_cannot_be_placed() -> None:
    window = CaptureWindow(start=utc(2026, 8, 30, 13), end=utc(2026, 8, 31, 13))
    with pytest.raises(ValueError, match="naive"):
        window.contains(dt.datetime(2026, 8, 31))


def test_a_window_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="end after it starts"):
        CaptureWindow(start=utc(2026, 8, 31, 13), end=utc(2026, 8, 30, 13))


def test_window_bounds_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CaptureWindow(
            start=dt.datetime(2026, 8, 30, 13),
            end=utc(2026, 8, 31, 13),
        )


# ---------------------------------------------------------------------------
# Cutoff placement
# ---------------------------------------------------------------------------


def test_bangkok_cutoff_is_thirteen_hundred_utc() -> None:
    """20:00 Asia/Bangkok is 13:00 UTC, all year - the zone has no DST."""
    assert cutoff_on(dt.date(2026, 8, 30), CUTOFF, BANGKOK) == utc(2026, 8, 30, 13)
    assert cutoff_on(dt.date(2026, 1, 15), CUTOFF, BANGKOK) == utc(2026, 1, 15, 13)


def test_london_cutoff_moves_with_daylight_saving() -> None:
    """20:00 local is 19:00 UTC in summer and 20:00 UTC in winter.

    A scheduler that ignored this would fire an hour off for half the year.
    """
    assert cutoff_on(dt.date(2026, 7, 1), CUTOFF, LONDON) == utc(2026, 7, 1, 19)
    assert cutoff_on(dt.date(2026, 12, 1), CUTOFF, LONDON) == utc(2026, 12, 1, 20)


def test_most_recent_cutoff_before_todays_cutoff_is_yesterdays() -> None:
    just_before = utc(2026, 8, 30, 12, 59)
    assert most_recent_cutoff(just_before, CUTOFF, BANGKOK) == utc(2026, 8, 29, 13)


def test_most_recent_cutoff_at_the_cutoff_is_today() -> None:
    """The boundary instant belongs to the window that just closed."""
    assert most_recent_cutoff(utc(2026, 8, 30, 13), CUTOFF, BANGKOK) == utc(2026, 8, 30, 13)


def test_next_cutoff_is_strictly_after_now() -> None:
    at_cutoff = utc(2026, 8, 30, 13)
    assert next_cutoff_after(at_cutoff, CUTOFF, BANGKOK) == utc(2026, 8, 31, 13)
    assert next_cutoff_after(utc(2026, 8, 30, 12), CUTOFF, BANGKOK) == utc(2026, 8, 30, 13)


# ---------------------------------------------------------------------------
# Window enumeration
# ---------------------------------------------------------------------------


def test_no_windows_when_the_cutoff_has_not_passed_again() -> None:
    previous = utc(2026, 8, 30, 13)
    assert windows_since(previous, utc(2026, 8, 31, 12), CUTOFF, BANGKOK) == []


def test_one_window_per_elapsed_day() -> None:
    previous = utc(2026, 8, 30, 13)
    windows = windows_since(previous, utc(2026, 8, 31, 14), CUTOFF, BANGKOK)

    assert len(windows) == 1
    assert windows[0].start == previous
    assert windows[0].end == utc(2026, 8, 31, 13)


def test_catch_up_after_three_days_offline_produces_three_windows() -> None:
    """ "If the machine is off at cutoff, a startup catch-up job processes every
    closed, unprocessed window oldest-first" (docs/DESIGN.md 4.2).

    Three windows, not one three-day window: each day gets its own digest.
    """
    previous = utc(2026, 8, 28, 13)
    windows = windows_since(previous, utc(2026, 8, 31, 14), CUTOFF, BANGKOK)

    assert [w.end for w in windows] == [
        utc(2026, 8, 29, 13),
        utc(2026, 8, 30, 13),
        utc(2026, 8, 31, 13),
    ]


def test_the_open_window_is_never_returned() -> None:
    """Organizing the accumulating window would produce a digest missing the day."""
    previous = utc(2026, 8, 30, 13)
    windows = windows_since(previous, utc(2026, 8, 31, 20), CUTOFF, BANGKOK)

    assert all(w.end <= utc(2026, 8, 31, 13) for w in windows)


def test_windows_are_contiguous_and_non_overlapping() -> None:
    """No thought may fall between two windows, and none may appear in both."""
    windows = windows_since(utc(2026, 8, 25, 13), utc(2026, 8, 31, 14), CUTOFF, BANGKOK)

    assert len(windows) > 1
    for earlier, later in pairwise(windows):
        assert earlier.end == later.start


def test_catch_up_is_bounded() -> None:
    """A machine offline for years must not queue years of work in one command."""
    windows = windows_since(utc(2020, 1, 1, 13), utc(2026, 8, 31, 14), CUTOFF, BANGKOK, limit=5)
    assert len(windows) == 5


# ---------------------------------------------------------------------------
# Daylight saving
# ---------------------------------------------------------------------------


def test_windows_stay_contiguous_across_the_spring_transition() -> None:
    """The 25-hour and 23-hour days must not leave a gap or an overlap.

    Stepping by local date rather than by 24 hours is what makes this hold.
    """
    windows = windows_since(
        cutoff_on(dt.date(2026, 3, 27), CUTOFF, LONDON),
        utc(2026, 3, 31, 20),
        CUTOFF,
        LONDON,
    )

    for earlier, later in pairwise(windows):
        assert earlier.end == later.start
    # The window spanning the spring-forward night is an hour shorter.
    lengths = {w.duration for w in windows}
    assert dt.timedelta(hours=23) in lengths


def test_windows_stay_contiguous_across_the_autumn_transition() -> None:
    windows = windows_since(
        cutoff_on(dt.date(2026, 10, 23), CUTOFF, LONDON),
        utc(2026, 10, 27, 21),
        CUTOFF,
        LONDON,
    )

    for earlier, later in pairwise(windows):
        assert earlier.end == later.start
    lengths = {w.duration for w in windows}
    assert dt.timedelta(hours=25) in lengths


def test_every_local_day_has_exactly_one_cutoff_across_a_transition() -> None:
    """Neither transition may skip a day or produce two cutoffs for one."""
    windows = windows_since(
        cutoff_on(dt.date(2026, 3, 25), CUTOFF, LONDON),
        utc(2026, 4, 2, 20),
        CUTOFF,
        LONDON,
    )
    zone = ZoneInfo(LONDON)
    local_dates = [w.end.astimezone(zone).date() for w in windows]
    assert len(local_dates) == len(set(local_dates))


# ---------------------------------------------------------------------------
# Invariants over arbitrary inputs
# ---------------------------------------------------------------------------


@given(
    now=st.datetimes(
        min_value=dt.datetime(2020, 1, 1),
        max_value=dt.datetime(2035, 12, 31),
        timezones=st.just(dt.UTC),
    ),
    timezone=st.sampled_from([BANGKOK, LONDON, "America/New_York", "Pacific/Chatham"]),
)
def test_the_most_recent_cutoff_is_never_in_the_future(now: dt.datetime, timezone: str) -> None:
    assert most_recent_cutoff(now, CUTOFF, timezone) <= now


@given(
    now=st.datetimes(
        min_value=dt.datetime(2020, 1, 1),
        max_value=dt.datetime(2035, 12, 31),
        timezones=st.just(dt.UTC),
    ),
    timezone=st.sampled_from([BANGKOK, LONDON, "America/New_York", "Pacific/Chatham"]),
)
def test_the_next_cutoff_is_always_in_the_future(now: dt.datetime, timezone: str) -> None:
    assert next_cutoff_after(now, CUTOFF, timezone) > now


@given(
    days_behind=st.integers(min_value=0, max_value=60),
    timezone=st.sampled_from([BANGKOK, LONDON, "America/New_York"]),
)
def test_enumerated_windows_are_always_contiguous_and_closed(
    days_behind: int, timezone: str
) -> None:
    """The two properties everything else depends on, over arbitrary lag."""
    now = utc(2026, 8, 31, 14)
    previous = most_recent_cutoff(now, CUTOFF, timezone) - dt.timedelta(days=days_behind)

    windows = windows_since(previous, now, CUTOFF, timezone)

    for earlier, later in pairwise(windows):
        assert earlier.end == later.start
    assert all(window.end <= now for window in windows)
    if windows:
        assert windows[0].start == previous

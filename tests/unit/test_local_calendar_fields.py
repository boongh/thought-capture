"""Timezone handling for the capture layer.

The Release 1 acceptance checklist requires DST correctness for at least Bangkok
and London (docs/DESIGN.md Appendix A). Bangkok is a fixed +07:00 offset with no
DST; London moves between GMT and BST, so it is where an off-by-one-hour bug
would actually show up.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tc_domain.capture import local_calendar_fields

BANGKOK = "Asia/Bangkok"
LONDON = "Europe/London"


# ---------------------------------------------------------------------------
# Bangkok: fixed offset, the default workspace timezone
# ---------------------------------------------------------------------------


@given(
    instant=st.datetimes(
        min_value=dt.datetime(2000, 1, 1),
        max_value=dt.datetime(2099, 12, 31),
        timezones=st.just(dt.UTC),
    )
)
def test_bangkok_is_always_exactly_seven_hours_ahead(instant: dt.datetime) -> None:
    local_date, local_time = local_calendar_fields(instant, BANGKOK)

    reconstructed = dt.datetime.combine(local_date, local_time, tzinfo=ZoneInfo(BANGKOK))
    assert reconstructed.utcoffset() == dt.timedelta(hours=7)
    assert reconstructed == instant.astimezone(ZoneInfo(BANGKOK))


def test_bangkok_evening_cutoff_boundary() -> None:
    """13:00 UTC is exactly the 20:00 local cutoff."""
    local_date, local_time = local_calendar_fields(
        dt.datetime(2026, 8, 30, 13, 0, tzinfo=dt.UTC), BANGKOK
    )
    assert (local_date, local_time) == (dt.date(2026, 8, 30), dt.time(20, 0))


# ---------------------------------------------------------------------------
# London: DST transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc_instant", "expected_date", "expected_time", "note"),
    [
        (
            dt.datetime(2026, 3, 29, 0, 30, tzinfo=dt.UTC),
            dt.date(2026, 3, 29),
            dt.time(0, 30),
            "just before BST begins, still GMT",
        ),
        (
            dt.datetime(2026, 3, 29, 1, 30, tzinfo=dt.UTC),
            dt.date(2026, 3, 29),
            dt.time(2, 30),
            "after the spring-forward gap, now BST",
        ),
        (
            dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.UTC),
            dt.date(2026, 10, 25),
            dt.time(1, 30),
            "first pass through the repeated hour, still BST",
        ),
        (
            dt.datetime(2026, 10, 25, 1, 30, tzinfo=dt.UTC),
            dt.date(2026, 10, 25),
            dt.time(1, 30),
            "second pass through the repeated hour, now GMT",
        ),
        (
            dt.datetime(2026, 10, 25, 23, 30, tzinfo=dt.UTC),
            dt.date(2026, 10, 25),
            dt.time(23, 30),
            "same day once GMT has resumed",
        ),
    ],
)
def test_london_dst_transitions(
    utc_instant: dt.datetime, expected_date: dt.date, expected_time: dt.time, note: str
) -> None:
    local_date, local_time = local_calendar_fields(utc_instant, LONDON)
    assert (local_date, local_time) == (expected_date, expected_time), note


def test_repeated_hour_is_ambiguous_by_wall_clock_alone() -> None:
    """Two distinct instants share one local wall-clock reading.

    This is why `client_created_at` is stored alongside the derived local
    fields: the instant is authoritative, the local fields are for search.
    """
    first = local_calendar_fields(dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.UTC), LONDON)
    second = local_calendar_fields(dt.datetime(2026, 10, 25, 1, 30, tzinfo=dt.UTC), LONDON)
    assert first == second


# ---------------------------------------------------------------------------
# General invariants
# ---------------------------------------------------------------------------


@given(
    instant=st.datetimes(
        min_value=dt.datetime(1970, 1, 1),
        max_value=dt.datetime(2099, 12, 31),
        timezones=st.just(dt.UTC),
    ),
    timezone=st.sampled_from(
        [BANGKOK, LONDON, "America/New_York", "Australia/Lord_Howe", "Pacific/Chatham", "UTC"]
    ),
)
def test_derivation_matches_the_zone_conversion(instant: dt.datetime, timezone: str) -> None:
    """Half-hour and 45-minute zones must work as well as whole-hour ones."""
    local_date, local_time = local_calendar_fields(instant, timezone)
    expected = instant.astimezone(ZoneInfo(timezone))
    assert local_date == expected.date()
    assert local_time == expected.time()

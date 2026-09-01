"""Digest message formatting, split for a message-length-limited channel."""

from __future__ import annotations

import datetime as dt

from tc_domain.digest import MAX_MESSAGE_CHARS, DigestContent, format_digest_messages

WINDOW_START = dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC)
WINDOW_END = dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC)


def a_digest(body: str, *, title: str = "Daily digest") -> DigestContent:
    return DigestContent(
        title=title, body_markdown=body, window_start=WINDOW_START, window_end=WINDOW_END
    )


def test_a_short_digest_fits_in_one_chunk() -> None:
    body = "## Summary\n\nWalked the dog.\n\n## Open threads\n\n- none"
    chunks = format_digest_messages(a_digest(body))

    assert len(chunks) == 1
    assert "Daily digest" in chunks[0]
    assert "2026-08-30 13:00 UTC" in chunks[0]
    assert "2026-08-31 13:00 UTC" in chunks[0]
    assert "Walked the dog." in chunks[0]


def test_every_chunk_stays_within_the_limit() -> None:
    section = "x" * 500
    body = "\n\n".join(f"## Section {i}\n\n{section}" for i in range(10))
    chunks = format_digest_messages(a_digest(body))

    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_MESSAGE_CHARS for chunk in chunks)


def test_splitting_happens_on_section_boundaries_not_mid_section() -> None:
    # Large enough that header+section already exceeds the limit, so each
    # section lands in its own chunk, but small enough that neither section
    # alone needs a hard split.
    section = "y" * 1950
    body = f"## First\n\n{section}\n\n## Second\n\n{section}"
    chunks = format_digest_messages(a_digest(body))

    assert len(chunks) == 3  # header, First, Second - First and header don't fit together
    assert "## First" in chunks[1]
    assert "## Second" in chunks[2]
    assert "## First" not in chunks[2]


def test_a_single_oversized_section_is_hard_split_rather_than_dropped() -> None:
    huge_section = "\n".join(f"line {i} " + "z" * 100 for i in range(50))
    body = f"## Huge\n\n{huge_section}"
    chunks = format_digest_messages(a_digest(body))

    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_MESSAGE_CHARS for chunk in chunks)
    # No content is lost across the split.
    assert "line 0 " in chunks[1]
    assert "line 49 " in chunks[-1]


def test_consecutive_short_sections_are_packed_into_one_chunk() -> None:
    body = "## A\n\nshort\n\n## B\n\nshort\n\n## C\n\nshort"
    chunks = format_digest_messages(a_digest(body))

    assert len(chunks) == 1
    assert "## A" in chunks[0]
    assert "## B" in chunks[0]
    assert "## C" in chunks[0]


def test_the_header_always_starts_the_first_chunk() -> None:
    chunks = format_digest_messages(a_digest("## Summary\n\nsomething"))
    assert chunks[0].startswith("\U0001f4cb")

"""Formatting the daily digest for a message-length-limited channel.

Discord's practical limit is 2000 characters per message. The organize-v1
prompt is already instructed to keep digests short, so this is a safety net,
not the primary control: split on section (``## ``) boundaries first, and only
hard-split a single oversized section as a last resort (docs/DESIGN.md 4.2).
"""

from __future__ import annotations

import dataclasses
import datetime as dt

MAX_MESSAGE_CHARS = 2000


@dataclasses.dataclass(frozen=True, slots=True)
class DigestContent:
    """One digest document revision, as read for delivery."""

    title: str
    body_markdown: str
    window_start: dt.datetime
    window_end: dt.datetime


def _header(content: DigestContent) -> str:
    start = content.window_start.strftime("%Y-%m-%d %H:%M UTC")
    end = content.window_end.strftime("%Y-%m-%d %H:%M UTC")
    return f"\U0001f4cb **{content.title}** ({start} → {end})"


def _sections(body_markdown: str) -> list[str]:
    """Split on ``## `` heading boundaries, keeping each heading with its body."""
    sections: list[list[str]] = []
    for line in body_markdown.splitlines():
        if line.startswith("## ") or not sections:
            sections.append([line])
        else:
            sections[-1].append(line)
    return [text for section in sections if (text := "\n".join(section).strip())]


def _hard_split(section: str, max_chars: int) -> list[str]:
    """Last-resort split of one oversized section, on line boundaries."""
    pieces: list[str] = []
    current = ""
    for line in section.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_chars and current:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def format_digest_messages(
    content: DigestContent, *, max_chars: int = MAX_MESSAGE_CHARS
) -> tuple[str, ...]:
    """Pack the digest into message-length-safe chunks, split on section boundaries.

    The header always starts the first chunk. Sections are packed greedily so
    consecutive short sections share a chunk; a section that alone exceeds
    ``max_chars`` is hard-split on line boundaries rather than dropped.
    """
    chunks: list[str] = [_header(content)]

    for section in _sections(content.body_markdown):
        pieces = _hard_split(section, max_chars) if len(section) > max_chars else [section]
        for piece in pieces:
            candidate = f"{chunks[-1]}\n\n{piece}"
            if len(candidate) <= max_chars:
                chunks[-1] = candidate
            else:
                chunks.append(piece)

    return tuple(chunks)

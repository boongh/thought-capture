"""Translation between Discord Gateway events and application commands.

Transport only. No database access, no business rules: the allowlist and the
capture policy live in ``tc_domain``, so replacing Discord means replacing this
file (docs/DESIGN.md 5.3).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from tc_domain.capture import (
    AttachmentCandidate,
    CaptureCommand,
    CaptureSource,
    UserId,
    WorkspaceId,
)


class DiscordAttachment(Protocol):
    """The subset of ``discord.Attachment`` this adapter uses."""

    @property
    def filename(self) -> str: ...
    @property
    def content_type(self) -> str | None: ...
    @property
    def size(self) -> int: ...
    @property
    def url(self) -> str: ...


class DiscordMessage(Protocol):
    """The subset of ``discord.Message`` this adapter uses.

    Declared structurally so the translation can be tested without constructing
    real Gateway objects, which require a connected client.
    """

    @property
    def id(self) -> int: ...
    @property
    def content(self) -> str: ...
    @property
    def created_at(self) -> dt.datetime: ...


@dataclass(frozen=True, slots=True)
class MessageFacts:
    """Everything the allowlist needs, extracted from a Gateway message."""

    author_id: int
    author_is_bot: bool
    guild_id: int | None
    channel_id: int | None


DEFAULT_MEDIA_TYPE = "application/octet-stream"


def facts_from(message: object) -> MessageFacts:
    """Read allowlist-relevant identity from a ``discord.Message``.

    A webhook message has ``webhook_id`` set and may not be flagged as a bot, so
    both are treated as non-human (docs/DESIGN.md 4.1).
    """
    author = message.author  # type: ignore[attr-defined]
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)

    is_bot = bool(getattr(author, "bot", False)) or getattr(message, "webhook_id", None) is not None

    return MessageFacts(
        author_id=int(author.id),
        author_is_bot=is_bot,
        guild_id=int(guild.id) if guild is not None else None,
        channel_id=int(channel.id) if channel is not None else None,
    )


def attachment_from(attachment: DiscordAttachment) -> AttachmentCandidate:
    """Discord reports ``content_type`` as optional; fall back to a safe default."""
    return AttachmentCandidate(
        filename=attachment.filename,
        media_type=attachment.content_type or DEFAULT_MEDIA_TYPE,
        size_bytes=int(attachment.size),
        url=attachment.url,
        # Discord CDN URLs are signed and expire. The exact expiry is encoded in
        # the query string; recording the observation time is enough for
        # provenance, and the bytes are copied during ingestion regardless.
        url_expires_at=None,
    )


def command_from(
    message: object,
    *,
    workspace_id: WorkspaceId,
    user_id: UserId,
    workspace_timezone: str,
) -> CaptureCommand:
    """Build the capture command for an allowlisted message.

    ``created_at`` from Discord is timezone-aware UTC, which is what the domain
    requires; the workspace timezone is what resolves it to a local calendar day.
    """
    facts = facts_from(message)
    attachments = tuple(attachment_from(a) for a in getattr(message, "attachments", ()))

    return CaptureCommand(
        workspace_id=workspace_id,
        author_user_id=user_id,
        source=CaptureSource.DISCORD,
        source_message_id=str(message.id),  # type: ignore[attr-defined]
        source_channel_id=str(facts.channel_id) if facts.channel_id is not None else None,
        body=message.content,  # type: ignore[attr-defined]
        client_created_at=message.created_at,  # type: ignore[attr-defined]
        client_timezone=workspace_timezone,
        attachments=attachments,
    )


def acknowledgement_for(thought_id: int, attachment_count: int, deduplicated: bool) -> str:
    """A compact confirmation carrying the thought ID (docs/DESIGN.md 4.1).

    Deduplication is surfaced rather than hidden: seeing the same ID again is
    how the owner knows a retry did not create a second copy.
    """
    marker = "↩︎ already captured" if deduplicated else "✅ captured"
    parts = [f"{marker} `#{thought_id}`"]
    if attachment_count == 1:
        parts.append("with 1 attachment")
    elif attachment_count > 1:
        parts.append(f"with {attachment_count} attachments")
    return " ".join(parts)

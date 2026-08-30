"""Translation from Discord Gateway messages into capture commands.

Uses lightweight stand-ins rather than real ``discord.Message`` objects, which
cannot be constructed without a connected client. The adapter reads only the
fields declared in its Protocols, so the stand-ins are a faithful substitute.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from tc_discord_bot.adapter import (
    DEFAULT_MEDIA_TYPE,
    acknowledgement_for,
    attachment_from,
    command_from,
    facts_from,
)
from tc_domain.capture import CaptureSource, UserId, WorkspaceId

WORKSPACE_ID = WorkspaceId(uuid.UUID("11111111-1111-4111-8111-111111111111"))
USER_ID = UserId(uuid.UUID("22222222-2222-4222-8222-222222222222"))
OWNER_ID = 1234567890123456


@dataclass
class FakeAuthor:
    id: int = OWNER_ID
    bot: bool = False


@dataclass
class FakeChannel:
    id: int = 555


@dataclass
class FakeGuild:
    id: int = 999


@dataclass
class FakeAttachment:
    filename: str = "note.png"
    content_type: str | None = "image/png"
    size: int = 2048
    url: str = "https://cdn.discordapp.com/attachments/1/2/note.png?ex=a&is=b&hm=sig"


@dataclass
class FakeMessage:
    id: int = 42
    content: str = "remember to renew the passport"
    created_at: dt.datetime = dt.datetime(2026, 8, 30, 13, 0, tzinfo=dt.UTC)
    author: FakeAuthor = field(default_factory=FakeAuthor)
    channel: FakeChannel = field(default_factory=FakeChannel)
    guild: FakeGuild | None = None
    attachments: tuple[FakeAttachment, ...] = ()
    webhook_id: int | None = None


# ---------------------------------------------------------------------------
# Allowlist facts
# ---------------------------------------------------------------------------


def test_direct_message_has_no_guild() -> None:
    facts = facts_from(FakeMessage())
    assert facts.guild_id is None
    assert facts.channel_id == 555
    assert facts.author_id == OWNER_ID
    assert facts.author_is_bot is False


def test_guild_message_reports_its_guild() -> None:
    facts = facts_from(FakeMessage(guild=FakeGuild(id=777)))
    assert facts.guild_id == 777


def test_bot_author_is_detected() -> None:
    facts = facts_from(FakeMessage(author=FakeAuthor(bot=True)))
    assert facts.author_is_bot is True


def test_webhook_message_counts_as_non_human() -> None:
    """A webhook may not set the bot flag, but it is not the owner typing."""
    facts = facts_from(FakeMessage(webhook_id=31337))
    assert facts.author_is_bot is True


# ---------------------------------------------------------------------------
# Command translation
# ---------------------------------------------------------------------------


def test_command_carries_identity_and_provenance() -> None:
    command = command_from(
        FakeMessage(),
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        workspace_timezone="Asia/Bangkok",
    )

    assert command.source == CaptureSource.DISCORD
    assert command.source_message_id == "42"
    assert command.source_channel_id == "555"
    assert command.body == "remember to renew the passport"
    assert command.workspace_id == WORKSPACE_ID
    assert command.author_user_id == USER_ID
    assert command.client_timezone == "Asia/Bangkok"


def test_discord_timestamps_are_timezone_aware() -> None:
    """The domain rejects naive timestamps; Discord supplies aware UTC."""
    command = command_from(
        FakeMessage(),
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        workspace_timezone="Asia/Bangkok",
    )
    assert command.client_created_at.tzinfo is not None


def test_attachments_are_translated() -> None:
    command = command_from(
        FakeMessage(attachments=(FakeAttachment(), FakeAttachment(filename="b.pdf"))),
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        workspace_timezone="Asia/Bangkok",
    )

    assert len(command.attachments) == 2
    assert {a.filename for a in command.attachments} == {"note.png", "b.pdf"}


def test_missing_content_type_falls_back_to_a_safe_default() -> None:
    """Discord reports content_type as optional."""
    candidate = attachment_from(FakeAttachment(content_type=None))
    assert candidate.media_type == DEFAULT_MEDIA_TYPE


def test_message_with_only_an_attachment_is_translatable() -> None:
    command = command_from(
        FakeMessage(content="", attachments=(FakeAttachment(),)),
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        workspace_timezone="Asia/Bangkok",
    )
    assert command.body == ""
    assert command.is_empty is False


# ---------------------------------------------------------------------------
# Acknowledgement text
# ---------------------------------------------------------------------------


def test_acknowledgement_contains_the_thought_id() -> None:
    """The ID is what lets the owner find the row later (docs/DESIGN.md 4.1)."""
    assert "#41" in acknowledgement_for(41, attachment_count=0, deduplicated=False)


def test_acknowledgement_counts_attachments() -> None:
    assert "1 attachment" in acknowledgement_for(41, attachment_count=1, deduplicated=False)
    assert "2 attachments" in acknowledgement_for(41, attachment_count=2, deduplicated=False)


def test_acknowledgement_distinguishes_a_redelivery() -> None:
    """Seeing the same ID is how the owner knows no duplicate was created."""
    fresh = acknowledgement_for(41, attachment_count=0, deduplicated=False)
    repeat = acknowledgement_for(41, attachment_count=0, deduplicated=True)
    assert fresh != repeat
    assert "already captured" in repeat
    assert "#41" in repeat

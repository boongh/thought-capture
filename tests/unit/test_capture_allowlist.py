"""The Discord allowlist.

"Messages from bots, webhooks, edits, and other users are ignored and audited
without storing their content" (docs/DESIGN.md 4.1). The acceptance checklist
states it as a hard requirement: unauthorized Discord content is neither stored
nor acknowledged.
"""

from __future__ import annotations

import pytest

from tc_domain.errors import NotAllowlisted
from tc_domain.policy import CaptureAllowlist

OWNER = 1234567890
OTHER = 9876543210
GUILD = 111
CHANNEL = 222


def check(
    allowlist: CaptureAllowlist,
    *,
    author_id: int = OWNER,
    guild_id: int | None = None,
    channel_id: int | None = None,
    author_is_bot: bool = False,
) -> None:
    allowlist.check(
        author_id=author_id,
        guild_id=guild_id,
        channel_id=channel_id,
        author_is_bot=author_is_bot,
    )


def test_owner_direct_message_is_allowed() -> None:
    check(CaptureAllowlist(owner_user_id=OWNER))


def test_another_user_is_rejected() -> None:
    with pytest.raises(NotAllowlisted, match="owner"):
        check(CaptureAllowlist(owner_user_id=OWNER), author_id=OTHER)


def test_bot_author_is_rejected_even_when_the_id_matches() -> None:
    """Guards against the bot capturing its own acknowledgements in a loop."""
    with pytest.raises(NotAllowlisted, match="bot"):
        check(CaptureAllowlist(owner_user_id=OWNER), author_is_bot=True)


def test_guild_message_is_rejected_when_only_direct_messages_are_configured() -> None:
    with pytest.raises(NotAllowlisted, match="direct messages only"):
        check(CaptureAllowlist(owner_user_id=OWNER), guild_id=GUILD, channel_id=CHANNEL)


def test_configured_guild_and_channel_are_allowed() -> None:
    allowlist = CaptureAllowlist(owner_user_id=OWNER, guild_id=GUILD, channel_id=CHANNEL)
    check(allowlist, guild_id=GUILD, channel_id=CHANNEL)


def test_wrong_guild_is_rejected() -> None:
    allowlist = CaptureAllowlist(owner_user_id=OWNER, guild_id=GUILD, channel_id=CHANNEL)
    with pytest.raises(NotAllowlisted, match="guild"):
        check(allowlist, guild_id=999, channel_id=CHANNEL)


def test_wrong_channel_in_the_right_guild_is_rejected() -> None:
    allowlist = CaptureAllowlist(owner_user_id=OWNER, guild_id=GUILD, channel_id=CHANNEL)
    with pytest.raises(NotAllowlisted, match="channel"):
        check(allowlist, guild_id=GUILD, channel_id=999)


def test_any_channel_is_allowed_when_no_channel_is_configured() -> None:
    allowlist = CaptureAllowlist(owner_user_id=OWNER, guild_id=GUILD)
    check(allowlist, guild_id=GUILD, channel_id=42)


def test_direct_message_still_allowed_when_a_guild_is_configured() -> None:
    allowlist = CaptureAllowlist(owner_user_id=OWNER, guild_id=GUILD, channel_id=CHANNEL)
    check(allowlist, guild_id=None, channel_id=None)


def test_rejection_message_never_contains_captured_content() -> None:
    """The error is logged; message bodies must not travel inside it."""
    try:
        check(CaptureAllowlist(owner_user_id=OWNER), author_id=OTHER)
    except NotAllowlisted as exc:
        assert str(OTHER) not in str(exc)
        assert "author is not the workspace owner" in str(exc)
    else:  # pragma: no cover - the call above must raise
        pytest.fail("expected NotAllowlisted")

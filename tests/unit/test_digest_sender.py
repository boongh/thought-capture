"""``DiscordDigestSender`` against fake Discord transport objects.

Not the real SDK (that needs a live Gateway connection - see this slice's own
commit report on why the transport layer is otherwise untested); these fakes
implement just enough of ``discord.Client``/``discord.abc.Messageable`` to
prove the orchestration and, specifically, the mention-safety regression:
the digest body is model-generated from untrusted captured text, and an
``@everyone``/``@role``/``@user`` mention it happens to contain must never
actually notify anyone.
"""

from __future__ import annotations

from typing import Any

import discord

from tc_discord_bot.digest_sender import DiscordDigestSender


class FakeDestination:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.sent: list[tuple[str, discord.AllowedMentions | None]] = []
        self._fail_after = fail_after

    async def send(self, content: str, *, allowed_mentions: Any = None, **_: object) -> None:
        if self._fail_after is not None and len(self.sent) >= self._fail_after:
            raise discord.DiscordException("synthetic send failure")
        self.sent.append((content, allowed_mentions))


class FakeClient:
    def __init__(
        self,
        *,
        channel: FakeDestination | None = None,
        user: FakeDestination | None = None,
        fail_resolution: bool = False,
    ) -> None:
        self._channel = channel
        self._user = user
        self._fail_resolution = fail_resolution

    def get_channel(self, channel_id: int) -> FakeDestination | None:
        return self._channel

    async def fetch_channel(self, channel_id: int) -> FakeDestination:
        if self._fail_resolution or self._channel is None:
            raise discord.DiscordException("synthetic channel lookup failure")
        return self._channel

    async def fetch_user(self, user_id: int) -> FakeDestination:
        if self._fail_resolution or self._user is None:
            raise discord.DiscordException("synthetic user lookup failure")
        return self._user


async def test_every_chunk_is_sent_with_mentions_disabled() -> None:
    """The regression this closes: a digest chunk sent without
    ``allowed_mentions=AllowedMentions.none()`` lets a mention embedded in
    captured text actually notify people.
    """
    destination = FakeDestination()
    sender = DiscordDigestSender(
        FakeClient(user=destination),  # type: ignore[arg-type]
        channel_id=None,
        owner_user_id=1,
    )

    ok = await sender.send(("chunk one", "chunk two"))

    assert ok is True
    assert [content for content, _ in destination.sent] == ["chunk one", "chunk two"]
    # `AllowedMentions` has no value equality, so each field is checked
    # rather than comparing to a second `.none()` instance.
    for _, mentions in destination.sent:
        assert mentions is not None
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert mentions.replied_user is False


async def test_sends_to_the_configured_channel_when_set() -> None:
    destination = FakeDestination()
    sender = DiscordDigestSender(
        FakeClient(channel=destination),  # type: ignore[arg-type]
        channel_id=123,
        owner_user_id=1,
    )

    ok = await sender.send(("chunk",))

    assert ok is True
    assert destination.sent[0][0] == "chunk"


async def test_dms_the_owner_when_no_channel_is_configured() -> None:
    destination = FakeDestination()
    sender = DiscordDigestSender(
        FakeClient(user=destination),  # type: ignore[arg-type]
        channel_id=None,
        owner_user_id=42,
    )

    ok = await sender.send(("chunk",))

    assert ok is True
    assert destination.sent[0][0] == "chunk"


async def test_a_destination_resolution_failure_returns_false() -> None:
    sender = DiscordDigestSender(
        FakeClient(fail_resolution=True),  # type: ignore[arg-type]
        channel_id=None,
        owner_user_id=1,
    )

    ok = await sender.send(("chunk",))

    assert ok is False


async def test_a_partial_send_failure_returns_false() -> None:
    """A partial send is treated as a full failure - the outbox retries the
    whole digest rather than half of it silently vanishing.
    """
    destination = FakeDestination(fail_after=1)
    sender = DiscordDigestSender(
        FakeClient(user=destination),  # type: ignore[arg-type]
        channel_id=None,
        owner_user_id=1,
    )

    ok = await sender.send(("first", "second"))

    assert ok is False
    assert [content for content, _ in destination.sent] == ["first"]


async def test_no_destination_at_all_returns_false() -> None:
    sender = DiscordDigestSender(
        FakeClient(),  # type: ignore[arg-type]
        channel_id=123,
        owner_user_id=1,
    )

    ok = await sender.send(("chunk",))

    assert ok is False

"""Delivers formatted digest chunks over the Gateway connection (docs/DESIGN.md 4.2).

Transport only, like ``adapter.py``: no formatting or business rules live here.
"""

from __future__ import annotations

import logging
from typing import cast

import discord

logger = logging.getLogger(__name__)


class DiscordDigestSender:
    """Sends to the configured channel if set, otherwise DMs the owner."""

    def __init__(
        self, client: discord.Client, *, channel_id: int | None, owner_user_id: int
    ) -> None:
        self._client = client
        self._channel_id = channel_id
        self._owner_user_id = owner_user_id

    async def send(self, chunks: tuple[str, ...]) -> bool:
        destination = await self._resolve_destination()
        if destination is None:
            return False

        for chunk in chunks:
            try:
                await destination.send(chunk)
            except discord.DiscordException as exc:
                # Sanitized: a Discord exception can carry request URLs and
                # response bodies (docs/DESIGN.md 14.2).
                logger.warning("digest.send_failed", extra={"error_class": type(exc).__name__})
                return False
        return True

    async def _resolve_destination(self) -> discord.abc.Messageable | None:
        try:
            if self._channel_id is not None:
                channel = self._client.get_channel(self._channel_id)
                if channel is None:
                    channel = await self._client.fetch_channel(self._channel_id)
                # The configured channel is an operator-supplied text channel;
                # discord.py's return type is broader than that (it also
                # covers e.g. forum and category channels) because the same
                # method serves every channel kind.
                return cast(discord.abc.Messageable, channel)
            return await self._client.fetch_user(self._owner_user_id)
        except discord.DiscordException as exc:
            logger.warning(
                "digest.destination_unresolved", extra={"error_class": type(exc).__name__}
            )
            return None

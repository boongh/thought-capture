"""The Discord Gateway client.

Acknowledges only after the capture use case reports a durable commit
(docs/DESIGN.md 3.1). If the commit does not happen, nothing is acknowledged and
the failure is visible rather than silent.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from tc_application.capture import CaptureThought
from tc_discord_bot.adapter import acknowledgement_for, command_from, facts_from
from tc_domain.capture import (
    CaptureCommand,
    CaptureResult,
    ThoughtId,
    UserId,
    WorkspaceId,
)
from tc_domain.errors import AttachmentRejected, CaptureRejected, NotAllowlisted
from tc_domain.policy import CaptureAllowlist
from tc_infrastructure.db.outbox import PostgresOutbox
from tc_infrastructure.db.thought_repository import THOUGHT_CAPTURED_EVENT

logger = logging.getLogger(__name__)

# A capture must acknowledge within five seconds at p95 (docs/DESIGN.md 2.1),
# so the retry budget is small and quick. Beyond it, the user is told plainly.
COMMIT_ATTEMPTS = 3
COMMIT_BACKOFF_SECONDS = (0.25, 1.0)


def required_intents() -> discord.Intents:
    """Only what capture needs.

    MESSAGE_CONTENT is privileged and must be enabled in the developer portal;
    without it message bodies arrive empty. Member and presence intents are
    deliberately not requested (docs/DESIGN.md 12.2).
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.messages = True
    intents.dm_messages = True
    intents.message_content = True
    return intents


class CaptureClient(discord.Client):
    """Listens for allowlisted messages and captures them."""

    def __init__(
        self,
        *,
        capture: CaptureThought,
        outbox: PostgresOutbox,
        allowlist: CaptureAllowlist,
        workspace_id: WorkspaceId,
        user_id: UserId,
        workspace_timezone: str,
    ) -> None:
        super().__init__(intents=required_intents())
        self._capture = capture
        self._outbox = outbox
        self._allowlist = allowlist
        self._workspace_id = workspace_id
        self._user_id = user_id
        self._timezone = workspace_timezone

    async def on_ready(self) -> None:
        # The bot's own identity is not personal memory content, but the owner's
        # account ID is, so it is not logged.
        logger.info(
            "discord.ready",
            extra={"bot_user": str(self.user), "workspace_id": str(self._workspace_id)},
        )

    async def on_message(self, message: discord.Message) -> None:
        facts = facts_from(message)

        try:
            self._allowlist.check(
                author_id=facts.author_id,
                guild_id=facts.guild_id,
                channel_id=facts.channel_id,
                author_is_bot=facts.author_is_bot,
            )
        except NotAllowlisted as rejection:
            # Audited without storing the content (docs/DESIGN.md 4.1). The
            # reason carries no message text and no account identifier.
            logger.info("capture.ignored", extra={"reason": str(rejection)})
            return

        command = command_from(
            message,
            workspace_id=self._workspace_id,
            user_id=self._user_id,
            workspace_timezone=self._timezone,
        )

        try:
            result = await self._capture_with_retry(command)
        except AttachmentRejected as rejection:
            await self._reply(message, f"⚠️ not captured: {rejection.reason}")
            return
        except CaptureRejected as rejection:
            logger.info("capture.rejected", extra={"reason_class": type(rejection).__name__})
            return
        except Exception:
            # No acknowledgement, because nothing is known to be committed.
            logger.exception("capture.failed")
            await self._reply(
                message,
                "❌ not captured — the store did not confirm the write. "
                "Your message is safe in Discord; send it again once the system is back.",
            )
            return

        await self._reply(
            message,
            acknowledgement_for(
                thought_id=int(result.thought_id),
                attachment_count=result.attachment_count,
                deduplicated=result.deduplicated,
            ),
        )
        await self._settle_acknowledgement(result.thought_id)

    async def _capture_with_retry(self, command: CaptureCommand) -> CaptureResult:
        """Retry transient store failures with bounded backoff.

        Domain rejections are not retried: an oversized attachment will still be
        oversized a second later.
        """
        last: Exception | None = None
        for attempt in range(COMMIT_ATTEMPTS):
            try:
                return await self._capture.execute(command)
            except CaptureRejected:
                raise
            except Exception as exc:
                last = exc
                if attempt < len(COMMIT_BACKOFF_SECONDS):
                    await asyncio.sleep(COMMIT_BACKOFF_SECONDS[attempt])
        assert last is not None
        raise last

    async def _settle_acknowledgement(self, thought_id: ThoughtId) -> None:
        """Mark the queued acknowledgement delivered, since we just sent it.

        Best-effort: if this fails, the outbox consumer sends a duplicate
        acknowledgement later. A duplicate reply is cosmetic; a missing one
        would break the promise that capture is confirmed.
        """
        try:
            event_id = await self._outbox.find_pending(THOUGHT_CAPTURED_EVENT, str(thought_id))
            if event_id is not None:
                await self._outbox.mark_delivered(event_id)
        except Exception:
            logger.warning("outbox.settle_failed", exc_info=True)

    async def _reply(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(text, mention_author=False)
        except discord.DiscordException:
            # The thought is committed regardless; the outbox will retry the
            # acknowledgement.
            logger.warning("discord.reply_failed", exc_info=True)

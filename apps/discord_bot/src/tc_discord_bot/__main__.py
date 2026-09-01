"""Discord bot entrypoint.

    python -m tc_discord_bot

Assumes the bootstrap step has already run: the schema exists and the owner's
Discord account is mapped to a workspace. It connects as the least-privilege
application role and cannot create either.
"""

from __future__ import annotations

import logging
import sys

import httpx

from tc_application.capture import CaptureThought
from tc_application.digest_delivery import DeliverDigests
from tc_discord_bot.client import CaptureClient
from tc_discord_bot.digest_loop import DigestDeliveryLoop
from tc_discord_bot.digest_sender import DiscordDigestSender
from tc_domain.policy import AttachmentPolicy, CaptureAllowlist
from tc_infrastructure.config import Settings, get_settings
from tc_infrastructure.db.digest_outbox import PostgresDigestOutbox
from tc_infrastructure.db.digest_reader import PostgresDigestReader
from tc_infrastructure.db.engine import create_engine, create_session_factory
from tc_infrastructure.db.identity import resolve_identity
from tc_infrastructure.db.outbox import PostgresOutbox
from tc_infrastructure.db.thought_repository import PostgresThoughtRepository
from tc_infrastructure.runtime import run
from tc_infrastructure.storage.attachment_archive import HttpAttachmentArchive
from tc_infrastructure.storage.blob_store import FilesystemBlobStore

logger = logging.getLogger(__name__)

DISCORD_PROVIDER = "discord"


class MissingBotTokenError(RuntimeError):
    """The bot cannot start without a token."""


def build_allowlist(settings: Settings) -> CaptureAllowlist:
    if settings.discord_owner_user_id is None:
        raise RuntimeError("TC_DISCORD_OWNER_USER_ID must be set")
    return CaptureAllowlist(
        owner_user_id=settings.discord_owner_user_id,
        guild_id=settings.discord_guild_id,
        channel_id=settings.discord_channel_id,
    )


async def serve(settings: Settings) -> None:
    token = settings.discord_bot_token.get_secret_value()
    if not token:
        raise MissingBotTokenError("TC_DISCORD_BOT_TOKEN must be set")

    allowlist = build_allowlist(settings)
    engine = create_engine(settings)
    http = httpx.AsyncClient()

    try:
        sessions = create_session_factory(engine)

        # Fatal if absent: a bot that cannot resolve its owner would ignore
        # every message, which looks exactly like a broken allowlist.
        identity = await resolve_identity(
            sessions,
            provider=DISCORD_PROVIDER,
            external_user_id=str(settings.discord_owner_user_id),
        )

        repository = PostgresThoughtRepository(sessions)
        archive = HttpAttachmentArchive(
            FilesystemBlobStore(settings.attachment_root),
            http,
            max_bytes=settings.attachment_max_bytes,
        )
        capture = CaptureThought(
            repository,
            archive,
            AttachmentPolicy(max_bytes=settings.attachment_max_bytes),
        )
        outbox = PostgresOutbox(sessions, lease_owner="discord-bot")

        client = CaptureClient(
            capture=capture,
            outbox=outbox,
            allowlist=allowlist,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            workspace_timezone=settings.workspace_timezone,
        )

        assert settings.discord_owner_user_id is not None  # enforced by build_allowlist above
        deliver_digests = DeliverDigests(
            outbox=PostgresDigestOutbox(sessions, lease_owner="discord-bot"),
            source=PostgresDigestReader(sessions),
            sender=DiscordDigestSender(
                client,
                channel_id=settings.discord_channel_id,
                owner_user_id=settings.discord_owner_user_id,
            ),
        )
        client.attach_digest_loop(DigestDeliveryLoop(deliver_digests))

        # discord.py installs its own logging; keep it at INFO so a Gateway
        # disconnect is visible without dumping message content.
        await client.start(token)
    finally:
        await http.aclose()
        await engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        run(serve(get_settings()))
    except KeyboardInterrupt:
        logger.info("discord.shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())

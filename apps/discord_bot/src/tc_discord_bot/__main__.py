"""Discord bot entrypoint.

    python -m tc_discord_bot

Assumes the bootstrap step has already run: the schema exists and the owner's
Discord account is mapped to a workspace. It connects as the least-privilege
application role and cannot create either.
"""

from __future__ import annotations

import logging
import sys
import uuid

import httpx

from tc_application.ask import AskQuestion
from tc_application.capture import CaptureThought
from tc_application.digest_delivery import DeliverDigests
from tc_application.organize import JournalFactory, OrganizeWindow
from tc_application.search import Search
from tc_application.structured import JournalWriter
from tc_discord_bot.client import CaptureClient
from tc_discord_bot.commands import build_admin_commands
from tc_discord_bot.digest_loop import DigestDeliveryLoop
from tc_discord_bot.digest_sender import DiscordDigestSender
from tc_domain.capture import WorkspaceId
from tc_domain.context import ContextAssemblyConfig
from tc_domain.llm import LLMRequest, LLMResponse
from tc_domain.policy import AttachmentPolicy, CaptureAllowlist
from tc_infrastructure.config import Settings, get_settings
from tc_infrastructure.db.context_index import PostgresContextIndex
from tc_infrastructure.db.digest_outbox import PostgresDigestOutbox
from tc_infrastructure.db.digest_reader import PostgresDigestReader
from tc_infrastructure.db.engine import create_engine, create_session_factory
from tc_infrastructure.db.entity_repository import PostgresEntityRepository
from tc_infrastructure.db.identity import resolve_identity
from tc_infrastructure.db.llm_journal import PostgresLLMJournal
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.outbox import PostgresOutbox
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.run_reader import PostgresRunReader
from tc_infrastructure.db.search_reader import PostgresExactSearch
from tc_infrastructure.db.semantic_hydrator import PostgresSemanticHydrator
from tc_infrastructure.db.thought_reader import PostgresThoughtReader
from tc_infrastructure.db.thought_repository import PostgresThoughtRepository
from tc_infrastructure.db.windows import PostgresCaptureWindows
from tc_infrastructure.khoj.client import HttpKhojClient
from tc_infrastructure.llm.factory import build_organize_provider, build_select_provider
from tc_infrastructure.runtime import run
from tc_infrastructure.storage.attachment_archive import HttpAttachmentArchive
from tc_infrastructure.storage.blob_store import FilesystemBlobStore

logger = logging.getLogger(__name__)

DISCORD_PROVIDER = "discord"


class MissingBotTokenError(RuntimeError):
    """The bot cannot start without a token."""


def _journal_factory(journal: PostgresLLMJournal) -> JournalFactory:
    """Identical to tc_worker.__main__'s own factory - the bot runs the exact
    same OrganizeWindow the scheduler does, not a second implementation."""

    def make(workspace_id: WorkspaceId, run_id: uuid.UUID) -> JournalWriter:
        async def write(request: LLMRequest, response: LLMResponse, attempt: int) -> None:
            await journal.record(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request,
                response=response,
                sequence=attempt,
            )

        return write

    return make


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

        thoughts_for_organize = PostgresThoughtReader(sessions)
        journal = PostgresLLMJournal(sessions)
        organize = OrganizeWindow(
            thoughts=thoughts_for_organize,
            context_index=PostgresContextIndex(sessions),
            provider=build_organize_provider(settings),
            # Matches tc_worker.__main__'s wiring exactly: without this, a
            # manual /organize run would silently fall back to the organize
            # provider for context-assembly's "select" call (OrganizeWindow's
            # own `select_provider or provider` default), not the separately
            # reviewed select-stage model docs/adr/0006 and PR #16 wire in -
            # a real behavioral gap the review caught, not an intentional
            # difference between a manual and a scheduled run.
            select_provider=build_select_provider(settings),
            journal_factory=_journal_factory(journal),
            writer=PostgresOrganizeWriter(sessions, entities=PostgresEntityRepository()),
            run_ledger=PostgresRunLedger(sessions),
            usage_for=journal.usage_for,
            config=ContextAssemblyConfig(
                max_selected_documents=settings.context_max_selected_documents,
                recency_days=settings.context_recency_days,
            ),
            reasoning_effort=settings.reasoning_effort_for(settings.model_organize),
        )
        khoj = HttpKhojClient(http, settings.khoj_base_url)
        hydrator = PostgresSemanticHydrator(sessions)
        exact_search = PostgresExactSearch(sessions)
        admin_commands = build_admin_commands(
            organize=organize,
            windows=PostgresCaptureWindows(sessions),
            runs=PostgresRunReader(sessions),
            first_capture_at=thoughts_for_organize.first_capture_at,
            workspace_id=identity.workspace_id,
            owner_user_id=settings.discord_owner_user_id,
            digest_local_time=settings.digest_local_time,
            timezone=settings.workspace_timezone,
            search=Search(exact_search, khoj, hydrator),
            ask=AskQuestion(khoj, hydrator, exact_search, enabled=settings.ask_enabled),
        )
        client.attach_admin_commands(admin_commands, guild_id=settings.discord_guild_id)

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

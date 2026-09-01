"""Worker entrypoint: the organize scheduler.

    python -m tc_worker

Assumes the bootstrap step (``python -m tc_worker.bootstrap``) has already run:
the schema exists and the owner is mapped to a workspace. Connects as the
least-privilege application role and cannot create either.

Catches up every closed, unprocessed window on startup (docs/DESIGN.md 4.2),
then arms the scheduler for the next cutoff and runs until interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid

from tc_application.organize import JournalFactory, OrganizeWindow
from tc_application.structured import JournalWriter
from tc_domain.capture import WorkspaceId
from tc_domain.context import ContextAssemblyConfig
from tc_domain.llm import LLMRequest, LLMResponse
from tc_infrastructure.config import Settings, get_settings
from tc_infrastructure.db.context_index import PostgresContextIndex
from tc_infrastructure.db.engine import create_engine, create_session_factory
from tc_infrastructure.db.entity_repository import PostgresEntityRepository
from tc_infrastructure.db.identity import resolve_identity
from tc_infrastructure.db.llm_journal import PostgresLLMJournal
from tc_infrastructure.db.organize_writer import PostgresOrganizeWriter
from tc_infrastructure.db.run_ledger import PostgresRunLedger
from tc_infrastructure.db.thought_reader import PostgresThoughtReader
from tc_infrastructure.db.windows import PostgresCaptureWindows
from tc_infrastructure.llm.factory import build_organize_provider
from tc_infrastructure.runtime import run
from tc_worker.scheduler import OrganizeScheduler

logger = logging.getLogger(__name__)

DISCORD_PROVIDER = "discord"


def _journal_factory(journal: PostgresLLMJournal) -> JournalFactory:
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


async def serve(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        sessions = create_session_factory(engine)

        identity = await resolve_identity(
            sessions,
            provider=DISCORD_PROVIDER,
            external_user_id=str(settings.discord_owner_user_id),
        )

        journal = PostgresLLMJournal(sessions)
        thoughts = PostgresThoughtReader(sessions)

        organize = OrganizeWindow(
            thoughts=thoughts,
            context_index=PostgresContextIndex(sessions),
            provider=build_organize_provider(settings),
            journal_factory=_journal_factory(journal),
            writer=PostgresOrganizeWriter(sessions, entities=PostgresEntityRepository()),
            run_ledger=PostgresRunLedger(sessions),
            usage_for=journal.usage_for,
            config=ContextAssemblyConfig(
                max_selected_documents=settings.context_max_selected_documents,
                recency_days=settings.context_recency_days,
            ),
        )

        windows = PostgresCaptureWindows(sessions)

        scheduler = OrganizeScheduler(
            windows=windows,
            organize=organize,
            first_capture=thoughts.first_capture_at,
            workspace_id=identity.workspace_id,
            digest_local_time=settings.digest_local_time,
            timezone=settings.workspace_timezone,
        )

        await scheduler.catch_up()
        scheduler.start()
        scheduler.schedule_next()

        logger.info("worker.ready", extra={"workspace_id": str(identity.workspace_id)})
        try:
            await asyncio.Event().wait()
        finally:
            scheduler.shutdown()
    finally:
        await engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        run(serve(get_settings()))
    except KeyboardInterrupt:
        logger.info("worker.shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())

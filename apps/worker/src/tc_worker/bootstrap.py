"""One-shot bootstrap: apply migrations, then seed the workspace.

Runs with the migration role, before any service starts (docs/DESIGN.md 13).
Both steps are idempotent, so this is safe to run on every deployment.

    python -m tc_worker.bootstrap
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tc_infrastructure.config import Settings, get_settings
from tc_infrastructure.db.seed import ensure_workspace
from tc_infrastructure.runtime import run

logger = logging.getLogger(__name__)

# apps/worker/src/tc_worker/bootstrap.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[4]


def apply_migrations(settings: Settings) -> None:
    """Bring the schema to head using the synchronous migration engine."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.migration_database_url.get_secret_value())
    # Keep the logging this process already configured; alembic.ini would
    # otherwise reset the root logger to WARNING and hide the lines below.
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    logger.info("bootstrap.migrations_applied")


async def seed_workspace(settings: Settings) -> None:
    """Create the owner, workspace, membership, and Discord identity if absent."""
    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session, session.begin():
            seeded = await ensure_workspace(session, settings)
        # NOTE: `created` is a reserved LogRecord attribute (the record's
        # timestamp); using it as an `extra` key raises KeyError.
        logger.info(
            "bootstrap.workspace_ready",
            extra={
                "workspace_id": str(seeded.workspace_id),
                "workspace_created": seeded.created,
            },
        )
    finally:
        await engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = get_settings()

    apply_migrations(settings)
    run(seed_workspace(settings))

    # Identifiers only; never the Discord account ID (docs/DESIGN.md 12.2).
    logging.getLogger(__name__).info("bootstrap.complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

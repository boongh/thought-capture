"""Integration fixtures backed by a real PostgreSQL instance.

These tests run against a disposable database created from scratch and dropped
afterwards, so they never touch the developer's canonical data. Start the server
with:

    docker compose --env-file .env -f deploy/compose/docker-compose.yml \
      --profile core up -d
"""

from __future__ import annotations

import argparse
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tc_infrastructure.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_NAME = "thought_capture_test"

# Fail fast when PostgreSQL is not listening. Without this, every test in the
# suite waits out the driver's default connect timeout, turning "the database is
# down" into a four-minute run instead of a four-second one.
CONNECT_ARGS = {"connect_timeout": 5}


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Refuse to report success when required integration tests were skipped.

    A database that is merely unreachable must never look like a passing suite.
    The check scripts set TC_REQUIRE_INTEGRATION=1 before invoking this suite,
    so any skip at all is a failure of verification, not a neutral outcome.
    """
    if os.environ.get("TC_REQUIRE_INTEGRATION") != "1":
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - defensive
        return

    skipped = reporter.stats.get("skipped", [])
    passed = reporter.stats.get("passed", [])
    if skipped:
        reporter.write_line(
            f"TC_REQUIRE_INTEGRATION=1: {len(skipped)} integration test(s) were skipped; "
            "a skipped integration suite is not verification.",
            red=True,
        )
        session.exitstatus = 1
    elif not passed:
        reporter.write_line("TC_REQUIRE_INTEGRATION=1: no integration tests ran.", red=True)
        session.exitstatus = 1


def _migration_url() -> URL:
    return make_url(get_settings().migration_database_url.get_secret_value())


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[URL]:
    """Create a disposable database for the session and drop it afterwards.

    A connection failure is deliberately allowed to propagate as an error. It
    used to be converted into a skip, which meant an unreachable database
    produced a green run.
    """
    base = _migration_url()
    maintenance = base.set(database="postgres")
    target = base.set(database=TEST_DATABASE_NAME)

    # AUTOCOMMIT: CREATE/DROP DATABASE cannot run inside a transaction block.
    admin = sa.create_engine(maintenance, isolation_level="AUTOCOMMIT", connect_args=CONNECT_ARGS)
    with admin.connect() as connection:
        _drop_database(connection)
        connection.execute(sa.text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"'))

    try:
        yield target
    finally:
        with admin.connect() as connection:
            _drop_database(connection)
        admin.dispose()


@pytest.fixture(scope="session")
def app_database_url(migrated_database: URL) -> URL:
    """The migrated test database, addressed as the least-privilege app role.

    This is the connection the running system actually uses. Exercising it is
    the only way to prove the grants in migration 0001 permit a real capture.
    """
    settings = get_settings()
    return migrated_database.set(
        username="tc_app",
        password=settings.app_db_password.get_secret_value(),
    )


def _drop_database(connection: sa.Connection) -> None:
    # WITH (FORCE) terminates leftover sessions; without it a stray connection
    # from a failed run blocks the drop and poisons every later run.
    connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE_NAME}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def migrated_database(test_database_url: URL) -> URL:
    """Apply the full first-party migration history to the disposable database."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    # env.py reads `-x url=` first, which keeps the real database untouched.
    config.cmd_opts = argparse.Namespace(x=[f"url={test_database_url.render_as_string(False)}"])
    command.upgrade(config, "head")
    return test_database_url


@pytest.fixture(scope="session")
def engine(migrated_database: URL) -> Iterator[Engine]:
    """Session-scoped engine connected as the schema-owning migration role."""
    created = sa.create_engine(migrated_database, connect_args=CONNECT_ARGS)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[sa.Connection]:
    """A connection whose work is rolled back, so tests cannot leak state."""
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
async def app_session_factory(
    app_database_url: URL,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Async session factory connected as the least-privilege application role.

    The repository commits its own transactions, so tests using this cannot rely
    on rollback isolation; they use unique source_message_id values instead and
    clean up nothing, because the log is append-only by design.
    """
    async_url = app_database_url.set(drivername="postgresql+psycopg")
    engine = create_async_engine(async_url, poolclass=NullPool, connect_args=CONNECT_ARGS)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def seeded_identity(engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    """A committed workspace and owner, created with the migration role.

    Committed rather than rolled back, because the application role connects on
    a different connection and must be able to see it. Since migration 0003 the
    application role may only read these tables.
    """
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO users (id, display_name) VALUES (:id, :name)"),
            {"id": user_id, "name": "synthetic owner"},
        )
        conn.execute(
            sa.text(
                "INSERT INTO workspaces (id, name, mode, timezone)"
                " VALUES (:id, :name, 'personal', 'Asia/Bangkok')"
            ),
            {"id": workspace_id, "name": "synthetic workspace"},
        )
        conn.execute(
            sa.text(
                "INSERT INTO workspace_memberships (workspace_id, user_id, role)"
                " VALUES (:workspace_id, :user_id, 'owner')"
            ),
            {"workspace_id": workspace_id, "user_id": user_id},
        )
    return workspace_id, user_id


@pytest.fixture
async def admin_session_factory(
    migrated_database: URL,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Async session factory as the schema-owning migration role.

    Bootstrap work - seeding the owner, workspace, and membership - runs with
    this role, not the application role. Creating identities is administrative
    (migration 0003).
    """
    async_url = migrated_database.set(drivername="postgresql+psycopg")
    engine = create_async_engine(async_url, poolclass=NullPool, connect_args=CONNECT_ARGS)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


@pytest.fixture
def unique_message_id() -> str:
    """A source_message_id no other test will collide with."""
    return f"test-{uuid.uuid4()}"

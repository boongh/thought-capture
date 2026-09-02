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
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tc_api.app import create_app
from tc_api.dependencies import ApiContext
from tc_application.capture import CaptureThought
from tc_domain.capture import UserId, WorkspaceId
from tc_domain.policy import AttachmentPolicy
from tc_infrastructure.config import Settings, get_settings
from tc_infrastructure.db.document_reader import PostgresDocumentReader
from tc_infrastructure.db.entity_reader import PostgresEntityReader
from tc_infrastructure.db.llm_call_reader import PostgresLlmCallReader
from tc_infrastructure.db.outbox import PostgresOutbox
from tc_infrastructure.db.thought_reader import PostgresThoughtReader
from tc_infrastructure.db.thought_repository import PostgresThoughtRepository
from tests.integration.support import CONNECT_ARGS
from tests.unit.fakes import FakeAttachmentArchive

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_NAME = "thought_capture_test"

API_TOKEN = "test-bearer-token-value"


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
async def fresh_identity(
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """A committed workspace and owner, private to one test.

    Unlike ``seeded_identity`` (session-scoped, shared by every integration
    test), this workspace belongs to nobody else - for a test whose own
    ``runs`` rows would otherwise collide with another file's workspace-wide
    cleanup logic (organize-pipeline tests, notably, whose runs always carry
    ``window_start``/``window_end`` and now have ``document_revisions``
    pointing at them, which a same-workspace ``DELETE FROM runs`` elsewhere
    cannot get past).
    """
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.text("INSERT INTO users (id, display_name) VALUES (:id, :name)"),
            {"id": user_id, "name": "synthetic owner"},
        )
        await session.execute(
            sa.text(
                "INSERT INTO workspaces (id, name, mode, timezone)"
                " VALUES (:id, :name, 'personal', 'Asia/Bangkok')"
            ),
            {"id": workspace_id, "name": "synthetic workspace"},
        )
        await session.execute(
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


async def _api_client(
    app_session_factory: async_sessionmaker[AsyncSession], identity: tuple[uuid.UUID, uuid.UUID]
) -> AsyncIterator[httpx.AsyncClient]:
    """The real app, wired to the test database with a known bearer token.

    Shared by every ``/v1`` and ``/debug`` test file, since the lifespan
    replacement and ``ApiContext`` construction is identical across all of
    them - duplicating it per file would mean every new reader added to
    ``ApiContext`` needs updating in every copy.

    The lifespan is replaced so the test does not depend on a seeded Discord
    identity or a live HTTP client; everything else is the production code path.
    """
    workspace_id, user_id = identity
    settings = Settings(
        _env_file=None, api_bearer_token=API_TOKEN, workspace_timezone="Asia/Bangkok"
    )

    context = ApiContext(
        settings=settings,
        capture=CaptureThought(
            PostgresThoughtRepository(app_session_factory),
            FakeAttachmentArchive(),
            AttachmentPolicy(max_bytes=1024),
        ),
        reader=PostgresThoughtReader(app_session_factory),
        documents=PostgresDocumentReader(app_session_factory),
        entities=PostgresEntityReader(app_session_factory),
        llm_calls=PostgresLlmCallReader(app_session_factory),
        outbox=PostgresOutbox(app_session_factory, lease_owner="test-api"),
        session_factory=app_session_factory,
        workspace_id=WorkspaceId(workspace_id),
        user_id=UserId(user_id),
    )

    @asynccontextmanager
    async def no_startup(_: object) -> AsyncIterator[None]:
        """Skip the production lifespan; the context is injected above."""
        yield

    app = create_app(lifespan_handler=no_startup)
    app.state.context = context

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def api(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> AsyncIterator[httpx.AsyncClient]:
    """API client scoped to the shared, session-wide workspace.

    Use this for read-only checks and writes that never set ``runs.window_start``
    (e.g. plain entity resolution) - anything that does must use ``api_fresh``
    instead, or it collides with ``test_organize_scheduler.py``'s cleanup of
    windowed runs in this same shared workspace.
    """
    async for client in _api_client(app_session_factory, seeded_identity):
        yield client


@pytest.fixture
async def api_fresh(
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> AsyncIterator[httpx.AsyncClient]:
    """API client scoped to a private, per-test workspace.

    Required for any test that writes through ``PostgresOrganizeWriter`` /
    ``PostgresRunLedger``, since those always set ``runs.window_start`` -
    see ``fresh_identity``'s own docstring for why that collides with the
    shared workspace.
    """
    async for client in _api_client(app_session_factory, fresh_identity):
        yield client

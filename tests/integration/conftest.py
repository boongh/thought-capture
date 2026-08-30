"""Integration fixtures backed by a real PostgreSQL instance.

These tests run against a disposable database created from scratch and dropped
afterwards, so they never touch the developer's canonical data. Start the server
with:

    docker compose --profile core -f deploy/compose/docker-compose.yml up -d
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, Engine, make_url

from tc_infrastructure.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_NAME = "thought_capture_test"


def _migration_url() -> URL:
    return make_url(get_settings().migration_database_url.get_secret_value())


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[URL]:
    """Create a disposable database for the session and drop it afterwards."""
    base = _migration_url()
    maintenance = base.set(database="postgres")
    target = base.set(database=TEST_DATABASE_NAME)

    # AUTOCOMMIT: CREATE/DROP DATABASE cannot run inside a transaction block.
    admin = sa.create_engine(maintenance, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            _drop_database(connection)
            connection.execute(sa.text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"'))
    except sa.exc.OperationalError as exc:
        pytest.skip(f"PostgreSQL is not reachable at {maintenance.render_as_string()}: {exc}")

    try:
        yield target
    finally:
        with admin.connect() as connection:
            _drop_database(connection)
        admin.dispose()


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
    created = sa.create_engine(migrated_database)
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

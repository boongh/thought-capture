"""The migration history must be reversible and re-appliable.

docs/DESIGN.md Appendix B defines a safe change as one where "migrations work
from every supported release". A downgrade path that was never executed is not a
downgrade path, so this exercises upgrade -> downgrade -> upgrade against a
throwaway database of its own.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url

from tc_infrastructure.config import get_settings
from tests.integration.support import CONNECT_ARGS

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUNDTRIP_DATABASE_NAME = "thought_capture_roundtrip"

FIRST_PARTY_TABLES = {
    "users",
    "workspaces",
    "workspace_memberships",
    "external_identities",
    "thoughts",
    "blobs",
    "thought_attachments",
}


def _alembic_config(url: URL) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.cmd_opts = argparse.Namespace(x=[f"url={url.render_as_string(False)}"])
    return config


@pytest.fixture
def roundtrip_url() -> Iterator[URL]:
    base = make_url(get_settings().migration_database_url.get_secret_value())
    maintenance = base.set(database="postgres")
    target = base.set(database=ROUNDTRIP_DATABASE_NAME)

    admin = sa.create_engine(maintenance, isolation_level="AUTOCOMMIT", connect_args=CONNECT_ARGS)
    drop = sa.text(f'DROP DATABASE IF EXISTS "{ROUNDTRIP_DATABASE_NAME}" WITH (FORCE)')
    # A connection failure propagates as an error rather than a skip: an
    # unreachable database is a failed verification, not an absent one.
    with admin.connect() as connection:
        connection.execute(drop)
        connection.execute(sa.text(f'CREATE DATABASE "{ROUNDTRIP_DATABASE_NAME}"'))

    try:
        yield target
    finally:
        with admin.connect() as connection:
            connection.execute(drop)
        admin.dispose()


def _public_tables(url: URL) -> set[str]:
    engine = sa.create_engine(url, connect_args=CONNECT_ARGS)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_is_clean(roundtrip_url: URL) -> None:
    config = _alembic_config(roundtrip_url)

    command.upgrade(config, "head")
    assert _public_tables(roundtrip_url) >= FIRST_PARTY_TABLES

    command.downgrade(config, "base")
    remaining = _public_tables(roundtrip_url) & FIRST_PARTY_TABLES
    assert not remaining, f"downgrade left tables behind: {sorted(remaining)}"

    # Re-applying proves the downgrade did not leave a partially-dropped state
    # (an orphaned trigger function or index would fail this second upgrade).
    command.upgrade(config, "head")
    assert _public_tables(roundtrip_url) >= FIRST_PARTY_TABLES


def test_migration_0008_downgrade_aborts_before_any_ddl_when_an_embedding_sync_run_exists(
    roundtrip_url: URL,
) -> None:
    """Migration 0008's downgrade() drops `document_embeddings` and then
    restores a `runs_kind_check` that no longer allows `kind='embedding_sync'`
    (review finding). Without a preflight, that leaves two bad outcomes: the
    ADD CONSTRAINT statement fails outright after the table is already gone,
    or - if someone "fixes" it by deleting the offending rows first - real
    run-journal history is silently destroyed just to make a downgrade
    possible. This proves the preflight added to downgrade() catches the
    unsafe case and refuses BEFORE dropping anything at all.
    """
    config = _alembic_config(roundtrip_url)
    command.upgrade(config, "head")

    engine = sa.create_engine(roundtrip_url, connect_args=CONNECT_ARGS)
    try:
        with engine.begin() as connection:
            workspace_id = connection.execute(
                sa.text(
                    "INSERT INTO workspaces (id, name, mode, timezone, digest_local_time)"
                    " VALUES (gen_random_uuid(), 'synthetic workspace', 'personal', 'UTC', '20:00')"
                    " RETURNING id"
                )
            ).scalar_one()
            connection.execute(
                sa.text(
                    "INSERT INTO runs (id, workspace_id, kind, status)"
                    " VALUES (gen_random_uuid(), :workspace_id, 'embedding_sync', 'succeeded')"
                ),
                {"workspace_id": workspace_id},
            )

        assert "document_embeddings" in _public_tables(roundtrip_url)

        with pytest.raises(Exception, match="embedding_sync"):
            command.downgrade(config, "0007")

        # The preflight must have refused BEFORE the DROP TABLE ran - not
        # merely failed later on the ADD CONSTRAINT, which would already
        # have destroyed the table by that point.
        assert "document_embeddings" in _public_tables(roundtrip_url), (
            "downgrade dropped document_embeddings despite an unsafe "
            "embedding_sync run existing - the preflight did not run before "
            "the destructive DDL"
        )

        with engine.begin() as connection:
            remaining = connection.execute(
                sa.text("SELECT count(*) FROM runs WHERE kind = 'embedding_sync'")
            ).scalar_one()
        assert remaining == 1, (
            "the embedding_sync run itself must be untouched by the refused downgrade"
        )
    finally:
        engine.dispose()

"""End-to-end privilege check as the least-privilege application role.

The other schema tests connect as the schema owner, which proves what the
grants *say*. This one connects as ``tc_app`` - the role the running system
actually uses - and proves what the grants *do*.

That distinction matters concretely here. ``thoughts.id`` is
``GENERATED ALWAYS AS IDENTITY``. Had it been ``bigserial``, an INSERT would
need separate ``USAGE`` on the backing sequence and would fail with
"permission denied for sequence" despite the table grant. PostgreSQL treats an
identity column's sequence as internally owned and does not check it
separately, so no sequence grant is required - but that is a fact about the
column kind, not something to take on trust. The final test in this module pins
the distinction so that changing the column to ``serial`` cannot silently break
capture.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, Engine

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def app_engine(app_database_url: URL) -> Iterator[Engine]:
    engine = sa.create_engine(app_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def app_connection(app_engine: Engine) -> Iterator[sa.Connection]:
    """A tc_app connection whose work is rolled back after each test."""
    with app_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _insert_thought(
    connection: sa.Connection,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    source_message_id: str,
) -> int:
    return int(
        connection.execute(
            sa.text("""
                INSERT INTO thoughts (
                  workspace_id, author_user_id, source, source_message_id, body,
                  client_created_at, client_timezone, client_local_date, client_local_time
                ) VALUES (
                  :workspace_id, :user_id, 'discord', :source_message_id,
                  'synthetic thought',
                  '2026-08-30T13:00:00+00:00', 'Asia/Bangkok', '2026-08-30', '20:00'
                ) RETURNING id
            """),
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "source_message_id": source_message_id,
            },
        ).scalar_one()
    )


def test_application_role_connects(app_connection: sa.Connection) -> None:
    who = app_connection.execute(sa.text("SELECT current_user")).scalar_one()
    assert who == "tc_app"


def test_application_role_can_append_a_thought(
    app_connection: sa.Connection, seeded_identity: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The real capture path: INSERT ... RETURNING id as tc_app.

    This is the test that would have caught a missing sequence grant.
    """
    workspace_id, user_id = seeded_identity

    thought_id = _insert_thought(
        app_connection, workspace_id, user_id, source_message_id="app-role-1"
    )

    assert thought_id > 0
    body = app_connection.execute(
        sa.text("SELECT body FROM thoughts WHERE id = :id"), {"id": thought_id}
    ).scalar_one()
    assert body == "synthetic thought"


def test_application_role_can_archive_an_attachment(
    app_connection: sa.Connection, seeded_identity: tuple[uuid.UUID, uuid.UUID]
) -> None:
    workspace_id, user_id = seeded_identity
    thought_id = _insert_thought(
        app_connection, workspace_id, user_id, source_message_id="app-role-2"
    )
    digest = "a" * 64

    app_connection.execute(
        sa.text("""
            INSERT INTO blobs (sha256, size_bytes, media_type, storage_key)
            VALUES (:sha, 1024, 'image/png', :key)
        """),
        {"sha": digest, "key": f"{digest[:2]}/{digest}"},
    )
    app_connection.execute(
        sa.text("""
            INSERT INTO thought_attachments (thought_id, workspace_id, blob_sha256, source_filename)
            VALUES (:thought_id, :workspace_id, :sha, 'note.png')
        """),
        {"thought_id": thought_id, "workspace_id": workspace_id, "sha": digest},
    )

    status = app_connection.execute(
        sa.text("SELECT extracted_status FROM thought_attachments WHERE thought_id = :id"),
        {"id": thought_id},
    ).scalar_one()
    assert status == "not_supported"


def test_application_role_insert_is_idempotent_by_constraint(
    app_connection: sa.Connection, seeded_identity: tuple[uuid.UUID, uuid.UUID]
) -> None:
    workspace_id, user_id = seeded_identity
    _insert_thought(app_connection, workspace_id, user_id, source_message_id="app-role-dup")

    with pytest.raises(sa.exc.IntegrityError):
        _insert_thought(app_connection, workspace_id, user_id, source_message_id="app-role-dup")


def test_application_role_cannot_update_a_committed_thought(
    app_connection: sa.Connection, seeded_identity: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Denied twice over: no UPDATE grant, and the trigger would reject it too."""
    workspace_id, user_id = seeded_identity
    thought_id = _insert_thought(
        app_connection, workspace_id, user_id, source_message_id="app-role-3"
    )

    with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
        app_connection.execute(
            sa.text("UPDATE thoughts SET body = 'rewritten' WHERE id = :id"),
            {"id": thought_id},
        )


def test_application_role_cannot_delete_a_committed_thought(
    app_connection: sa.Connection, seeded_identity: tuple[uuid.UUID, uuid.UUID]
) -> None:
    workspace_id, user_id = seeded_identity
    thought_id = _insert_thought(
        app_connection, workspace_id, user_id, source_message_id="app-role-4"
    )

    with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
        app_connection.execute(sa.text("DELETE FROM thoughts WHERE id = :id"), {"id": thought_id})


def test_application_role_cannot_create_a_table(app_connection: sa.Connection) -> None:
    with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
        app_connection.execute(sa.text("CREATE TABLE tc_app_should_not_manage (id int)"))


def test_thought_id_is_an_identity_column_not_a_serial(connection: sa.Connection) -> None:
    """Pins the column kind that makes the grants above sufficient.

    A ``serial`` column would need ``GRANT USAGE ON SEQUENCE`` in addition to
    the table grant. If someone changes this column, this test fails and points
    at the grant that would need to be added alongside it.
    """
    attidentity = connection.execute(
        sa.text("""
            SELECT attidentity
            FROM pg_attribute
            WHERE attrelid = 'thoughts'::regclass AND attname = 'id'
        """)
    ).scalar_one()
    assert attidentity == "a", (
        "thoughts.id must be GENERATED ALWAYS AS IDENTITY. A serial column would "
        "additionally require GRANT USAGE ON SEQUENCE thoughts_id_seq TO tc_app."
    )

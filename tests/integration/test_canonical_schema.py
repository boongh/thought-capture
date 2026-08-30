"""Asserts that migration 0001 delivers the guarantees docs/DESIGN.md 6.2 claims.

The append-only invariant is the foundation of the whole system: "Raw thoughts
and original attachments are canonical and append-only" (CLAUDE.md). These tests
prove the database enforces it, rather than trusting that no future code will
issue an UPDATE.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "users",
    "workspaces",
    "workspace_memberships",
    "external_identities",
    "thoughts",
    "blobs",
    "thought_attachments",
}


def _seed_workspace_and_user(connection: sa.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    connection.execute(
        sa.text("INSERT INTO users (id, display_name) VALUES (:id, :name)"),
        {"id": user_id, "name": "synthetic owner"},
    )
    connection.execute(
        sa.text(
            "INSERT INTO workspaces (id, name, mode, timezone)"
            " VALUES (:id, :name, 'personal', 'Asia/Bangkok')"
        ),
        {"id": workspace_id, "name": "synthetic workspace"},
    )
    return workspace_id, user_id


def _insert_thought(
    connection: sa.Connection,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    source_message_id: str,
    body: str = "synthetic thought",
) -> int:
    result = connection.execute(
        sa.text("""
            INSERT INTO thoughts (
              workspace_id, author_user_id, source, source_message_id, body,
              client_created_at, client_timezone, client_local_date, client_local_time
            ) VALUES (
              :workspace_id, :user_id, 'discord', :source_message_id, :body,
              '2026-08-30T13:00:00+00:00', 'Asia/Bangkok', '2026-08-30', '20:00'
            ) RETURNING id
        """),
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "source_message_id": source_message_id,
            "body": body,
        },
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_expected_tables_exist(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    present = {row[0] for row in rows}
    assert present >= EXPECTED_TABLES, f"missing tables: {EXPECTED_TABLES - present}"


def test_pg_trgm_is_installed(connection: sa.Connection) -> None:
    installed = connection.execute(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
    ).scalar_one_or_none()
    assert installed == 1


def test_body_tsv_is_a_stored_generated_column(connection: sa.Connection) -> None:
    """PostgreSQL 18 defaults generated columns to VIRTUAL, which cannot be indexed.

    A virtual column here would silently disable the full-text index and make
    exact search quietly wrong, so the storage kind is asserted directly.
    """
    attgenerated = connection.execute(
        sa.text("""
            SELECT attgenerated
            FROM pg_attribute
            WHERE attrelid = 'thoughts'::regclass AND attname = 'body_tsv'
        """)
    ).scalar_one()
    assert attgenerated == "s", (
        "thoughts.body_tsv must be STORED; a VIRTUAL generated column cannot back an index"
    )


@pytest.mark.parametrize(
    "index_name",
    ["thoughts_workspace_time_idx", "thoughts_tsv_idx", "thoughts_trgm_idx"],
)
def test_retrieval_indexes_exist(connection: sa.Connection, index_name: str) -> None:
    found = connection.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"),
        {"name": index_name},
    ).scalar_one_or_none()
    assert found == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_duplicate_source_message_is_rejected(connection: sa.Connection) -> None:
    """Redelivery of the same Discord message must not create a second thought."""
    workspace_id, user_id = _seed_workspace_and_user(connection)
    _insert_thought(connection, workspace_id, user_id, source_message_id="dup-1")

    with pytest.raises(sa.exc.IntegrityError):
        _insert_thought(connection, workspace_id, user_id, source_message_id="dup-1")


# ---------------------------------------------------------------------------
# Append-only enforcement
# ---------------------------------------------------------------------------


def test_update_of_a_thought_is_rejected(connection: sa.Connection) -> None:
    workspace_id, user_id = _seed_workspace_and_user(connection)
    thought_id = _insert_thought(connection, workspace_id, user_id, source_message_id="upd-1")

    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        connection.execute(
            sa.text("UPDATE thoughts SET body = 'rewritten' WHERE id = :id"),
            {"id": thought_id},
        )


def test_delete_of_a_thought_is_rejected(connection: sa.Connection) -> None:
    workspace_id, user_id = _seed_workspace_and_user(connection)
    thought_id = _insert_thought(connection, workspace_id, user_id, source_message_id="del-1")

    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        connection.execute(sa.text("DELETE FROM thoughts WHERE id = :id"), {"id": thought_id})


def test_restore_session_may_mutate_thoughts(connection: sa.Connection) -> None:
    """The one documented escape: a restore-only administrative session."""
    workspace_id, user_id = _seed_workspace_and_user(connection)
    thought_id = _insert_thought(connection, workspace_id, user_id, source_message_id="restore-1")

    connection.execute(sa.text("SET LOCAL tc.allow_thought_restore = 'on'"))
    connection.execute(
        sa.text("UPDATE thoughts SET body = 'restored' WHERE id = :id"),
        {"id": thought_id},
    )

    body = connection.execute(
        sa.text("SELECT body FROM thoughts WHERE id = :id"), {"id": thought_id}
    ).scalar_one()
    assert body == "restored"


def test_correction_is_an_append_not_an_edit(connection: sa.Connection) -> None:
    """Corrections are new rows linked to the original (docs/DESIGN.md 3.2)."""
    workspace_id, user_id = _seed_workspace_and_user(connection)
    original_id = _insert_thought(
        connection, workspace_id, user_id, source_message_id="orig-1", body="teh meeting is at 4"
    )

    corrected_id = connection.execute(
        sa.text("""
            INSERT INTO thoughts (
              workspace_id, author_user_id, source, source_message_id, body,
              client_created_at, client_timezone, client_local_date, client_local_time,
              correction_of
            ) VALUES (
              :workspace_id, :user_id, 'discord', 'corr-1', 'the meeting is at 4',
              '2026-08-30T13:05:00+00:00', 'Asia/Bangkok', '2026-08-30', '20:05',
              :original_id
            ) RETURNING id
        """),
        {"workspace_id": workspace_id, "user_id": user_id, "original_id": original_id},
    ).scalar_one()

    rows = connection.execute(
        sa.text("SELECT id, body FROM thoughts WHERE id IN (:a, :b) ORDER BY id"),
        {"a": original_id, "b": corrected_id},
    ).all()
    assert len(rows) == 2, "the original must survive its own correction"
    assert rows[0][1] == "teh meeting is at 4"


# ---------------------------------------------------------------------------
# Least-privilege grants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("privilege", ["UPDATE", "DELETE"])
def test_application_role_cannot_mutate_thoughts(connection: sa.Connection, privilege: str) -> None:
    granted = connection.execute(
        sa.text("SELECT has_table_privilege('tc_app', 'thoughts', :privilege)"),
        {"privilege": privilege},
    ).scalar_one()
    assert granted is False, f"tc_app must not hold {privilege} on thoughts"


@pytest.mark.parametrize("privilege", ["SELECT", "INSERT"])
def test_application_role_can_capture(connection: sa.Connection, privilege: str) -> None:
    granted = connection.execute(
        sa.text("SELECT has_table_privilege('tc_app', 'thoughts', :privilege)"),
        {"privilege": privilege},
    ).scalar_one()
    assert granted is True


def test_application_role_cannot_create_objects(connection: sa.Connection) -> None:
    granted: Any = connection.execute(
        sa.text("SELECT has_schema_privilege('tc_app', 'public', 'CREATE')")
    ).scalar_one()
    assert granted is False, "tc_app must not be able to create objects in public"

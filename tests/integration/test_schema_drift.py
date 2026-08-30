"""The Core table definitions must match the migrated schema.

``tc_infrastructure.db.tables`` describes the same tables the migrations create.
Two descriptions of one schema drift silently: a column added by a migration and
forgotten in the table definition produces a query that compiles and returns the
wrong shape. This compares them directly.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from tc_infrastructure.db import tables

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("table", sorted(tables.metadata.tables), ids=lambda name: str(name))
def test_declared_columns_match_the_database(connection: sa.Connection, table: str) -> None:
    declared = {column.name for column in tables.metadata.tables[table].columns}

    actual = {
        row[0]
        for row in connection.execute(
            sa.text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :table
            """),
            {"table": table},
        )
    }

    assert actual, f"table {table!r} does not exist in the migrated database"

    missing_from_code = actual - declared
    missing_from_database = declared - actual

    assert not missing_from_code, (
        f"{table}: columns exist in the database but not in tables.py: {sorted(missing_from_code)}"
    )
    assert not missing_from_database, (
        f"{table}: columns declared in tables.py do not exist in the database: "
        f"{sorted(missing_from_database)}"
    )


def test_every_migrated_table_is_declared(connection: sa.Connection) -> None:
    """Catches a new migration whose table nobody added to tables.py."""
    actual = {
        row[0]
        for row in connection.execute(
            sa.text("""
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public' AND tablename <> 'alembic_version'
            """)
        )
    }

    undeclared = actual - set(tables.metadata.tables)
    assert not undeclared, f"migrated tables missing from tables.py: {sorted(undeclared)}"

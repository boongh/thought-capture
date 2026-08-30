"""Reduce the application role to exactly what capture needs.

Migration 0001 granted the application role INSERT on ``users``, ``workspaces``,
and ``external_identities`` on the assumption that it would seed them. It does
not: seeding is a bootstrap step that runs once with the migration role, in the
same way migrations do (docs/DESIGN.md 13). Creating identities and workspaces
is administrative, and a capture service that can mint them holds more privilege
than its job requires (docs/DESIGN.md 12.2).

After this migration the application role can read the identity tables and
append to the capture tables. Nothing else.

This is expressed as a new migration rather than an edit to 0001, because 0001
has already been applied elsewhere and migration history is append-only once
shared.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tc_app"

# Identity and workspace records are created by the bootstrap step, then only
# read by the running services.
READ_ONLY_TABLES = ("users", "workspaces", "workspace_memberships", "external_identities")


def upgrade() -> None:
    for table in READ_ONLY_TABLES:
        op.execute(f"REVOKE INSERT, UPDATE, DELETE ON {table} FROM {APP_ROLE}")
        op.execute(f"GRANT SELECT ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    # Restores the grants as 0001 left them.
    for table in ("users", "workspaces", "external_identities"):
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON workspace_memberships TO {APP_ROLE}")

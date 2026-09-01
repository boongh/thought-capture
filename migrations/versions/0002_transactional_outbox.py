"""Transactional outbox for side effects that must not diverge from state.

Implements docs/DESIGN.md 6.5. A database commit and a Discord acknowledgement
cannot be made atomic with each other, so the *intent* to acknowledge is written
in the same transaction as the thought it describes, and delivery happens
afterwards with retries.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tc_app"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE outbox_events (
          id uuid PRIMARY KEY,
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          event_type text NOT NULL,
          aggregate_id text NOT NULL,
          payload jsonb NOT NULL,
          attempts integer NOT NULL DEFAULT 0,
          max_attempts integer NOT NULL DEFAULT 10,
          available_at timestamptz NOT NULL DEFAULT now(),
          leased_until timestamptz,
          lease_owner text,
          delivered_at timestamptz,
          last_error text,
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK (attempts >= 0),
          CHECK (max_attempts > 0)
        )
    """)

    # Partial index over undelivered work only. The table is append-mostly and
    # will accumulate delivered rows; the consumer must not pay for them.
    # Column order matches the claim query's predicates so the index can drive
    # `FOR UPDATE SKIP LOCKED` directly.
    op.execute("""
        CREATE INDEX outbox_events_claimable_idx
          ON outbox_events (workspace_id, available_at, id)
          WHERE delivered_at IS NULL
    """)

    # Diagnostics: "what happened to the acknowledgement for thought 41?"
    op.execute("""
        CREATE INDEX outbox_events_aggregate_idx
          ON outbox_events (workspace_id, event_type, aggregate_id)
    """)

    # The consumer marks attempts, leases, and delivery, so UPDATE is required
    # here even though the capture tables are insert-only. DELETE is withheld:
    # pruning delivered events is an administrative task, not something the
    # application may do while running.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON outbox_events TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON outbox_events FROM {APP_ROLE}")
    op.execute("DROP TABLE IF EXISTS outbox_events")

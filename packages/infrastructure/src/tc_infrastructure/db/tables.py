"""SQLAlchemy Core table definitions for the first-party schema.

These are for *querying*, not for creating. The schema is owned by the
hand-written Alembic migrations, which express generated columns, triggers, and
role grants that Core cannot. Keeping a second description of the same tables
risks drift, so ``tests/integration/test_schema_drift.py`` compares every column
here against the live database.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

metadata = sa.MetaData()

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

workspaces = sa.Table(
    "workspaces",
    metadata,
    sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("mode", sa.Text, nullable=False),
    sa.Column("timezone", sa.Text, nullable=False),
    sa.Column("digest_local_time", sa.Time, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

workspace_memberships = sa.Table(
    "workspace_memberships",
    metadata,
    sa.Column("workspace_id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("user_id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("role", sa.Text, nullable=False),
)

external_identities = sa.Table(
    "external_identities",
    metadata,
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("provider", sa.Text, primary_key=True),
    sa.Column("external_user_id", sa.Text, primary_key=True),
)

thoughts = sa.Table(
    "thoughts",
    metadata,
    # GENERATED ALWAYS AS IDENTITY. Never supply this on insert.
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("author_user_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("source_message_id", sa.Text, nullable=False),
    sa.Column("source_channel_id", sa.Text),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("client_created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("client_timezone", sa.Text, nullable=False),
    sa.Column("client_local_date", sa.Date, nullable=False),
    sa.Column("client_local_time", sa.Time, nullable=False),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("content_language", sa.Text, nullable=False),
    sa.Column("correction_of", sa.BigInteger),
    # Generated STORED; read-only from the application's point of view.
    sa.Column("body_tsv", pg.TSVECTOR),
)

blobs = sa.Table(
    "blobs",
    metadata,
    sa.Column("sha256", sa.CHAR(64), primary_key=True),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("media_type", sa.Text, nullable=False),
    sa.Column("storage_key", sa.Text, nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

thought_attachments = sa.Table(
    "thought_attachments",
    metadata,
    sa.Column("thought_id", sa.BigInteger, primary_key=True),
    sa.Column("blob_sha256", sa.CHAR(64), primary_key=True),
    sa.Column("source_filename", sa.Text, primary_key=True),
    sa.Column("source_url_expires_at", sa.DateTime(timezone=True)),
    sa.Column("extracted_status", sa.Text, nullable=False),
)

outbox_events = sa.Table(
    "outbox_events",
    metadata,
    sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("aggregate_id", sa.Text, nullable=False),
    sa.Column("payload", pg.JSONB, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
    sa.Column("max_attempts", sa.Integer, nullable=False),
    sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("leased_until", sa.DateTime(timezone=True)),
    sa.Column("lease_owner", sa.Text),
    sa.Column("delivered_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

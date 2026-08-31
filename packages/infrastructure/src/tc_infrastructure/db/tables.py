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
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("thought_id", sa.BigInteger, primary_key=True),
    sa.Column("blob_sha256", sa.CHAR(64), primary_key=True),
    sa.Column("source_filename", sa.Text, primary_key=True),
    sa.Column("source_url_expires_at", sa.DateTime(timezone=True)),
    sa.Column("extracted_status", sa.Text, nullable=False),
)

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("window_start", sa.DateTime(timezone=True)),
    sa.Column("window_end", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("prompt_version", sa.Text),
    sa.Column("model_provider", sa.Text),
    sa.Column("model_id", sa.Text),
    sa.Column("embedding_model_id", sa.Text),
    sa.Column("input_tokens", sa.BigInteger),
    sa.Column("output_tokens", sa.BigInteger),
    sa.Column("estimated_cost_usd", sa.Numeric(12, 6)),
    sa.Column("context_recall", sa.Numeric(4, 3)),
    sa.Column("context_degraded", sa.Boolean, nullable=False),
    sa.Column("replay_of_run_id", pg.UUID(as_uuid=True)),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("error_code", sa.Text),
    sa.Column("error_detail", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

documents = sa.Table(
    "documents",
    metadata,
    sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("stable_key", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("current_revision_id", pg.UUID(as_uuid=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

document_revisions = sa.Table(
    "document_revisions",
    metadata,
    sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("parent_revision_id", pg.UUID(as_uuid=True)),
    sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column("body_markdown", sa.Text, nullable=False),
    sa.Column("body_sha256", sa.CHAR(64), nullable=False),
    sa.Column("change_summary", sa.Text, nullable=False),
    sa.Column("change_kind", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

revision_sources = sa.Table(
    "revision_sources",
    metadata,
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("revision_id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("thought_id", sa.BigInteger, primary_key=True),
    sa.Column("support_type", sa.Text, nullable=False),
)

entities = sa.Table(
    "entities",
    metadata,
    sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("entity_type", sa.Text, nullable=False),
    sa.Column("canonical_name", sa.Text, nullable=False),
    sa.Column("normalized_name", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

entity_aliases = sa.Table(
    "entity_aliases",
    metadata,
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("entity_id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("alias", sa.Text, nullable=False),
    sa.Column("normalized_alias", sa.Text, primary_key=True),
)

entity_mentions = sa.Table(
    "entity_mentions",
    metadata,
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("entity_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("thought_id", sa.BigInteger),
    sa.Column("revision_id", pg.UUID(as_uuid=True)),
    sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("surface_form", sa.Text, nullable=False),
    sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
)

capture_windows = sa.Table(
    "capture_windows",
    metadata,
    sa.Column("workspace_id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("window_end", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("organized_by_run_id", pg.UUID(as_uuid=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# Append-only journal of every model call (ADR-0008). Contains raw personal
# content, because prompts embed thought bodies.
llm_calls = sa.Table(
    "llm_calls",
    metadata,
    sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("step", sa.Text, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("prompt_version", sa.Text, nullable=False),
    sa.Column("schema_version", sa.Text, nullable=False),
    sa.Column("model_requested", sa.Text, nullable=False),
    sa.Column("model_served", sa.Text),
    sa.Column("provider", sa.Text),
    sa.Column("generation_id", sa.Text),
    sa.Column("request_messages", pg.JSONB, nullable=False),
    sa.Column("request_params", pg.JSONB, nullable=False),
    sa.Column("response_raw", pg.JSONB),
    sa.Column("input_tokens", sa.BigInteger),
    sa.Column("output_tokens", sa.BigInteger),
    sa.Column("estimated_cost_usd", sa.Numeric(12, 6)),
    sa.Column("latency_ms", sa.Integer),
    sa.Column("error_code", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# Context assembly observability (docs/DESIGN.md 7.3.5).
run_context_selections = sa.Table(
    "run_context_selections",
    metadata,
    sa.Column("workspace_id", pg.UUID(as_uuid=True), nullable=False),
    sa.Column("run_id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("document_id", pg.UUID(as_uuid=True), primary_key=True),
    sa.Column("signals", pg.ARRAY(sa.Text), nullable=False),
    sa.Column("inclusion", sa.Text, nullable=False),
    sa.Column("body_tokens", sa.Integer, nullable=False),
    sa.Column("referenced_in_output", sa.Boolean, nullable=False),
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

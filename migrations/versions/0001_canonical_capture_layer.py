"""Canonical capture layer: identity, workspace, append-only thoughts, blobs.

Implements docs/DESIGN.md 6.1 and 6.2, plus the append-only enforcement described
there ("Database roles deny UPDATE and DELETE on `thoughts`; an additional
trigger rejects mutation except in a restore-only administrative session").

Revision ID: 0001
Revises:
Create Date: 2026-08-30

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "tc_app"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ------------------------------------------------------------------
    # Identity and workspace (docs/DESIGN.md 6.1)
    #
    # Every domain table carries workspace_id even though Release 1 seeds a
    # single workspace. This costs nothing now and avoids re-keying the database
    # when isolated personal workspaces or shared workspaces arrive.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE users (
          id uuid PRIMARY KEY,
          display_name text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE workspaces (
          id uuid PRIMARY KEY,
          name text NOT NULL,
          mode text NOT NULL CHECK (mode IN ('personal','shared')),
          timezone text NOT NULL,
          digest_local_time time NOT NULL DEFAULT '20:00',
          created_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE workspace_memberships (
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          user_id uuid NOT NULL REFERENCES users(id),
          role text NOT NULL CHECK (role IN ('owner','editor','viewer')),
          PRIMARY KEY (workspace_id, user_id)
        )
    """)

    op.execute("""
        CREATE TABLE external_identities (
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          user_id uuid NOT NULL REFERENCES users(id),
          provider text NOT NULL,
          external_user_id text NOT NULL,
          PRIMARY KEY (provider, external_user_id),
          UNIQUE (workspace_id, user_id, provider)
        )
    """)

    # ------------------------------------------------------------------
    # Append-only capture layer (docs/DESIGN.md 6.2)
    #
    # NOTE: `STORED` is mandatory and explicit. PostgreSQL 18 changed the
    # default kind of a generated column to VIRTUAL; omitting the keyword would
    # silently produce a column computed on read, which cannot be indexed and
    # would break the full-text index below.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE thoughts (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          workspace_id uuid NOT NULL REFERENCES workspaces(id),
          author_user_id uuid NOT NULL REFERENCES users(id),
          source text NOT NULL CHECK (source IN ('discord','api','import')),
          source_message_id text NOT NULL,
          source_channel_id text,
          body text NOT NULL DEFAULT '',
          client_created_at timestamptz NOT NULL,
          client_timezone text NOT NULL,
          client_local_date date NOT NULL,
          client_local_time time NOT NULL,
          received_at timestamptz NOT NULL DEFAULT now(),
          content_language text NOT NULL DEFAULT 'en',
          correction_of bigint REFERENCES thoughts(id),
          body_tsv tsvector GENERATED ALWAYS AS
            (to_tsvector('english', coalesce(body,''))) STORED,
          UNIQUE (source, source_message_id)
        )
    """)

    op.execute("""
        CREATE INDEX thoughts_workspace_time_idx
          ON thoughts (workspace_id, client_created_at DESC)
    """)
    op.execute("CREATE INDEX thoughts_tsv_idx ON thoughts USING gin (body_tsv)")
    op.execute("CREATE INDEX thoughts_trgm_idx ON thoughts USING gin (body gin_trgm_ops)")

    op.execute("""
        CREATE TABLE blobs (
          sha256 char(64) PRIMARY KEY,
          size_bytes bigint NOT NULL,
          media_type text NOT NULL,
          storage_key text NOT NULL UNIQUE,
          created_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE thought_attachments (
          thought_id bigint NOT NULL REFERENCES thoughts(id),
          blob_sha256 char(64) NOT NULL REFERENCES blobs(sha256),
          source_filename text NOT NULL,
          source_url_expires_at timestamptz,
          extracted_status text NOT NULL DEFAULT 'not_supported',
          PRIMARY KEY (thought_id, blob_sha256, source_filename)
        )
    """)

    # ------------------------------------------------------------------
    # Append-only enforcement, layer 1: trigger.
    #
    # Defence in depth alongside the role grants below. The trigger stops a
    # mutation even when it is attempted by a superuser or the migration role,
    # which the grants cannot. The single documented escape is a restore-only
    # administrative session that sets `tc.allow_thought_restore` to 'on'.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE FUNCTION reject_thought_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF coalesce(current_setting('tc.allow_thought_restore', true), 'off') = 'on' THEN
            RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
          END IF;
          RAISE EXCEPTION
            'thoughts is append-only: % rejected. Corrections are new rows linked by correction_of.',
            TG_OP
            USING ERRCODE = 'restrict_violation';
        END;
        $$
    """)

    op.execute("""
        CREATE TRIGGER thoughts_append_only
          BEFORE UPDATE OR DELETE ON thoughts
          FOR EACH ROW EXECUTE FUNCTION reject_thought_mutation()
    """)

    # ------------------------------------------------------------------
    # Append-only enforcement, layer 2: least-privilege application role.
    #
    # The role is created here only if an operator has not already provisioned
    # it (deploy/compose/initdb/01-roles.sh does so with a password). Creating it
    # without a password is safe: NOLOGIN means it cannot be used to connect.
    # ------------------------------------------------------------------
    op.execute(f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            CREATE ROLE {APP_ROLE} NOLOGIN;
          END IF;
        END
        $$
    """)

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")

    # Reference data the application reads and seeds.
    for table in ("users", "workspaces", "external_identities"):
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON workspace_memberships TO {APP_ROLE}")

    # Canonical capture data: insert and read only. No UPDATE, no DELETE.
    for table in ("thoughts", "blobs", "thought_attachments"):
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    op.execute("DROP TRIGGER IF EXISTS thoughts_append_only ON thoughts")
    op.execute("DROP FUNCTION IF EXISTS reject_thought_mutation()")
    op.execute("DROP TABLE IF EXISTS thought_attachments")
    op.execute("DROP TABLE IF EXISTS blobs")
    op.execute("DROP TABLE IF EXISTS thoughts")
    op.execute("DROP TABLE IF EXISTS external_identities")
    op.execute("DROP TABLE IF EXISTS workspace_memberships")
    op.execute("DROP TABLE IF EXISTS workspaces")
    op.execute("DROP TABLE IF EXISTS users")
    # pg_trgm is left installed: it is cheap, and other schemas may rely on it.

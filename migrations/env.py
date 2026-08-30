"""Alembic environment for the first-party schema.

Migrations use a synchronous engine and the dedicated migration role. The
application role has no schema privileges and is denied UPDATE/DELETE on
`thoughts`, which is what makes the append-only invariant a database guarantee
rather than an application convention (docs/DESIGN.md 6.2).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from tc_infrastructure.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Migrations are hand-written against the DDL in docs/DESIGN.md 6, which uses
# generated columns, triggers, and role grants that autogenerate does not model
# well. There is deliberately no target_metadata.
target_metadata = None


def _database_url() -> str:
    """Resolve the migration URL, preferring an explicit -x url= override."""
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    return get_settings().migration_database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review or manual application."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database in a single transaction."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

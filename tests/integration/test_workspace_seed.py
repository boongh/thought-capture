"""Workspace bootstrap.

``ensure_workspace`` runs on process start, so it must be safe to call
repeatedly. Release 1 seeds one owner, one workspace, one membership
(docs/DESIGN.md 6).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_infrastructure.config import Settings
from tc_infrastructure.db.seed import (
    WorkspaceNotConfiguredError,
    ensure_workspace,
)
from tc_infrastructure.db.tables import (
    external_identities,
    workspace_memberships,
    workspaces,
)

pytestmark = pytest.mark.integration


def settings_for(discord_owner_user_id: str | None, timezone: str = "Asia/Bangkok") -> Settings:
    values: dict[str, object] = {
        "workspace_name": "synthetic workspace",
        "workspace_timezone": timezone,
    }
    if discord_owner_user_id is not None:
        values["discord_owner_user_id"] = discord_owner_user_id
    return Settings(_env_file=None, **values)


def a_discord_id() -> str:
    """A plausible-looking synthetic snowflake, unique per test."""
    return str(uuid.uuid4().int)[:18]


async def test_seeding_creates_owner_workspace_and_identity(
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    discord_id = a_discord_id()

    async with admin_session_factory() as session, session.begin():
        seeded = await ensure_workspace(session, settings_for(discord_id))

    assert seeded.created is True

    async with admin_session_factory() as session:
        workspace = (
            await session.execute(
                sa.select(workspaces).where(workspaces.c.id == seeded.workspace_id)
            )
        ).one()
        membership = (
            await session.execute(
                sa.select(workspace_memberships).where(
                    workspace_memberships.c.workspace_id == seeded.workspace_id
                )
            )
        ).one()
        identity = (
            await session.execute(
                sa.select(external_identities).where(
                    external_identities.c.external_user_id == discord_id
                )
            )
        ).one()

    assert workspace.mode == "personal"
    assert workspace.timezone == "Asia/Bangkok"
    assert membership.role == "owner"
    assert membership.user_id == seeded.user_id
    assert identity.provider == "discord"
    assert identity.workspace_id == seeded.workspace_id


async def test_seeding_twice_is_a_no_op(
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """It runs on every process start, so re-running must not duplicate anything."""
    discord_id = a_discord_id()
    configuration = settings_for(discord_id)

    async with admin_session_factory() as session, session.begin():
        first = await ensure_workspace(session, configuration)
    async with admin_session_factory() as session, session.begin():
        second = await ensure_workspace(session, configuration)

    assert second.workspace_id == first.workspace_id
    assert second.user_id == first.user_id
    assert first.created is True
    assert second.created is False

    async with admin_session_factory() as session:
        workspace_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(workspaces)
            .where(workspaces.c.id == first.workspace_id)
        )
        identity_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(external_identities)
            .where(external_identities.c.external_user_id == discord_id)
        )

    assert workspace_count == 1
    assert identity_count == 1


async def test_the_configured_timezone_is_persisted_on_the_workspace(
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second workspace must be able to differ without a code change."""
    async with admin_session_factory() as session, session.begin():
        seeded = await ensure_workspace(
            session, settings_for(a_discord_id(), timezone="Europe/London")
        )

    async with admin_session_factory() as session:
        timezone = await session.scalar(
            sa.select(workspaces.c.timezone).where(workspaces.c.id == seeded.workspace_id)
        )
    assert timezone == "Europe/London"


async def test_seeding_without_an_owner_id_fails_loudly(
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Better a clear startup error than a workspace nobody can capture into."""
    async with admin_session_factory() as session:
        with pytest.raises(WorkspaceNotConfiguredError, match="TC_DISCORD_OWNER_USER_ID"):
            await ensure_workspace(session, settings_for(None))

"""Idempotent workspace bootstrap.

Release 1 seeds one owner, one workspace, and one membership (docs/DESIGN.md 6).
This runs as an explicit step rather than inside a migration, because it depends
on runtime configuration - the owner's Discord ID and the workspace timezone -
which does not belong in schema history.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tc_domain.capture import UserId, WorkspaceId
from tc_infrastructure.config import Settings
from tc_infrastructure.db.tables import (
    external_identities,
    users,
    workspace_memberships,
    workspaces,
)

logger = logging.getLogger(__name__)

DISCORD_PROVIDER = "discord"


@dataclass(frozen=True, slots=True)
class SeededWorkspace:
    workspace_id: WorkspaceId
    user_id: UserId
    created: bool


class WorkspaceNotConfiguredError(RuntimeError):
    """The owner's Discord ID is required before a workspace can be seeded."""


async def ensure_workspace(session: AsyncSession, settings: Settings) -> SeededWorkspace:
    """Create the owner, workspace, membership, and Discord identity if absent.

    Idempotent by lookup on ``external_identities``, whose primary key is
    ``(provider, external_user_id)``. Re-running is a no-op, so this is safe to
    call on every process start.
    """
    if settings.discord_owner_user_id is None:
        raise WorkspaceNotConfiguredError(
            "TC_DISCORD_OWNER_USER_ID must be set before the workspace can be seeded"
        )

    external_user_id = str(settings.discord_owner_user_id)

    existing = (
        await session.execute(
            sa.select(
                external_identities.c.workspace_id,
                external_identities.c.user_id,
            ).where(
                external_identities.c.provider == DISCORD_PROVIDER,
                external_identities.c.external_user_id == external_user_id,
            )
        )
    ).first()

    if existing is not None:
        return SeededWorkspace(
            workspace_id=WorkspaceId(existing.workspace_id),
            user_id=UserId(existing.user_id),
            created=False,
        )

    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()

    await session.execute(sa.insert(users).values(id=user_id, display_name=settings.workspace_name))
    await session.execute(
        sa.insert(workspaces).values(
            id=workspace_id,
            name=settings.workspace_name,
            mode="personal",
            timezone=settings.workspace_timezone,
            digest_local_time=settings.digest_local_time,
        )
    )
    await session.execute(
        sa.insert(workspace_memberships).values(
            workspace_id=workspace_id, user_id=user_id, role="owner"
        )
    )
    await session.execute(
        sa.insert(external_identities).values(
            workspace_id=workspace_id,
            user_id=user_id,
            provider=DISCORD_PROVIDER,
            external_user_id=external_user_id,
        )
    )

    # Identifiers only: the Discord user ID is a personal identifier and stays
    # out of logs (docs/DESIGN.md 12.2).
    logger.info(
        "workspace.seeded",
        extra={"workspace_id": str(workspace_id), "timezone": settings.workspace_timezone},
    )

    return SeededWorkspace(
        workspace_id=WorkspaceId(workspace_id), user_id=UserId(user_id), created=True
    )

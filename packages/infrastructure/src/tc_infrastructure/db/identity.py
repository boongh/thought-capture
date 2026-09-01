"""Resolving an external account to a workspace and user.

Read-only: the running services may look identities up but not create them.
Creating them is a bootstrap task (migration 0003).
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import UserId, WorkspaceId
from tc_infrastructure.db.tables import external_identities


@dataclass(frozen=True, slots=True)
class CaptureIdentity:
    workspace_id: WorkspaceId
    user_id: UserId


class IdentityNotFoundError(RuntimeError):
    """No workspace is mapped to this external account.

    Deliberately fatal at startup: a bot that cannot resolve its owner would
    silently ignore every message, which looks identical to a broken allowlist.
    """


async def resolve_identity(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: str,
    external_user_id: str,
) -> CaptureIdentity:
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.select(
                    external_identities.c.workspace_id,
                    external_identities.c.user_id,
                ).where(
                    external_identities.c.provider == provider,
                    external_identities.c.external_user_id == external_user_id,
                )
            )
        ).first()

    if row is None:
        # The external ID is a personal identifier and stays out of the message.
        raise IdentityNotFoundError(
            f"no workspace is mapped to the configured {provider} account; "
            "run the bootstrap step before starting the services"
        )

    return CaptureIdentity(workspace_id=WorkspaceId(row.workspace_id), user_id=UserId(row.user_id))

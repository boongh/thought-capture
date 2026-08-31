"""Dependency wiring and authentication.

Everything the routes need is assembled once at startup and stored on the app
state, so a request does not build engines or clients.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_api.problems import unauthorized
from tc_application.capture import CaptureThought
from tc_domain.capture import UserId, WorkspaceId
from tc_infrastructure.config import Settings
from tc_infrastructure.db.outbox import PostgresOutbox
from tc_infrastructure.db.thought_reader import PostgresThoughtReader


@dataclass(frozen=True, slots=True)
class ApiContext:
    """Everything a request handler may need, built once."""

    settings: Settings
    capture: CaptureThought
    reader: PostgresThoughtReader
    outbox: PostgresOutbox
    session_factory: async_sessionmaker[AsyncSession]
    workspace_id: WorkspaceId
    user_id: UserId


def get_context(request: Request) -> ApiContext:
    context: ApiContext = request.app.state.context
    return context


def require_bearer_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Local bearer authentication with a constant-time comparison.

    A plain `==` on a secret leaks its length and prefix through timing;
    docs/DESIGN.md 12.2 requires constant-time comparison.
    """
    context: ApiContext = request.app.state.context
    expected = context.settings.api_bearer_token.get_secret_value()

    if not expected:
        # Refusing is safer than allowing: an unset token must not mean "open".
        raise unauthorized("the API bearer token is not configured on the server")

    if authorization is None:
        raise unauthorized("an Authorization header is required")

    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise unauthorized("expected an 'Authorization: Bearer <token>' header")

    if not hmac.compare_digest(presented, expected):
        raise unauthorized("the presented bearer token is not valid")


Context = Annotated[ApiContext, Depends(get_context)]
Authenticated = Depends(require_bearer_token)

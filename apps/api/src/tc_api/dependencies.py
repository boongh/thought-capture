"""Dependency wiring and authentication.

Everything the routes need is assembled once at startup and stored on the app
state, so a request does not build engines or clients.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_api.problems import unauthorized
from tc_application.capture import CaptureThought
from tc_domain.capture import UserId, WorkspaceId
from tc_infrastructure.config import Settings
from tc_infrastructure.db.document_reader import PostgresDocumentReader
from tc_infrastructure.db.entity_reader import PostgresEntityReader
from tc_infrastructure.db.outbox import PostgresOutbox
from tc_infrastructure.db.thought_reader import PostgresThoughtReader


@dataclass(frozen=True, slots=True)
class ApiContext:
    """Everything a request handler may need, built once."""

    settings: Settings
    capture: CaptureThought
    reader: PostgresThoughtReader
    documents: PostgresDocumentReader
    entities: PostgresEntityReader
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


def require_debug_access(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Like ``require_bearer_token``, but also accepts ``?token=`` for a plain browser tab.

    Scoped to ``/debug/*`` only (docs/adr/0009): the JSON ``/v1`` surface still
    requires the ``Authorization`` header. The loopback-only bind
    (docs/DESIGN.md 12.2) is what makes a query-param token an acceptable
    relaxation here - it never leaves the operator's own machine.
    """
    context: ApiContext = request.app.state.context
    expected = context.settings.api_bearer_token.get_secret_value()

    if not expected:
        raise unauthorized("the API bearer token is not configured on the server")

    presented = token
    if presented is None and authorization is not None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            presented = value

    if not presented or not hmac.compare_digest(presented, expected):
        raise unauthorized("a valid token is required (Authorization header or ?token=)")


Context = Annotated[ApiContext, Depends(get_context)]
Authenticated = Depends(require_bearer_token)
DebugAuthenticated = Depends(require_debug_access)

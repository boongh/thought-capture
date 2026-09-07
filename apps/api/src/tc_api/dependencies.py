"""Dependency wiring and authentication.

Everything the routes need is assembled once at startup and stored on the app
state, so a request does not build engines or clients.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_api.problems import ProblemError, unauthorized
from tc_application.ask import AskQuestion
from tc_application.capture import CaptureThought
from tc_application.khoj_sync import SyncKhojIndex
from tc_application.search import Search
from tc_domain.capture import UserId, WorkspaceId
from tc_infrastructure.config import Settings
from tc_infrastructure.db.document_reader import PostgresDocumentReader
from tc_infrastructure.db.entity_reader import PostgresEntityReader
from tc_infrastructure.db.llm_call_reader import PostgresLlmCallReader
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
    search: Search
    ask: AskQuestion
    khoj_sync: SyncKhojIndex
    llm_calls: PostgresLlmCallReader
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


_DEBUG_WWW_AUTHENTICATE = 'Basic realm="thought-capture-ai debug", charset="UTF-8"'


def require_debug_access(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """HTTP Basic Auth for ``/debug/*`` only (docs/adr/0009).

    Not a ``?token=`` query parameter, and not the plain ``Authorization:
    Bearer`` scheme ``require_bearer_token`` uses for ``/v1``: a browser tab
    navigating directly to a page cannot set a custom header, but *can*
    supply Basic Auth credentials via its own native prompt, which it then
    caches per-origin for the session - no JS, no link-carried secret, no
    cookie machinery needed for three fixed pages. Critically, unlike a
    query parameter, Basic Auth credentials travel in a header, which
    ``uvicorn``'s access log (docs/DESIGN.md 14.2: "Never include ... API
    keys ... in logs") does not record - the port's loopback-only bind
    (docs/DESIGN.md 12.2) prevents network exposure but never prevented a
    query-string token from being written to a local log file on every
    request, which is why that earlier design was replaced.
    """
    context: ApiContext = request.app.state.context
    expected = context.settings.api_bearer_token.get_secret_value()

    if not expected:
        raise unauthorized("the API bearer token is not configured on the server")

    presented = _basic_auth_credential(authorization)
    if not presented or not hmac.compare_digest(presented, expected):
        raise ProblemError(
            status=401,
            title="Unauthorized",
            code="unauthorized",
            detail="valid Basic Auth credentials are required",
            headers={"WWW-Authenticate": _DEBUG_WWW_AUTHENTICATE},
        )


def _basic_auth_credential(authorization: str | None) -> str | None:
    """The password half of a ``Basic`` credential, or ``None`` if absent/malformed.

    The username is ignored entirely - like ``Bearer <token>``, only the
    secret itself is checked, so any username the browser's prompt is given
    works.
    """
    if authorization is None:
        return None
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except binascii.Error, UnicodeDecodeError:
        return None
    _, _, password = decoded.partition(":")
    return password or None


Context = Annotated[ApiContext, Depends(get_context)]
Authenticated = Depends(require_bearer_token)
DebugAuthenticated = Depends(require_debug_access)

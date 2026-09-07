"""FastAPI application assembly.

Transport and wiring only. Business rules live in ``tc_application`` and
``tc_domain``; this module knows how to speak HTTP and nothing else
(docs/DESIGN.md 5.3).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tc_api.dependencies import ApiContext
from tc_api.problems import ProblemError, problem_response
from tc_api.routers import admin, ask, debug, documents, entities, health, search, thoughts
from tc_application.ask import AskQuestion
from tc_application.capture import CaptureThought
from tc_application.khoj_sync import SyncKhojIndex
from tc_application.search import Search
from tc_domain.policy import AttachmentPolicy
from tc_infrastructure.config import Settings, get_settings
from tc_infrastructure.db.document_reader import PostgresDocumentReader
from tc_infrastructure.db.engine import create_engine, create_session_factory
from tc_infrastructure.db.entity_reader import PostgresEntityReader
from tc_infrastructure.db.identity import resolve_identity
from tc_infrastructure.db.llm_call_reader import PostgresLlmCallReader
from tc_infrastructure.db.outbox import PostgresOutbox
from tc_infrastructure.db.search_reader import PostgresExactSearch
from tc_infrastructure.db.thought_reader import PostgresThoughtReader
from tc_infrastructure.db.thought_repository import PostgresThoughtRepository
from tc_infrastructure.khoj.client import HttpKhojClient
from tc_infrastructure.storage.attachment_archive import HttpAttachmentArchive
from tc_infrastructure.storage.blob_store import FilesystemBlobStore

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
DISCORD_PROVIDER = "discord"


async def build_context(settings: Settings, http: httpx.AsyncClient) -> ApiContext:
    engine = create_engine(settings)
    sessions = create_session_factory(engine)

    identity = await resolve_identity(
        sessions,
        provider=DISCORD_PROVIDER,
        external_user_id=str(settings.discord_owner_user_id),
    )

    capture = CaptureThought(
        PostgresThoughtRepository(sessions),
        HttpAttachmentArchive(
            FilesystemBlobStore(settings.attachment_root),
            http,
            max_bytes=settings.attachment_max_bytes,
        ),
        AttachmentPolicy(max_bytes=settings.attachment_max_bytes),
    )

    document_reader = PostgresDocumentReader(sessions)
    exact_search = PostgresExactSearch(sessions)
    # Always constructed - this project's own bearer token remains the only
    # credential a client needs (docs/adr/0003's zero-Khoj-credential
    # deployment) - but every call it makes is opt-in per use case: `search`
    # degrades explicitly if the `ai` profile is not running
    # (`KhojUnavailableError`), and `ask` additionally requires
    # `settings.ask_enabled` (docs/adr/0010).
    khoj = HttpKhojClient(http, settings.khoj_base_url)

    return ApiContext(
        settings=settings,
        capture=capture,
        reader=PostgresThoughtReader(sessions),
        documents=document_reader,
        entities=PostgresEntityReader(sessions),
        search=Search(exact_search, khoj),
        ask=AskQuestion(khoj, exact_search, enabled=settings.ask_enabled),
        khoj_sync=SyncKhojIndex(document_reader, khoj),
        llm_calls=PostgresLlmCallReader(sessions),
        outbox=PostgresOutbox(sessions, lease_owner="api"),
        session_factory=sessions,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    http = httpx.AsyncClient()
    try:
        app.state.context = await build_context(settings, http)
        yield
    finally:
        await http.aclose()


def create_app(*, lifespan_handler: object | None = None) -> FastAPI:
    app = FastAPI(
        title="Thought Capture AI",
        version="0.1.0",
        summary="Append-only personal memory capture and retrieval.",
        lifespan=lifespan_handler or lifespan,  # type: ignore[arg-type]
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Correlate a report of a failure with the log line that produced it."""
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(ProblemError)
    async def handle_problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Render FastAPI's validation errors as problem documents too.

        Otherwise clients would have to parse two different error shapes.
        """
        return problem_response(
            request,
            ProblemError(
                status=422,
                title="Unprocessable Content",
                code="validation-failed",
                detail="the request did not match the expected schema",
                extra={"errors": exc.errors()},
            ),
        )

    app.include_router(health.router)
    app.include_router(thoughts.router)
    app.include_router(documents.router)
    app.include_router(entities.router)
    app.include_router(search.router)
    app.include_router(ask.router)
    app.include_router(admin.router)
    app.include_router(debug.router)
    return app

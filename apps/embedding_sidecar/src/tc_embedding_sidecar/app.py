"""FastAPI application: `POST /embed`, `GET /health`.

Owns exactly one responsibility (docs/DESIGN.md 8.1): turning input text
into embedding vectors via a locally-hosted `sentence-transformers` model.
No index, no conversation state, no filter logic - the vectors this
returns are stored and queried entirely by the first-party pgvector schema
(docs/DESIGN.md 8.4), not by this process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from tc_embedding_sidecar.middleware import MaxBodySizeMiddleware
from tc_embedding_sidecar.model import EmbeddingModel, TooManyRequestsError
from tc_embedding_sidecar.schemas import EmbedRequest, EmbedResponse, HealthResponse
from tc_embedding_sidecar.settings import get_settings

logger = logging.getLogger(__name__)

MODEL_NOT_READY_DETAIL = "model is still loading"

# Delays between model-load attempts. A monkeypatchable module attribute
# (not a plain local constant) so tests can shrink or empty it rather than
# actually waiting through real backoff delays - see tests/test_lifespan.py.
MODEL_LOAD_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)


def _terminate_process() -> None:
    """Ask this process to shut down.

    Called only after every retry in `MODEL_LOAD_RETRY_DELAYS_SECONDS` has
    also failed. Sending the process its own SIGTERM triggers uvicorn's
    normal graceful-shutdown handling, so the container's restart policy
    (`docker-compose.yml`'s `restart: unless-stopped`) gives model loading a
    fresh attempt - rather than the process staying alive with `/health`
    stuck at 503 forever and nothing automatically retrying or restarting.
    A monkeypatchable module-level function, not inlined, so tests can
    observe "gave up" without actually killing the test process.
    """
    os.kill(os.getpid(), signal.SIGTERM)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bind and start serving immediately; load the model in the background.

    Under the ASGI lifespan protocol, uvicorn refuses connections until
    everything before `yield` completes. Awaiting `model.load()` here would
    make the `/health`/`/embed` 503-while-loading branches below dead code
    in the real deployed process (only reachable via a test's overridden
    lifespan) - a request during the ~30s cold start would get connection-
    refused, never an HTTP 503. Loading in a background task instead makes
    that 503 a real, observable state a caller (or a Docker healthcheck)
    can actually see.
    """
    settings = get_settings()
    model = EmbeddingModel(
        settings.model_id,
        revision=settings.model_revision,
        max_concurrent_encodes=settings.max_concurrent_encodes,
        max_queued_encodes=settings.max_queued_encodes,
    )
    app.state.model = model
    logger.info(
        "embedding_sidecar.loading_model",
        extra={"model_id": settings.model_id, "model_revision": settings.model_revision},
    )

    async def _load() -> None:
        delays = (0.0, *MODEL_LOAD_RETRY_DELAYS_SECONDS)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await model.load()
            except Exception:
                logger.exception(
                    "embedding_sidecar.model_load_attempt_failed",
                    extra={
                        "model_id": settings.model_id,
                        "attempt": attempt,
                        "attempts_remaining": len(delays) - attempt,
                    },
                )
                continue
            logger.info(
                "embedding_sidecar.model_ready",
                extra={"model_id": settings.model_id, "dimensions": model.dimensions},
            )
            return

        # Every attempt failed. Logged, then the process is asked to exit -
        # see `_terminate_process`'s own docstring for why a restart, not a
        # silently-permanent 503, is the right outcome here.
        logger.error(
            "embedding_sidecar.model_load_failed_permanently",
            extra={"model_id": settings.model_id, "attempts": len(delays)},
        )
        _terminate_process()

    load_task = asyncio.create_task(_load())
    try:
        yield
    finally:
        if not load_task.done():
            load_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await load_task


def create_app(*, lifespan_handler: object | None = None) -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Thought Capture AI - Embedding Sidecar",
        version="0.1.0",
        summary="Local sentence-transformers embedding service (docs/adr/0010).",
        lifespan=lifespan_handler or lifespan,  # type: ignore[arg-type]
    )
    # Runs before FastAPI/Pydantic ever buffer or parse the request body -
    # middleware.py's own docstring has the full reasoning for why the
    # per-field checks in the /embed handler below are not, by themselves,
    # early enough to bound memory use.
    app.add_middleware(
        MaxBodySizeMiddleware,
        max_bytes=settings.max_request_bytes,
        max_read_seconds=settings.max_body_read_seconds,
    )

    @app.get("/health", response_model=HealthResponse, summary="Readiness: model loaded and usable")
    async def health() -> HealthResponse:
        model: EmbeddingModel = app.state.model
        if not model.is_ready:
            raise HTTPException(status_code=503, detail=MODEL_NOT_READY_DETAIL)
        return HealthResponse(
            status="ok",
            model_id=model.model_id,
            model_revision=model.revision,
            dimensions=model.dimensions,
        )

    @app.post("/embed", response_model=EmbedResponse, summary="Batch text in, vectors out")
    async def embed(request: EmbedRequest) -> EmbedResponse:
        model: EmbeddingModel = app.state.model
        if not model.is_ready:
            raise HTTPException(status_code=503, detail=MODEL_NOT_READY_DETAIL)
        if len(request.texts) > settings.max_batch_size:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"batch of {len(request.texts)} texts exceeds the "
                    f"{settings.max_batch_size}-text limit"
                ),
            )
        too_long = [
            i for i, text in enumerate(request.texts) if len(text) > settings.max_text_length
        ]
        if too_long:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"texts at index {too_long} exceed the "
                    f"{settings.max_text_length}-character limit per text"
                ),
            )
        try:
            vectors = await model.embed(request.texts)
        except TooManyRequestsError as exc:
            raise HTTPException(
                status_code=429, detail=str(exc), headers={"Retry-After": "1"}
            ) from exc
        return EmbedResponse(
            model_id=model.model_id,
            model_revision=model.revision,
            dimensions=model.dimensions,
            vectors=tuple(tuple(v) for v in vectors),
        )

    return app

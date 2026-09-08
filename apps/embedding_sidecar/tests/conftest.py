"""Fixtures for the sidecar's own unit test suite.

Deliberately never loads the real `sentence-transformers` model: that is
what the repository-root contract tests (`tests/contract/embedding_sidecar/`,
run against a real built container) are for. These tests exercise only the
HTTP transport - request validation, the not-ready-yet 503, and the
response shape - against a fake model that mimics `EmbeddingModel`'s public
surface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tc_embedding_sidecar.app import create_app

FAKE_MODEL_ID = "fake/stub-embedder"
FAKE_MODEL_REVISION = "fake0000revision0000sha"
FAKE_DIMENSIONS = 4


class FakeEmbeddingModel:
    def __init__(self, *, ready: bool = True) -> None:
        self.model_id = FAKE_MODEL_ID
        self.revision = FAKE_MODEL_REVISION
        self.dimensions = FAKE_DIMENSIONS
        self._ready = ready

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # A deterministic, cheap stand-in vector - only its length and
        # per-text ordering matter for these transport-level tests.
        return [[float(len(text))] * self.dimensions for text in texts]


def _lifespan_with(model: FakeEmbeddingModel) -> object:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.model = model
        yield

    return lifespan


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(lifespan_handler=_lifespan_with(FakeEmbeddingModel(ready=True)))
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        yield http


@pytest.fixture
async def not_ready_client() -> AsyncIterator[AsyncClient]:
    app = create_app(lifespan_handler=_lifespan_with(FakeEmbeddingModel(ready=False)))
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        yield http

"""The real (non-test-override) `lifespan` - background model loading.

`tests/conftest.py`'s `client`/`not_ready_client` fixtures bypass the real
`lifespan` entirely via `lifespan_handler`, so they cannot catch a
regression in `lifespan` itself (e.g. going back to awaiting `model.load()`
before `yield`, which would make the app stop accepting connections at all
during the load window instead of serving a real 503). These tests drive
`create_app()`'s actual default lifespan, with `EmbeddingModel` itself
monkeypatched to a controllable fake so no real model is downloaded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from httpx import ASGITransport, AsyncClient

from tc_embedding_sidecar.app import create_app

FAKE_DIMENSIONS = 4


class _ControllableFakeModel:
    """Stands in for `EmbeddingModel`; `load()` blocks until `release` is set."""

    def __init__(self, model_id: str, *, release: asyncio.Event, fail: bool = False) -> None:
        self.model_id = model_id
        self.dimensions = 0
        self._ready = False
        self._release = release
        self._fail = fail

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def load(self) -> None:
        await self._release.wait()
        if self._fail:
            raise RuntimeError("synthetic load failure")
        self.dimensions = FAKE_DIMENSIONS
        self._ready = True

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self.dimensions for _ in texts]


async def test_the_app_serves_503_while_the_model_loads_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    monkeypatch.setattr(
        "tc_embedding_sidecar.app.EmbeddingModel",
        lambda model_id: _ControllableFakeModel(model_id, release=release),
    )

    app = create_app()  # the real lifespan - no override.
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        # Lifespan startup has already returned (the `async with` above only
        # completed because it did) - proof the app is serving requests
        # despite the model not having finished loading yet.
        still_loading = await http.get("/health")
        assert still_loading.status_code == 503

        release.set()
        # No API to await "the background task specifically" from outside -
        # poll briefly instead of a fixed sleep, so this isn't flaky under
        # a slow CI runner.
        for _ in range(50):
            if (await http.get("/health")).status_code == 200:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("model never became ready after release.set()")

        ready = await http.get("/health")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ok", "model_id": "thenlper/gte-small", "dimensions": 4}


async def test_a_failed_load_leaves_health_at_503_rather_than_crashing_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    monkeypatch.setattr(
        "tc_embedding_sidecar.app.EmbeddingModel",
        lambda model_id: _ControllableFakeModel(model_id, release=release, fail=True),
    )
    release.set()  # fail immediately once the background task starts.

    app = create_app()
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        # Give the background task a moment to actually run and fail - the
        # fake model's `load()` fails immediately once released, so a short
        # fixed wait is enough (unlike the success case above, there is no
        # "ready" state to poll for).
        await asyncio.sleep(0.05)

        response = await http.get("/health")
        assert response.status_code == 503

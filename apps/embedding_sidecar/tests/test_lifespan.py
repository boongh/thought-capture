"""The real (non-test-override) `lifespan` - background model loading,
retry-then-give-up behavior on a persistently failing load.

`tests/conftest.py`'s `client`/`not_ready_client` fixtures bypass the real
`lifespan` entirely via `lifespan_handler`, so they cannot catch a
regression in `lifespan` itself (e.g. going back to awaiting `model.load()`
before `yield`, which would make the app stop accepting connections at all
during the load window instead of serving a real 503). These tests drive
`create_app()`'s actual default lifespan, with `EmbeddingModel` itself
monkeypatched to a controllable fake so no real model is downloaded, and
`_terminate_process` monkeypatched so a "give up" outcome is observable
without actually sending this test process a SIGTERM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from httpx import ASGITransport, AsyncClient

from tc_embedding_sidecar.app import create_app

FAKE_DIMENSIONS = 4


class _ControllableFakeModel:
    """Stands in for `EmbeddingModel`. `load()` blocks until `release` is
    set, then fails `fail_times` times before succeeding (or fails forever
    if `fail_times` is `None`)."""

    def __init__(
        self,
        model_id: str,
        *,
        release: asyncio.Event,
        fail_times: int | None = 0,
        **_ignored: object,
    ) -> None:
        self.model_id = model_id
        self.revision = "fake-revision"
        self.dimensions = 0
        self._ready = False
        self._release = release
        self._fail_times = fail_times
        self._attempts = 0

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def load(self) -> None:
        await self._release.wait()
        self._attempts += 1
        if self._fail_times is None or self._attempts <= self._fail_times:
            raise RuntimeError(f"synthetic load failure (attempt {self._attempts})")
        self.dimensions = FAKE_DIMENSIONS
        self._ready = True

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self.dimensions for _ in texts]


def _patch_model(monkeypatch: pytest.MonkeyPatch, release: asyncio.Event, **kwargs: object) -> None:
    monkeypatch.setattr(
        "tc_embedding_sidecar.app.EmbeddingModel",
        lambda model_id, **_ignored: _ControllableFakeModel(model_id, release=release, **kwargs),
    )


async def test_the_app_serves_503_while_the_model_loads_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    _patch_model(monkeypatch, release)

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
        body = ready.json()
        assert body["status"] == "ok"
        assert body["dimensions"] == FAKE_DIMENSIONS


async def test_a_transient_load_failure_is_retried_until_it_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first attempt fails; the retry (with a shrunk delay, so this test
    doesn't actually wait through real backoff) succeeds - the model must
    end up ready, and `_terminate_process` must never be called."""
    monkeypatch.setattr("tc_embedding_sidecar.app.MODEL_LOAD_RETRY_DELAYS_SECONDS", (0.01,))
    terminated = False

    def fake_terminate() -> None:
        nonlocal terminated
        terminated = True

    monkeypatch.setattr("tc_embedding_sidecar.app._terminate_process", fake_terminate)

    release = asyncio.Event()
    _patch_model(monkeypatch, release, fail_times=1)

    app = create_app()
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        release.set()
        for _ in range(100):
            if (await http.get("/health")).status_code == 200:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("model never became ready after the retried attempt succeeded")

        assert not terminated


async def test_a_persistent_load_failure_gives_up_and_asks_the_process_to_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every attempt fails - `/health` must stay at 503 forever (never crash
    the ASGI app itself), and the process must be asked to terminate rather
    than sitting unhealthy with nothing ever retrying or restarting it."""
    # Zero retries: fails fast instead of waiting through real backoff delays.
    monkeypatch.setattr("tc_embedding_sidecar.app.MODEL_LOAD_RETRY_DELAYS_SECONDS", ())
    terminate_calls = 0

    def fake_terminate() -> None:
        nonlocal terminate_calls
        terminate_calls += 1

    monkeypatch.setattr("tc_embedding_sidecar.app._terminate_process", fake_terminate)

    release = asyncio.Event()
    _patch_model(monkeypatch, release, fail_times=None)
    release.set()  # fail immediately once the background task starts.

    app = create_app()
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        # Give the background task a moment to actually run, fail once (its
        # only configured attempt), and give up.
        await asyncio.sleep(0.05)

        response = await http.get("/health")
        assert response.status_code == 503

    assert terminate_calls == 1

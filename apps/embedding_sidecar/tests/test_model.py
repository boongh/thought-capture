"""`EmbeddingModel`'s own admission-bound counting logic.

`tests/test_app.py`'s `busy_client` fixture proves the HTTP-layer mapping
(`TooManyRequestsError` -> 429); this proves the counting logic that raises
it in the first place, independent of the HTTP layer and without loading a
real model.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import numpy as np
import pytest

from tc_embedding_sidecar.model import EmbeddingModel, TooManyRequestsError


class _SlowStubModel:
    """Stands in for `SentenceTransformer`. Runs inside a real OS thread via
    `asyncio.to_thread`, so it blocks on a `threading.Event`, not an
    `asyncio.Event` - the latter is not safe to wait on from a plain
    thread. Returns real numpy arrays, not plain lists: `EmbeddingModel
    .embed()` calls `.tolist()` on each result, matching what
    `SentenceTransformer.encode(..., convert_to_numpy=True)` actually
    returns."""

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def encode(self, texts: list[str], **_kwargs: Any) -> Any:
        self._release.wait(timeout=5)
        return np.array([[0.0, 0.0] for _ in texts])


def _model_with_stub(release: threading.Event, **kwargs: Any) -> EmbeddingModel:
    model = EmbeddingModel("fake/id", revision="fake-revision", **kwargs)
    model.dimensions = 2
    # Bypasses `load()` entirely - these tests are about the admission
    # counter around `embed()`, not model loading.
    model._model = _SlowStubModel(release)
    return model


async def test_embed_raises_once_admission_capacity_is_reached() -> None:
    release = threading.Event()
    model = _model_with_stub(release, max_concurrent_encodes=1, max_queued_encodes=1)

    # Fills both the 1 in-flight + 1 queued slot with calls that won't
    # return until `release` is set.
    first = asyncio.create_task(model.embed(["a"]))
    second = asyncio.create_task(model.embed(["b"]))
    await asyncio.sleep(0.05)  # let both actually register admission

    with pytest.raises(TooManyRequestsError):
        await model.embed(["c"])

    release.set()
    await first
    await second


async def test_admission_frees_up_once_in_flight_work_completes() -> None:
    release = threading.Event()
    release.set()  # nothing blocks; each embed() completes immediately.
    model = _model_with_stub(release, max_concurrent_encodes=1, max_queued_encodes=0)

    # Capacity is exactly 1; sequential calls must each succeed because the
    # prior one has already released its slot before the next begins.
    first = await model.embed(["a"])
    second = await model.embed(["b"])

    assert first == [[0.0, 0.0]]
    assert second == [[0.0, 0.0]]

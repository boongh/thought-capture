"""`EmbeddingModel`'s own admission-bound counting logic and the single
CPU-bound `embed()` entry point (tokenize-for-truncation, then encode).

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

from tc_embedding_sidecar.model import EmbeddingModel, EncodeResult, TooManyRequestsError


class _StubTokenizer:
    """Stands in for the HF tokenizer `SentenceTransformer.tokenizer`
    exposes. A real tokenizer's `.encode()` returns one int id per token
    plus special tokens; only the *length* of that list matters here, so
    this counts whitespace-separated words as a stand-in for token count."""

    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]:
        token_count = len(text.split())
        return list(range(token_count + (2 if add_special_tokens else 0)))


class _StubModelWithTokenizer:
    """Stands in for `SentenceTransformer` for the truncation-flag tests -
    exposes `max_seq_length`, `tokenizer`, and a trivial `encode()` so the
    single `embed()` entry point can be exercised end to end."""

    def __init__(self, max_seq_length: int | None) -> None:
        self.max_seq_length = max_seq_length
        self.tokenizer = _StubTokenizer()

    def encode(self, texts: list[str], **_kwargs: Any) -> Any:
        return np.array([[0.0, 0.0] for _ in texts])


def _model_with_tokenizer_stub(max_seq_length: int | None) -> EmbeddingModel:
    model = EmbeddingModel("fake/id", revision="fake-revision")
    model.dimensions = 2
    model._model = _StubModelWithTokenizer(max_seq_length)
    return model


async def test_embed_reports_false_for_a_short_text() -> None:
    model = _model_with_tokenizer_stub(max_seq_length=512)

    result = await model.embed(["hello world"])

    assert result.truncated == (False,)


async def test_embed_reports_true_for_a_text_over_the_token_limit() -> None:
    model = _model_with_tokenizer_stub(max_seq_length=512)
    # 600 whitespace-separated words tokenizes (via the stub) to well over
    # the 512-token limit once the two special tokens are added.
    long_text = "word " * 600

    result = await model.embed([long_text])

    assert result.truncated == (True,)


async def test_embed_reports_one_truncation_flag_per_text_in_order() -> None:
    model = _model_with_tokenizer_stub(max_seq_length=512)

    result = await model.embed(["hello world", "word " * 600, "short"])

    assert result.truncated == (False, True, False)


async def test_embed_with_an_empty_batch_returns_no_vectors_or_flags() -> None:
    model = _model_with_tokenizer_stub(max_seq_length=512)

    result = await model.embed([])

    assert result == EncodeResult(vectors=[], truncated=())


async def test_embed_reports_all_false_when_the_model_declares_no_max_seq_length() -> None:
    # `max_seq_length is None` still means "nothing to report as truncated",
    # never a guess.
    model = _model_with_tokenizer_stub(max_seq_length=None)

    result = await model.embed(["word " * 600])

    assert result.truncated == (False,)


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
        self.max_seq_length = None  # no truncation bookkeeping needed here

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

    assert first.vectors == [[0.0, 0.0]]
    assert second.vectors == [[0.0, 0.0]]


# ---------------------------------------------------------------------------
# F14 regression: both CPU-bound phases (tokenize-for-truncation, encode)
# must run inside the SAME admitted, semaphore-held call - not one after the
# other has already released. See docs/plans/embedding-sync-review-round-3.md
# section F14.
# ---------------------------------------------------------------------------


class _CpuPhaseTracker:
    """Shared state a fake model's `tokenizer.encode` and `encode` both
    touch, so a test can observe how many requests are inside *either*
    CPU-bound phase at once, and hold one of them open on demand."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.in_phase = 0
        self.peak = 0

    def enter(self) -> None:
        with self.lock:
            self.in_phase += 1
            self.peak = max(self.peak, self.in_phase)

    def exit(self) -> None:
        with self.lock:
            self.in_phase -= 1


class _TrackingTokenizer:
    def __init__(self, tracker: _CpuPhaseTracker, gate: threading.Event | None) -> None:
        self._tracker = tracker
        self._gate = gate

    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]:
        self._tracker.enter()
        try:
            if self._gate is not None:
                self._gate.wait(timeout=5)
            return [0, 1, 2]
        finally:
            self._tracker.exit()


class _TrackingModel:
    """A fake `SentenceTransformer` whose tokenizer and `encode()` both
    report into a shared `_CpuPhaseTracker`, letting a test observe the
    peak number of requests concurrently inside either CPU-bound phase.

    `encode()` optionally rendezvous on a `threading.Barrier` before
    returning: this makes "N concurrently admitted calls actually overlap"
    deterministic rather than a matter of thread-scheduling luck - every
    admitted call blocks in `encode()` until exactly `barrier.parties`
    calls have arrived, so the tracker's peak can only be exactly that
    number, never accidentally lower.
    """

    def __init__(
        self,
        tracker: _CpuPhaseTracker,
        *,
        gate: threading.Event | None = None,
        barrier: threading.Barrier | None = None,
    ) -> None:
        self.max_seq_length = 512
        self.tokenizer = _TrackingTokenizer(tracker, gate)
        self._tracker = tracker
        self._barrier = barrier

    def encode(self, texts: list[str], **_kwargs: Any) -> Any:
        self._tracker.enter()
        try:
            if self._barrier is not None:
                self._barrier.wait(timeout=5)
            return np.array([[0.0, 0.0] for _ in texts])
        finally:
            self._tracker.exit()


def _model_with_tracker(
    tracker: _CpuPhaseTracker,
    *,
    gate: threading.Event | None = None,
    barrier: threading.Barrier | None = None,
    **kwargs: Any,
) -> EmbeddingModel:
    model = EmbeddingModel("fake/id", revision="fake-revision", **kwargs)
    model.dimensions = 2
    model._model = _TrackingModel(tracker, gate=gate, barrier=barrier)
    return model


async def test_the_configured_bound_covers_both_cpu_bound_phases() -> None:
    """With `max_concurrent_encodes=1`, several concurrent `embed()` calls
    must never observe more than one request inside tokenize-or-encode at
    once. THIS TEST MUST FAIL against the pre-fix code, where `embed()`
    releases its semaphore/admission slot before the separate
    `detect_truncation()` call ever starts its own unbounded worker
    thread."""
    tracker = _CpuPhaseTracker()
    model = _model_with_tracker(tracker, max_concurrent_encodes=1, max_queued_encodes=8)

    await asyncio.gather(*(model.embed(["hello world"]) for _ in range(5)))

    assert tracker.peak == 1


async def test_admission_covers_the_tokenize_phase() -> None:
    """With `max_concurrent_encodes=1, max_queued_encodes=0`, a second
    `embed()` issued while the first is inside its *tokenize* phase must be
    rejected - proving admission now spans tokenize, not just encode."""
    tracker = _CpuPhaseTracker()
    gate = threading.Event()  # holds the first call inside tokenize
    model = _model_with_tracker(tracker, gate=gate, max_concurrent_encodes=1, max_queued_encodes=0)

    first = asyncio.create_task(model.embed(["hello world"]))
    # Wait until the first call is actually inside the tokenize phase
    # (tracker.enter() has run) before issuing the second.
    for _ in range(100):
        await asyncio.sleep(0.01)
        if tracker.in_phase >= 1:
            break
    assert tracker.in_phase == 1, "first call never entered its tokenize phase"

    with pytest.raises(TooManyRequestsError):
        await model.embed(["a second request"])

    gate.set()
    await first


async def test_two_concurrent_encodes_are_permitted_when_configured() -> None:
    """`max_concurrent_encodes=2` must observe a peak of exactly 2 - the
    bound is respected, not accidentally serialized down to 1. Four calls,
    a barrier of 2 parties: the first pair rendezvous and prove the peak
    reaches 2, then the second pair does the same once the semaphore
    admits it."""
    tracker = _CpuPhaseTracker()
    barrier = threading.Barrier(2)
    model = _model_with_tracker(
        tracker, barrier=barrier, max_concurrent_encodes=2, max_queued_encodes=8
    )

    await asyncio.gather(*(model.embed(["hello world"]) for _ in range(4)))

    assert tracker.peak == 2

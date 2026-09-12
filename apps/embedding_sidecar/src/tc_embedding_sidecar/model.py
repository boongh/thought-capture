"""Wraps `sentence-transformers` behind an async-friendly interface.

`SentenceTransformer.encode` (and loading the model itself) is synchronous
and CPU-bound (docs/adr/0010 §5: CPU-only, ~30s cold start); both run in a
worker thread via `asyncio.to_thread` so a request never blocks the event
loop for the duration of a forward pass.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sentence_transformers import SentenceTransformer


class TooManyRequestsError(Exception):
    """Raised when admission is already at `max_concurrent_encodes +
    max_queued_encodes` - the caller should surface this as HTTP 429."""


@dataclass(frozen=True)
class EncodeResult:
    """The outcome of one admitted `EmbeddingModel.embed()` call: one vector
    and one truncation flag per input text, in input order."""

    vectors: list[list[float]]
    truncated: tuple[bool, ...]


def _detect_truncation(model: Any, texts: Sequence[str]) -> tuple[bool, ...]:
    """Report, per text, whether encoding it would silently truncate it.

    `sentence-transformers`' `encode()` truncates any input longer than
    `max_seq_length` tokens with no signal in its output (Decision D,
    docs/plans/khoj-retirement-completion.md) - the only way to observe it
    is to tokenize first and compare lengths, not to inspect the resulting
    vector. Runs synchronously inside the same worker thread as the encode
    call that follows it (see `EmbeddingModel._encode_and_flag`) - never
    awaited or admitted on its own.
    """
    max_seq_length = model.max_seq_length
    if max_seq_length is None:
        # No declared limit to compare against - nothing to report as
        # truncated rather than guessing at one.
        return tuple(False for _ in texts)
    tokenizer = model.tokenizer
    return tuple(
        len(tokenizer.encode(text, add_special_tokens=True)) > max_seq_length for text in texts
    )


class EmbeddingModel:
    def __init__(
        self,
        model_id: str,
        *,
        revision: str,
        max_concurrent_encodes: int = 1,
        max_queued_encodes: int = 8,
    ) -> None:
        self.model_id = model_id
        self.revision = revision
        self.dimensions = 0
        self._model: SentenceTransformer | None = None
        # Bounds concurrent `encode()` calls (settings.py's own docstring has
        # the full reasoning): CPU-bound work does not parallelize usefully
        # across threads here, so concurrent requests queue instead of each
        # spawning their own worker-thread forward pass.
        self._encode_semaphore = asyncio.Semaphore(max_concurrent_encodes)
        # A *separate* admission cap from the semaphore above: the semaphore
        # only bounds how many `encode()` calls run at once, not how many
        # already-parsed requests may be waiting behind it - an unbounded
        # queue would let unlimited validated payloads pile up in memory
        # while waiting their turn. `_admitted` is incremented/decremented
        # with no `await` in between, so it needs no lock: asyncio only
        # switches coroutines at an `await` point, making that check-and-
        # increment atomic within this single-threaded event loop.
        self._max_admitted = max_concurrent_encodes + max_queued_encodes
        self._admitted = 0

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    async def load(self) -> None:
        model = await asyncio.to_thread(SentenceTransformer, self.model_id, revision=self.revision)
        dimensions = model.get_embedding_dimension()
        if dimensions is None:
            raise RuntimeError(f"model {self.model_id!r} did not report an embedding dimension")
        self.dimensions = dimensions
        # Published only once fully loaded, so `is_ready` cannot observe a
        # half-initialized model from another task.
        self._model = model

    def _encode_and_flag(self, texts: list[str]) -> EncodeResult:
        """Run both CPU-bound phases for one request inside a single worker
        thread: tokenize-for-truncation first, then encode.

        Tokenizing first means a tokenizer failure never costs a wasted
        forward pass. Note the cost this preserves rather than introduces:
        each text is tokenized twice per request - once here for the
        truncation flag, once again inside `SentenceTransformer.encode`'s
        own internal tokenization. That duplication already existed before
        this method did; what changes here is that both phases now run
        under the same admitted, semaphore-held call instead of the second
        phase running unbounded after the first released its slot.
        `max_text_length` remains the per-request ceiling on it.
        """
        model = self._model
        assert model is not None  # narrowed by the caller before to_thread
        truncated = _detect_truncation(model, texts)
        vectors = model.encode(texts, convert_to_numpy=True)
        return EncodeResult(vectors=[vector.tolist() for vector in vectors], truncated=truncated)

    async def embed(self, texts: Sequence[str]) -> EncodeResult:
        """Tokenize-for-truncation and encode `texts`, both under one
        admitted, semaphore-held call - see `_encode_and_flag`."""
        if self._model is None:
            raise RuntimeError("embed() called before the model finished loading")
        if not texts:
            return EncodeResult(vectors=[], truncated=())
        if self._admitted >= self._max_admitted:
            raise TooManyRequestsError(
                f"already at capacity ({self._max_admitted} concurrent/queued encode requests)"
            )
        self._admitted += 1
        try:
            async with self._encode_semaphore:
                return await asyncio.to_thread(self._encode_and_flag, list(texts))
        finally:
            self._admitted -= 1

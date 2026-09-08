"""Wraps `sentence-transformers` behind an async-friendly interface.

`SentenceTransformer.encode` (and loading the model itself) is synchronous
and CPU-bound (docs/adr/0010 §5: CPU-only, ~30s cold start); both run in a
worker thread via `asyncio.to_thread` so a request never blocks the event
loop for the duration of a forward pass.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sentence_transformers import SentenceTransformer


class TooManyRequestsError(Exception):
    """Raised when admission is already at `max_concurrent_encodes +
    max_queued_encodes` - the caller should surface this as HTTP 429."""


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

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._model is None:
            raise RuntimeError("embed() called before the model finished loading")
        if not texts:
            return []
        if self._admitted >= self._max_admitted:
            raise TooManyRequestsError(
                f"already at capacity ({self._max_admitted} concurrent/queued encode requests)"
            )
        self._admitted += 1
        try:
            async with self._encode_semaphore:
                vectors = await asyncio.to_thread(
                    self._model.encode, list(texts), convert_to_numpy=True
                )
        finally:
            self._admitted -= 1
        return [vector.tolist() for vector in vectors]

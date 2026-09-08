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


class EmbeddingModel:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.dimensions = 0
        self._model: SentenceTransformer | None = None

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    async def load(self) -> None:
        model = await asyncio.to_thread(SentenceTransformer, self.model_id)
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
        vectors = await asyncio.to_thread(self._model.encode, list(texts), convert_to_numpy=True)
        return [vector.tolist() for vector in vectors]

"""Request/response shapes for the sidecar's HTTP contract.

Deliberately a plain batch-in/batch-out shape - `docs/DESIGN.md` 8.1: this
process holds no index, no conversation state, no filter logic. The future
`EmbeddingPort`/`HttpEmbeddingClient` adapter (docs/adr/0010 §4) maps this
contract onto `EmbeddingVector`, one per input text.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EmbedRequest(BaseModel):
    texts: tuple[str, ...] = Field(
        default=(), description="Batch of raw text; one vector is returned per entry, in order."
    )


class EmbedResponse(BaseModel):
    model_id: str
    # A HF repo's default branch is mutable; this is the exact immutable
    # commit the returned vectors were computed from (settings.py has the
    # full reasoning). A future writer of `document_embeddings
    # .embedding_model_id` combines `model_id`/`model_revision` into one
    # identity string - not decided by this HTTP contract.
    model_revision: str
    dimensions: int
    vectors: tuple[tuple[float, ...], ...]


class HealthResponse(BaseModel):
    status: str
    model_id: str
    model_revision: str
    dimensions: int

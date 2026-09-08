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
    dimensions: int
    vectors: tuple[tuple[float, ...], ...]


class HealthResponse(BaseModel):
    status: str
    model_id: str
    dimensions: int

"""Self-hosted embedding sidecar adapter (docs/adr/0010 §4-5)."""

from __future__ import annotations

from tc_infrastructure.embedding.client import HttpEmbeddingClient, compose_embedding_model_id

__all__ = ["HttpEmbeddingClient", "compose_embedding_model_id"]

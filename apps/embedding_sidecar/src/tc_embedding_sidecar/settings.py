"""Sidecar configuration.

Deliberately not `tc_infrastructure.config.Settings`: this process does not
share a Python version with the main workspace (docs/adr/0010 §5), so it
cannot import first-party code from it either. A separate, minimal settings
object is the isolation boundary, not an oversight.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TC_EMBEDDING_")

    # docs/adr/0010 §5's recommended model: the same one Khoj already used,
    # with direct operational evidence (CPU-only, ~30s cold start, 384 dims).
    model_id: str = "thenlper/gte-small"
    # A Hugging Face repo's default branch is mutable - "thenlper/gte-small"
    # alone can silently resolve to different weights on a later rebuild
    # while every stored `document_embeddings.embedding_model_id` still
    # reads the same string, defeating docs/DESIGN.md 8.5's "never compare
    # vectors from different models" guarantee. Pinning this exact commit
    # (found via `HfApi().model_info("thenlper/gte-small").sha`) makes the
    # downloaded weights immutable and content-hash-verified by
    # huggingface_hub itself; `EmbedResponse`/`HealthResponse` report it
    # alongside `model_id` so a future revision bump is visible to whatever
    # writes `embedding_model_id` and can be treated as a different model
    # requiring a `reembed` run, not silently absorbed.
    model_revision: str = "17e1f347d17fe144873b1201da91788898c639cd"
    host: str = "0.0.0.0"
    port: int = 8081

    # Bounds on `POST /embed` (docs/DESIGN.md 8.1: "a stateless embed(texts)
    # -> vectors call", not an unbounded one). `SentenceTransformer.encode`
    # is CPU-bound and synchronous; without a cap, an arbitrarily large batch
    # or arbitrarily long strings tie up the process for an unbounded time,
    # and unbounded concurrent requests each spawn their own worker-thread
    # encode - a local resource-exhaustion vector today, and an
    # unauthenticated network DoS vector if this service were ever reachable
    # beyond the loopback-only Compose topology it currently has.
    max_batch_size: int = 64
    # gte-small's own max sequence length is 512 tokens; this is a generous
    # character ceiling above that (the tokenizer truncates the rest) purely
    # to bound how much CPU a single request can force this process to spend
    # tokenizing before truncation ever kicks in.
    max_text_length: int = 50_000
    # SentenceTransformer.encode() is CPU-bound work handed to a worker
    # thread (model.py); running more than one at once does not increase
    # throughput on a CPU-bound task, only memory pressure and context-switch
    # overhead, so concurrent requests are serialized rather than rejected.
    max_concurrent_encodes: int = 1


@lru_cache
def get_settings() -> Settings:
    return Settings()

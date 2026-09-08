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
    # How many additional requests may wait behind `max_concurrent_encodes`
    # before a new one is rejected outright (429) instead of queuing
    # indefinitely. An unbounded queue behind the semaphore would let an
    # unlimited number of already-validated requests each hold up to
    # `max_batch_size * max_text_length` of parsed payload in memory at
    # once - the semaphore only bounds *execution* concurrency, not
    # *admission*. Total admitted at once is
    # `max_concurrent_encodes + max_queued_encodes`.
    max_queued_encodes: int = 8
    # A hard ceiling on the raw request body, enforced by
    # `MaxBodySizeMiddleware` *before* FastAPI/Pydantic ever buffers or
    # parses it into Python objects - `max_batch_size`/`max_text_length`
    # alone only bound a body Pydantic has already fully parsed, which is
    # too late: a huge or chunked-transfer (no declared Content-Length)
    # request would already have forced the full parse first. Sized with
    # headroom over `max_batch_size * max_text_length`'s raw character total
    # (3.2 MB by default) for JSON structure/escaping overhead.
    max_request_bytes: int = 4_000_000
    # How long `MaxBodySizeMiddleware` will wait for a request body to
    # finish arriving before giving up (408). Without this, a client that
    # sends a body slower than `max_bytes` ever requires - a single byte
    # every few seconds, forever - holds its buffering slot open
    # indefinitely; the byte cap alone only defends against a body that is
    # too *large*, not one that simply never finishes (a slowloris-style
    # hold), and each such connection still occupies memory and a uvicorn
    # connection slot for as long as it is allowed to linger.
    max_body_read_seconds: float = 10.0
    # Passed straight through to uvicorn's own `limit_concurrency`: the
    # hard ceiling on concurrent connections/requests uvicorn will accept
    # at the transport level, *before* any of this process's own code
    # (including `MaxBodySizeMiddleware`'s body buffering) ever runs for
    # the request beyond it - uvicorn answers 503 itself. This is the
    # actual fix for "many concurrent requests each buffering their own
    # body": the request-level bounds above only ever bounded one request
    # at a time; nothing previously capped how many could be doing that
    # buffering simultaneously. Set comfortably above
    # `max_concurrent_encodes + max_queued_encodes` so legitimate bursts
    # (health checks alongside real traffic) are not the ones turned away.
    #
    # Confirmed by reading uvicorn's h11 protocol implementation directly
    # (not assumed): a connection is added to the tracked set in
    # `connection_made` - fired the instant the TCP connection is
    # accepted - and only removed in `connection_lost`. This limit
    # therefore *does* count a connection that never finishes sending its
    # request line/headers at all, not only ones with a complete request -
    # opening more than `limit_concurrency` such stalled connections
    # degrades the service to answering 503 for new legitimate requests
    # once the ceiling is reached, rather than exhausting memory/file
    # descriptors without bound. See `h11_max_incomplete_event_size` below
    # for the one further mitigation available without new infrastructure,
    # and its own docstring for the gap that remains even with both set.
    limit_concurrency: int = 32
    # Bounds how many bytes of a *single* incomplete HTTP event (e.g. a
    # request line/header block that has not yet terminated) uvicorn's h11
    # implementation will buffer before giving up on that connection -
    # uvicorn's own default (16 KiB), made explicit here rather than left
    # implicit. This is a size cap, not a time cap: a connection sending
    # a few bytes of valid-so-far header data every few seconds, forever,
    # is not rejected by this - only one sending more incomplete data than
    # this ceiling before ever completing its headers is. Combined with
    # `limit_concurrency` above, the residual gap is exactly what Codex's
    # review of this slice named: many connections held open with a small
    # amount of incomplete header data, sent slowly, are individually
    # within this size cap and are never time-boxed by uvicorn, degrading
    # the service to 503-for-everyone (bounded, not unbounded resource
    # exhaustion - `limit_concurrency` still caps how many such
    # connections can be held at once) for as long as an attacker sustains
    # them. Uvicorn has no built-in header-read timeout to close this the
    # rest of the way; doing so requires either a reverse proxy in front
    # (its own pinned image/config surface) or a hand-rolled asyncio
    # protocol-level timeout wrapper (nontrivial, easy to get subtly
    # wrong) - both out of scope for this slice, deliberately: this
    # service is loopback- and Docker-internal-network-only today, the
    # same perimeter every other `core` service (postgres, api) in this
    # stack already relies on with no proxy in front either. Revisit if
    # this service is ever exposed beyond that boundary.
    h11_max_incomplete_event_size: int = 16_384


@lru_cache
def get_settings() -> Settings:
    return Settings()

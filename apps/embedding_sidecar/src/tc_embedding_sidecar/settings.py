"""Sidecar configuration.

Deliberately not `tc_infrastructure.config.Settings`: this process does not
share a Python version with the main workspace (docs/adr/0010 §5), so it
cannot import first-party code from it either. A separate, minimal settings
object is the isolation boundary, not an oversight.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Two connection slots uvicorn's own accounting costs on top of whatever this
# process admits - see `Settings._limit_concurrency_covers_admission` for
# where each one comes from and how it was verified.
_UVICORN_CONNECTION_SLOT_OVERHEAD = 2

# A JSON string's worst-case expansion is 6 bytes out per 1 byte in, via a
# control character escaping to `\u0000`. This is the real ceiling for the
# first-party client, httpx 0.28.1's `_content.encode_json`, which uses
# `ensure_ascii=False` with compact separators - so any content, including
# an all-control-character string, expands by at most this factor.
_JSON_WORST_CASE_BYTES_PER_TEXT_BYTE = 6  # control char -> "\u0000": 1 byte in, 6 out
_JSON_ENVELOPE_FIXED_BYTES = 12  # len('{"texts":[]}')
_JSON_PER_TEXT_OVERHEAD_BYTES = 3  # 2 quote bytes + 1 comma per text (safe over-estimate of 3n-1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TC_EMBEDDING_")

    # docs/adr/0010 §5's recommended model: the same one Khoj already used,
    # with direct operational evidence (CPU-only, ~30s cold start, 384 dims).
    model_id: str = Field(default="thenlper/gte-small", min_length=1)
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
    model_revision: str = Field(default="17e1f347d17fe144873b1201da91788898c639cd", min_length=1)
    host: str = Field(default="0.0.0.0", min_length=1)
    port: int = Field(default=8081, ge=1, le=65535)

    # Bounds on `POST /embed` (docs/DESIGN.md 8.1: "a stateless embed(texts)
    # -> vectors call", not an unbounded one). `SentenceTransformer.encode`
    # is CPU-bound and synchronous; without a cap, an arbitrarily large batch
    # or arbitrarily long strings tie up the process for an unbounded time,
    # and unbounded concurrent requests each spawn their own worker-thread
    # encode - a local resource-exhaustion vector today, and an
    # unauthenticated network DoS vector if this service were ever reachable
    # beyond the loopback-only Compose topology it currently has.
    max_batch_size: int = Field(default=64, ge=1)
    # gte-small's own max sequence length is 512 tokens; this is a generous
    # UTF-8 byte ceiling above that (the tokenizer truncates the rest) purely
    # to bound how much CPU a single request can force this process to spend
    # tokenizing before truncation ever kicks in.
    max_text_bytes: int = Field(default=8_192, ge=1)
    # SentenceTransformer.encode() is CPU-bound work handed to a worker
    # thread (model.py); running more than one at once does not increase
    # throughput on a CPU-bound task, only memory pressure and context-switch
    # overhead, so concurrent requests are serialized rather than rejected.
    max_concurrent_encodes: int = Field(default=1, ge=1)
    # How many additional requests may wait behind `max_concurrent_encodes`
    # before a new one is rejected outright (429) instead of queuing
    # indefinitely. An unbounded queue behind the semaphore would let an
    # unlimited number of already-validated requests each hold up to
    # `max_batch_size * max_text_bytes` of parsed payload in memory at
    # once - the semaphore only bounds *execution* concurrency, not
    # *admission*. Total admitted at once is
    # `max_concurrent_encodes + max_queued_encodes`.
    max_queued_encodes: int = Field(default=8, ge=0)
    # A hard ceiling on the raw request body, enforced by
    # `MaxBodySizeMiddleware` *before* FastAPI/Pydantic ever buffers or
    # parses it into Python objects - `max_batch_size`/`max_text_bytes`
    # alone only bound a body Pydantic has already fully parsed, which is
    # too late: a huge or chunked-transfer (no declared Content-Length)
    # request would already have forced the full parse first. Sized with
    # headroom over `max_batch_size * max_text_bytes`'s worst-case JSON-
    # escaped total (see `_request_byte_cap_admits_the_worst_case_advertised_batch`)
    # so a batch at exactly the advertised limits is always admitted.
    max_request_bytes: int = Field(default=4_000_000, ge=1)
    # How long `MaxBodySizeMiddleware` will wait for a request body to
    # finish arriving before giving up (408). Without this, a client that
    # sends a body slower than `max_bytes` ever requires - a single byte
    # every few seconds, forever - holds its buffering slot open
    # indefinitely; the byte cap alone only defends against a body that is
    # too *large*, not one that simply never finishes (a slowloris-style
    # hold), and each such connection still occupies memory and a uvicorn
    # connection slot for as long as it is allowed to linger.
    max_body_read_seconds: float = Field(default=10.0, gt=0)
    # Passed straight through to uvicorn's own `limit_concurrency`: once
    # this many connections are open, a *complete* request arriving on a
    # connection beyond that count gets 503 from uvicorn itself, before
    # this process's own code (including `MaxBodySizeMiddleware`'s body
    # buffering) ever runs for it. This is the fix for "many *completed*
    # concurrent requests each buffering their own body": the request-level
    # bounds above only ever bounded one request at a time; nothing
    # previously capped how many could be doing that buffering
    # simultaneously. Set comfortably above `max_concurrent_encodes +
    # max_queued_encodes` so legitimate bursts (health checks alongside
    # real traffic) are not the ones turned away.
    #
    # This does **not** cap or close a connection that never completes its
    # request line/headers at all - confirmed by reading uvicorn's h11
    # protocol implementation directly, then verifying against a real
    # running container (300 simultaneous stalled, incomplete-header
    # connections; every one was accepted, none was ever refused or
    # closed). `connection_made` unconditionally accepts and tracks every
    # new TCP connection with no check against this limit at all; the
    # limit is consulted only when some *other* connection completes a
    # request, to decide whether *that* request gets 503. A prior version
    # of this comment claimed opening more than `limit_concurrency` stalled
    # connections "degrades the service... rather than exhausting memory/
    # file descriptors without bound" - that was wrong, caught by a second
    # review round: the stalled connections themselves are never rejected,
    # so they accumulate up to whatever the OS's file-descriptor/memory
    # limits allow, not a number this service controls. What *is* true, and
    # what `tests/contract/embedding_sidecar/test_embed_endpoint.py`'s
    # stalled-connection test actually proves: once enough stalled
    # connections exist to reach this count, *other*, complete requests
    # (e.g. a legitimate `/health` check) start getting 503 - a real,
    # verified symptom, but not evidence that the attack itself is capped.
    # See `h11_max_incomplete_event_size` below for the one further,
    # narrower mitigation available without new infrastructure.
    limit_concurrency: int = Field(default=32, ge=1)
    # Bounds how many bytes of a *single* incomplete HTTP event (e.g. a
    # request line/header block that has not yet terminated) uvicorn's h11
    # implementation will buffer before giving up on *that* connection -
    # uvicorn's own default (16 KiB), made explicit here rather than left
    # implicit. This bounds per-connection memory only; it does not bound
    # how many such connections may be held open at once (see
    # `limit_concurrency` above - confirmed not to cap this either) nor how
    # long one may be held (there is no time budget at all for the header-
    # read phase, unlike `max_body_read_seconds` for the body phase once a
    # request line is complete).
    #
    # Net residual gap, stated plainly: nothing in this process prevents an
    # attacker from opening an effectively unlimited number of connections
    # that each send a small amount of incomplete header data, slowly,
    # forever - bounded only by the host's file-descriptor and memory
    # limits, not by any setting here. Closing this requires either a
    # reverse proxy in front (its own pinned image/config surface, with
    # mature header-read-timeout and connection-cap handling) or a hand-
    # rolled asyncio accept-time gatekeeper (assessed and rejected for this
    # slice - a known asyncio failure mode makes a naive version risk
    # making things worse, e.g. an accept-retry busy-loop once a file-
    # descriptor limit is hit, and it would duplicate what a real proxy
    # already does more robustly). Deliberately accepted as residual risk
    # for this slice instead, given this service's actual reachability
    # today - loopback- and Docker-internal-network-only, the same
    # perimeter every other `core` service (postgres, api) in this stack
    # already relies on with no proxy in front of either. Revisit if this
    # service is ever exposed beyond that boundary.
    # Floor of 1 KiB, not 1: a value below that leaves no room for a
    # realistic request line plus a Host header, so h11 gives up on EVERY
    # connection mid-header and no HTTP request of any kind can complete -
    # including the Docker healthcheck. That is the same "starts, reports
    # healthy-ish, cannot serve" shape the rest of these bounds exist to
    # prevent, so it is rejected rather than accepted as a tuning choice.
    h11_max_incomplete_event_size: int = Field(default=16_384, ge=1024)

    @model_validator(mode="after")
    def _limit_concurrency_covers_admission(self) -> Settings:
        """uvicorn's ceiling must be able to hold everything admission accepts,
        *plus* the two slots its own accounting costs.

        `limit_concurrency` is enforced by uvicorn before this process's own
        code runs, so too low a value makes the admission queue dead
        configuration: uvicorn 503s the very requests `model.py` was sized to
        queue, and the operator's `TC_EMBEDDING_MAX_QUEUED_ENCODES` silently
        does nothing.

        The floor is `max_concurrent_encodes + max_queued_encodes + 2`, not
        that total itself, and the +2 is not padding - both slots were read
        out of the pinned uvicorn (`uvicorn/protocols/http/h11_impl.py`,
        identical in `httptools_impl.py`):

        * `connection_made` adds the connection to `self.connections`
          unconditionally, *before* the limit is consulted, and the test is
          ``len(self.connections) >= limit_concurrency``. So with
          ``limit_concurrency == L`` the L-th simultaneously-open connection
          is already 503'd, and only ``L - 1`` requests can ever reach
          admission. One slot pays for that off-by-one.
        * The test counts CONNECTIONS, not in-flight ``/embed`` requests. The
          container healthcheck opens its own connection every few seconds; at
          a full admission queue it would be the one turned away, and after
          `retries` failures the sidecar is marked unhealthy under legitimate
          load, failing `condition: service_healthy` for anything that depends
          on it. The second slot keeps that connection available.

        A hard startup failure rather than a warning, for the same reason the
        per-field bounds above exist at all: this whole class of
        misconfiguration produces a *green* `/health` in front of a service
        that cannot do its job, and a warning would reproduce exactly that.
        """
        admitted = self.max_concurrent_encodes + self.max_queued_encodes
        required = admitted + _UVICORN_CONNECTION_SLOT_OVERHEAD
        if self.limit_concurrency < required:
            raise ValueError(
                f"TC_EMBEDDING_LIMIT_CONCURRENCY={self.limit_concurrency} is below "
                f"max_concurrent_encodes + max_queued_encodes + "
                f"{_UVICORN_CONNECTION_SLOT_OVERHEAD} "
                f"({self.max_concurrent_encodes} + {self.max_queued_encodes} + "
                f"{_UVICORN_CONNECTION_SLOT_OVERHEAD} = {required}). uvicorn counts open "
                "connections, not requests, and 503s at >= the limit rather than above "
                "it, so anything lower rejects requests the admission queue was sized to "
                "accept and can starve the container healthcheck under load - making "
                "TC_EMBEDDING_MAX_QUEUED_ENCODES dead configuration. Raise "
                "TC_EMBEDDING_LIMIT_CONCURRENCY to at least that total, or lower the "
                "admission bounds."
            )
        return self

    @model_validator(mode="after")
    def _request_byte_cap_admits_the_worst_case_advertised_batch(self) -> Settings:
        """Every request within the advertised limits must fit the byte cap.

        `MaxBodySizeMiddleware` enforces `max_request_bytes` against the
        serialized JSON body's *bytes*, before Pydantic parses anything -
        but `max_text_bytes` bounds each text's *UTF-8 byte length before
        JSON-encoding*, and JSON string encoding can expand non-ASCII or
        control-character content well beyond that. Comparing the two
        directly (as an earlier version of this validator did) equates two
        different units and wrongly accepts configurations - including this
        module's own former defaults - where a maximum-sized, contract-legal
        `/embed` batch still gets 413'd by the middleware for some content.

        This validator instead computes the true worst case: every byte of
        every text escapes to its longest possible JSON form, a 6-byte
        `\\uXXXX` sequence (`_JSON_WORST_CASE_BYTES_PER_TEXT_BYTE`), plus the
        per-text quote/comma overhead and the fixed `{"texts":[]}` envelope.
        The guarantee this establishes: a request with `max_batch_size`
        texts, each up to `max_text_bytes` UTF-8 bytes, is *always* admitted
        by the byte-cap middleware, for ANY content - including text made
        entirely of control characters, JSON's worst case for size
        expansion.

        This also rules out the degenerate case the per-field `ge=1` bound
        alone allowed: `TC_EMBEDDING_MAX_REQUEST_BYTES=1` starts happily,
        answers `/health` with `status: ok` (a bodyless GET), and 413s every
        single `/embed`.
        """
        required = (
            self.max_batch_size * self.max_text_bytes * _JSON_WORST_CASE_BYTES_PER_TEXT_BYTE
            + self.max_batch_size * _JSON_PER_TEXT_OVERHEAD_BYTES
            + _JSON_ENVELOPE_FIXED_BYTES
        )
        if self.max_request_bytes < required:
            raise ValueError(
                f"TC_EMBEDDING_MAX_REQUEST_BYTES={self.max_request_bytes} is below the "
                "worst-case serialized size of a request at exactly the advertised "
                f"limits: TC_EMBEDDING_MAX_BATCH_SIZE ({self.max_batch_size}) * "
                f"TC_EMBEDDING_MAX_TEXT_BYTES ({self.max_text_bytes}) * "
                f"{_JSON_WORST_CASE_BYTES_PER_TEXT_BYTE} (worst-case JSON "
                "control-character escaping, one byte in becoming a 6-byte \\uXXXX "
                f"escape out) + {self.max_batch_size} * "
                f"{_JSON_PER_TEXT_OVERHEAD_BYTES} (per-text quote/comma overhead) + "
                f"{_JSON_ENVELOPE_FIXED_BYTES} (fixed envelope) = {required}. "
                "MaxBodySizeMiddleware would 413 requests those two settings say are "
                "acceptable, making them dead configuration. Raise "
                "TC_EMBEDDING_MAX_REQUEST_BYTES to at least that total, or lower "
                "TC_EMBEDDING_MAX_BATCH_SIZE/TC_EMBEDDING_MAX_TEXT_BYTES."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

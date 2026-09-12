"""The sidecar's real HTTP contract, against a real running container
(docs/adr/0010 §5, `docs/DESIGN.md` 8.5's "contract tests for the sidecar
cover its `embed` endpoint's request/response shape and failure modes").
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator

import httpx
import pytest

pytestmark = pytest.mark.contract

EXPECTED_MODEL_ID = "thenlper/gte-small"
# The immutable commit this project pins (settings.py's own docstring has
# the full reasoning) - asserting it here catches a rebuild that silently
# resolved a different revision, not just a different model id.
EXPECTED_MODEL_REVISION = "17e1f347d17fe144873b1201da91788898c639cd"
EXPECTED_DIMENSIONS = 384

# Must match apps/embedding_sidecar/src/tc_embedding_sidecar/settings.py's
# own defaults - duplicated here because the contract tests run in this
# project's own Python 3.14 environment and cannot import the sidecar's
# Python-3.12-only settings module directly. A mismatch between these
# constants and the real settings.py defaults is itself a silent-wrong-test
# hazard: a test here could pass or fail for the wrong reason if the sidecar's
# actual defaults ever drift without this file being updated to match. A
# self-describing contract via `/health` (reporting the configured limits
# back to callers) would close that gap; not fixed here (tracked as F20).
MAX_BATCH_SIZE = 64
MAX_TEXT_BYTES = 8_192
MAX_REQUEST_BYTES = 4_000_000
LIMIT_CONCURRENCY = 32


def _text_of_byte_length(byte_length: int, char: str = "x") -> str:
    """Builds a string whose UTF-8 encoding is exactly `byte_length` bytes,
    repeating `char` (which may be multibyte). `byte_length` must be evenly
    divisible by `char`'s own encoded byte length."""
    char_bytes = len(char.encode("utf-8"))
    assert byte_length % char_bytes == 0, (
        f"{byte_length} is not evenly divisible by {char!r}'s {char_bytes}-byte encoding"
    )
    text = char * (byte_length // char_bytes)
    assert len(text.encode("utf-8")) == byte_length
    return text


async def test_health_reports_the_ready_model(sidecar: httpx.AsyncClient) -> None:
    response = await sidecar.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model_id"] == EXPECTED_MODEL_ID
    assert body["model_revision"] == EXPECTED_MODEL_REVISION
    assert body["dimensions"] == EXPECTED_DIMENSIONS


async def test_embed_returns_a_vector_of_the_expected_dimensionality(
    sidecar: httpx.AsyncClient,
) -> None:
    response = await sidecar.post("/embed", json={"texts": ["a contract test sentence"]})

    assert response.status_code == 200
    body = response.json()
    assert body["model_id"] == EXPECTED_MODEL_ID
    assert body["model_revision"] == EXPECTED_MODEL_REVISION
    assert body["dimensions"] == EXPECTED_DIMENSIONS
    assert len(body["vectors"]) == 1
    vector = body["vectors"][0]
    assert len(vector) == EXPECTED_DIMENSIONS
    assert all(isinstance(component, float) for component in vector)


async def test_embed_returns_one_vector_per_input_text_in_order(
    sidecar: httpx.AsyncClient,
) -> None:
    texts = ["the first sentence", "a second, different sentence"]

    response = await sidecar.post("/embed", json={"texts": texts})

    assert response.status_code == 200
    vectors = response.json()["vectors"]
    assert len(vectors) == len(texts)
    # Different input text must not collapse to an identical vector.
    assert vectors[0] != vectors[1]


async def test_embed_is_deterministic_for_the_same_text(sidecar: httpx.AsyncClient) -> None:
    text = "determinism check: this exact sentence should embed the same way twice"

    first = await sidecar.post("/embed", json={"texts": [text]})
    second = await sidecar.post("/embed", json={"texts": [text]})

    assert first.json()["vectors"] == second.json()["vectors"]


async def test_embed_with_an_empty_batch_returns_no_vectors(sidecar: httpx.AsyncClient) -> None:
    response = await sidecar.post("/embed", json={"texts": []})

    assert response.status_code == 200
    assert response.json()["vectors"] == []


async def test_embed_rejects_a_malformed_request_body(sidecar: httpx.AsyncClient) -> None:
    response = await sidecar.post("/embed", json={"texts": "not-a-list"})

    assert response.status_code == 422


async def test_embed_rejects_a_batch_over_the_configured_limit(sidecar: httpx.AsyncClient) -> None:
    """Proves the real deployed process enforces this bound, not only the
    fake-model unit tests - a request this large would otherwise tie up the
    single-process CPU-bound encoder for an unbounded amount of work."""
    response = await sidecar.post("/embed", json={"texts": ["x"] * (MAX_BATCH_SIZE + 1)})

    assert response.status_code == 422


async def test_embed_rejects_a_text_over_the_configured_length_limit(
    sidecar: httpx.AsyncClient,
) -> None:
    over_limit_text = _text_of_byte_length(MAX_TEXT_BYTES + 1)

    response = await sidecar.post("/embed", json={"texts": [over_limit_text]})

    assert response.status_code == 422


async def test_embed_accepts_a_batch_at_exactly_the_configured_limits(
    sidecar: httpx.AsyncClient,
) -> None:
    """A batch of exactly `MAX_BATCH_SIZE` texts, each exactly `MAX_TEXT_BYTES`
    UTF-8 bytes, is the largest legal request - it must succeed, not be
    wrongly rejected the way a byte-vs-character unit mismatch between the
    per-text limit and the request-body byte cap previously could (a
    max-size batch of single-byte ASCII text is legal, but the same batch
    built from multibyte text encodes to more bytes for the same character
    count, which the two checks must still agree is within budget)."""
    max_size_text = _text_of_byte_length(MAX_TEXT_BYTES)

    response = await sidecar.post("/embed", json={"texts": [max_size_text] * MAX_BATCH_SIZE})

    assert response.status_code == 200
    assert len(response.json()["vectors"]) == MAX_BATCH_SIZE


async def test_embed_accepts_a_batch_at_exactly_the_configured_limits_with_multibyte_text(
    sidecar: httpx.AsyncClient,
) -> None:
    """Same boundary as above, but built from a 2-byte UTF-8 character
    ("é") rather than single-byte ASCII - the case this fix's floor
    arithmetic (budgeting for worst-case JSON escaping) exists for, since a
    naive character-count check would have let this through at half the
    real byte size."""
    max_size_multibyte_text = _text_of_byte_length(MAX_TEXT_BYTES, char="é")

    response = await sidecar.post(
        "/embed", json={"texts": [max_size_multibyte_text] * MAX_BATCH_SIZE}
    )

    assert response.status_code == 200
    assert len(response.json()["vectors"]) == MAX_BATCH_SIZE


async def test_embed_rejects_an_oversized_body_before_parsing_it(
    sidecar: httpx.AsyncClient,
) -> None:
    """A body over the byte limit must be rejected by `MaxBodySizeMiddleware`
    (413) against the real deployed process, not only the fake-model unit
    tests - proving the real ASGI middleware stack actually intercepts it
    rather than relying on FastAPI's own body parsing to fail first."""
    response = await sidecar.post("/embed", json={"texts": ["x" * (MAX_REQUEST_BYTES + 1)]})

    assert response.status_code == 413


async def test_concurrent_slow_requests_are_bounded_not_unlimited(
    sidecar: httpx.AsyncClient,
) -> None:
    """Fires more concurrent, slowly-streamed requests than the container's
    configured `limit_concurrency` allows, each individually well within
    every per-request bound (batch size, text length, byte size) proven
    above. Those per-request checks only ever bounded one request at a
    time; this proves *some* transport-level mechanism actually engages
    once too much arrives at once - uvicorn's own 503 above
    `limit_concurrency`, the model's 429 admission cap, or the body-read
    timeout's 408 - rather than everything being silently accepted with
    no ceiling at all."""
    request_count = LIMIT_CONCURRENCY + 20

    async def slow_body(index: int) -> AsyncIterator[bytes]:
        payload = json.dumps({"texts": [f"concurrency probe {index}"]}).encode()
        midpoint = len(payload) // 2
        yield payload[:midpoint]
        await asyncio.sleep(0.3)
        yield payload[midpoint:]

    async def slow_post(index: int) -> httpx.Response | httpx.HTTPError:
        try:
            return await sidecar.post(
                "/embed",
                content=slow_body(index),
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            # A connection reset while the server was over capacity is
            # itself a form of "not silently accepted," not a test bug.
            return exc

    responses = await asyncio.gather(*(slow_post(i) for i in range(request_count)))

    statuses = [
        response.status_code if isinstance(response, httpx.Response) else "connection-error"
        for response in responses
    ]
    rejected = [status for status in statuses if status != 200]
    assert rejected, f"expected at least one rejected/bounded response among {statuses}"


def _open_stalled_connection(host: str, port: int) -> socket.socket:
    """A raw TCP connection sending a request line and partial headers,
    deliberately never terminated with the blank line HTTP requires - h11
    waits indefinitely for the rest, exactly like a real slowloris
    connection. httpx cannot construct a request this malformed on
    purpose, so this bypasses it entirely."""
    sock = socket.create_connection((host, port), timeout=5)
    sock.sendall(b"POST /embed HTTP/1.1\r\nHost: sidecar\r\nContent-Type: application/json\r\n")
    return sock


async def test_stalled_incomplete_header_connections_cause_other_requests_to_be_bounced(
    sidecar: httpx.AsyncClient,
) -> None:
    """What this proves, precisely: opening more than `limit_concurrency`
    connections that each send incomplete headers and never finish causes
    uvicorn to answer an *unrelated, complete* request (e.g. this test's own
    `/health` check) with 503, because uvicorn counts a connection in its
    concurrency-limited set from the moment it is accepted
    (`connection_made`), not from when its request completes.

    What this does **not** prove, and an earlier version of this test's own
    name/docstring wrongly claimed it did: that the stalled connections
    themselves are capped, rejected, or evicted in any way. They are not -
    confirmed directly against a real container (a separate, larger probe
    opened 300 simultaneous stalled connections; every one was accepted,
    none was ever refused or closed) and by reading uvicorn's h11 protocol
    implementation, which unconditionally accepts and tracks every new TCP
    connection with no check against `limit_concurrency` at accept time at
    all - that check only runs when some *other* connection completes a
    request. Uvicorn also has no time-based header-read timeout at all, so
    a connection sending a few bytes of valid-so-far header data every few
    seconds, forever, is never evicted by time either.

    Net effect this test actually demonstrates: a real, verified symptom
    (other traffic gets 503) - not a general containment guarantee. The
    attacker's own connections accumulate up to whatever the host's file-
    descriptor/memory limits allow, not a number this service controls.
    Closing that gap needs a reverse proxy or a custom accept-time
    gatekeeper in front of uvicorn - deliberately out of scope for this
    loopback- and Docker-internal-network-only slice; see `settings.py`'s
    `limit_concurrency`/`h11_max_incomplete_event_size` docstrings for the
    full reasoning and the trigger to revisit."""
    assert sidecar.base_url.host is not None
    assert sidecar.base_url.port is not None

    stalled_sockets = [
        _open_stalled_connection(sidecar.base_url.host, sidecar.base_url.port)
        for _ in range(LIMIT_CONCURRENCY + 10)
    ]
    try:
        # Poll briefly rather than a single fixed-delay check - uvicorn
        # registers each accepted connection near-instantly, but a loaded
        # CI runner may take a moment longer than a local machine would.
        for _ in range(20):
            response = await sidecar.get("/health")
            if response.status_code == 503:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail(
                "expected /health to be answered 503 once limit_concurrency "
                "was exhausted by stalled connections, but it never was"
            )
    finally:
        for sock in stalled_sockets:
            sock.close()

"""The sidecar's real HTTP contract, against a real running container
(docs/adr/0010 §5, `docs/DESIGN.md` 8.5's "contract tests for the sidecar
cover its `embed` endpoint's request/response shape and failure modes").
"""

from __future__ import annotations

import asyncio
import json
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
# Python-3.12-only settings module directly.
MAX_BATCH_SIZE = 64
MAX_TEXT_LENGTH = 50_000
MAX_REQUEST_BYTES = 4_000_000
LIMIT_CONCURRENCY = 32


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
    response = await sidecar.post("/embed", json={"texts": ["x" * (MAX_TEXT_LENGTH + 1)]})

    assert response.status_code == 422


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

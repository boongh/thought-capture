"""Transport-level tests for the sidecar's HTTP contract, against a fake model."""

from __future__ import annotations

from httpx import AsyncClient

from tc_embedding_sidecar.app import MODEL_NOT_READY_DETAIL
from tc_embedding_sidecar.settings import get_settings
from tests.conftest import FAKE_DIMENSIONS, FAKE_MODEL_ID, FAKE_MODEL_REVISION


async def test_health_reports_ready_model(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ok",
        "model_id": FAKE_MODEL_ID,
        "model_revision": FAKE_MODEL_REVISION,
        "dimensions": FAKE_DIMENSIONS,
    }


async def test_health_returns_503_before_the_model_is_ready(not_ready_client: AsyncClient) -> None:
    response = await not_ready_client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"] == MODEL_NOT_READY_DETAIL


async def test_embed_returns_one_vector_per_text_in_order(client: AsyncClient) -> None:
    response = await client.post("/embed", json={"texts": ["a", "bb", "ccc"]})

    assert response.status_code == 200
    body = response.json()
    assert body["model_id"] == FAKE_MODEL_ID
    assert body["model_revision"] == FAKE_MODEL_REVISION
    assert body["dimensions"] == FAKE_DIMENSIONS
    assert len(body["vectors"]) == 3
    assert all(len(vector) == FAKE_DIMENSIONS for vector in body["vectors"])


async def test_embed_with_empty_batch_returns_empty_vectors(client: AsyncClient) -> None:
    response = await client.post("/embed", json={"texts": []})

    assert response.status_code == 200
    assert response.json()["vectors"] == []


async def test_embed_returns_503_before_the_model_is_ready(not_ready_client: AsyncClient) -> None:
    response = await not_ready_client.post("/embed", json={"texts": ["hello"]})

    assert response.status_code == 503
    assert response.json()["detail"] == MODEL_NOT_READY_DETAIL


# ---------------------------------------------------------------------------
# Request bounds (settings.py: batch size and per-text length), guarding
# against unbounded CPU-bound work per request.
# ---------------------------------------------------------------------------


async def test_embed_rejects_a_batch_over_the_configured_limit(client: AsyncClient) -> None:
    limit = get_settings().max_batch_size

    response = await client.post("/embed", json={"texts": ["x"] * (limit + 1)})

    assert response.status_code == 422


async def test_embed_accepts_a_batch_at_exactly_the_configured_limit(client: AsyncClient) -> None:
    limit = get_settings().max_batch_size

    response = await client.post("/embed", json={"texts": ["x"] * limit})

    assert response.status_code == 200
    assert len(response.json()["vectors"]) == limit


async def test_embed_rejects_a_text_over_the_configured_length_limit(client: AsyncClient) -> None:
    limit = get_settings().max_text_length

    response = await client.post("/embed", json={"texts": ["x" * (limit + 1)]})

    assert response.status_code == 422


async def test_embed_accepts_a_text_at_exactly_the_configured_length_limit(
    client: AsyncClient,
) -> None:
    limit = get_settings().max_text_length

    response = await client.post("/embed", json={"texts": ["x" * limit]})

    assert response.status_code == 200


async def test_embed_rejects_an_oversized_body_before_parsing_it(client: AsyncClient) -> None:
    """A body over `max_request_bytes` must be rejected (413) by
    `MaxBodySizeMiddleware`, distinct from the 422 the batch/length checks
    below would give it - proving the rejection happens before FastAPI ever
    parses the body, not just that the parsed value later failed a field
    check. One text well over both the byte cap and (incidentally) the
    per-text length cap: if the 422 path fired instead, that would mean the
    middleware did not intercept it first."""
    byte_limit = get_settings().max_request_bytes

    response = await client.post("/embed", json={"texts": ["x" * (byte_limit + 1)]})

    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Admission bound (settings.py: max_concurrent_encodes + max_queued_encodes) -
# `model.TooManyRequestsError` must surface as 429, not a 500 or a hang.
# ---------------------------------------------------------------------------


async def test_embed_returns_429_when_the_model_is_at_capacity(busy_client: AsyncClient) -> None:
    response = await busy_client.post("/embed", json={"texts": ["hello"]})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"

"""Transport-level tests for the sidecar's HTTP contract, against a fake model."""

from __future__ import annotations

from httpx import AsyncClient

from tc_embedding_sidecar.app import MODEL_NOT_READY_DETAIL
from tests.conftest import FAKE_DIMENSIONS, FAKE_MODEL_ID


async def test_health_reports_ready_model(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "model_id": FAKE_MODEL_ID, "dimensions": FAKE_DIMENSIONS}


async def test_health_returns_503_before_the_model_is_ready(not_ready_client: AsyncClient) -> None:
    response = await not_ready_client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"] == MODEL_NOT_READY_DETAIL


async def test_embed_returns_one_vector_per_text_in_order(client: AsyncClient) -> None:
    response = await client.post("/embed", json={"texts": ["a", "bb", "ccc"]})

    assert response.status_code == 200
    body = response.json()
    assert body["model_id"] == FAKE_MODEL_ID
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

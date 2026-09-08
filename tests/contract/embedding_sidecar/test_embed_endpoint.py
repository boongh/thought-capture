"""The sidecar's real HTTP contract, against a real running container
(docs/adr/0010 §5, `docs/DESIGN.md` 8.5's "contract tests for the sidecar
cover its `embed` endpoint's request/response shape and failure modes").
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.contract

EXPECTED_MODEL_ID = "thenlper/gte-small"
EXPECTED_DIMENSIONS = 384


async def test_health_reports_the_ready_model(sidecar: httpx.AsyncClient) -> None:
    response = await sidecar.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model_id"] == EXPECTED_MODEL_ID
    assert body["dimensions"] == EXPECTED_DIMENSIONS


async def test_embed_returns_a_vector_of_the_expected_dimensionality(
    sidecar: httpx.AsyncClient,
) -> None:
    response = await sidecar.post("/embed", json={"texts": ["a contract test sentence"]})

    assert response.status_code == 200
    body = response.json()
    assert body["model_id"] == EXPECTED_MODEL_ID
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

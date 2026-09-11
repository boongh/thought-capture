"""``HttpEmbeddingClient`` - no live sidecar needed (docs/adr/0010 §4-5).

Mirrors ``tests/unit/test_khoj_client_errors.py``'s shape: an unreachable
port for the transport-error path, ``httpx.MockTransport`` for asserting
exact responses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from tc_domain.embedding_ports import EmbeddingUnavailableError
from tc_infrastructure.embedding.client import HttpEmbeddingClient, compose_embedding_model_id

# Port 1 is a privileged, essentially-never-listening port: connecting to it
# reliably fails fast without depending on a specific unreachable-host DNS/
# firewall behavior (matches test_khoj_client_errors.py).
UNREACHABLE_URL = "http://127.0.0.1:1"

DIMENSIONS = 384


def _client_with(handler: object) -> tuple[httpx.AsyncClient, HttpEmbeddingClient]:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http = httpx.AsyncClient(transport=transport)
    return http, HttpEmbeddingClient(http, "http://embedding-sidecar.invalid", timeout=5.0)


def _vector(value: float = 0.1) -> list[float]:
    return [value] * DIMENSIONS


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def client(http: httpx.AsyncClient) -> HttpEmbeddingClient:
    return HttpEmbeddingClient(http, UNREACHABLE_URL, timeout=1.0)


async def test_embed_is_a_no_op_for_an_empty_tuple(client: HttpEmbeddingClient) -> None:
    """Never even attempts the unreachable connection - a genuine no-op,
    not a raised-and-ignored error (EmbeddingPort.embed's own contract)."""
    assert await client.embed(()) == ()


async def test_embed_raises_when_unreachable(client: HttpEmbeddingClient) -> None:
    with pytest.raises(EmbeddingUnavailableError):
        await client.embed(("hello",))


async def test_embed_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/embed"
        return httpx.Response(
            200,
            json={
                "model_id": "sentence-transformers/all-MiniLM-L6-v2",
                "model_revision": "abc123",
                "dimensions": DIMENSIONS,
                "vectors": [_vector(0.1), _vector(0.2)],
            },
        )

    http, client = _client_with(handler)
    async with http:
        vectors = await client.embed(("first", "second"))

    assert len(vectors) == 2
    assert vectors[0].model_id == "sentence-transformers/all-MiniLM-L6-v2@abc123"
    assert vectors[0].dimensions == DIMENSIONS
    assert vectors[0].values == tuple(_vector(0.1))
    assert vectors[1].values == tuple(_vector(0.2))


async def test_embed_reads_truncated_flags_in_order() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model_id": "sentence-transformers/all-MiniLM-L6-v2",
                "model_revision": "abc123",
                "dimensions": DIMENSIONS,
                "vectors": [_vector(0.1), _vector(0.2)],
                "truncated": [True, False],
            },
        )

    http, client = _client_with(handler)
    async with http:
        vectors = await client.embed(("first", "second"))

    assert vectors[0].truncated is True
    assert vectors[1].truncated is False


async def test_embed_defaults_truncated_to_false_when_key_absent() -> None:
    """An older sidecar that predates the ``truncated`` field omits the key
    entirely - this must not raise, and every vector must default to
    untruncated (the schema addition is genuinely additive)."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model_id": "sentence-transformers/all-MiniLM-L6-v2",
                "model_revision": "abc123",
                "dimensions": DIMENSIONS,
                "vectors": [_vector(0.1), _vector(0.2)],
            },
        )

    http, client = _client_with(handler)
    async with http:
        vectors = await client.embed(("first", "second"))

    assert vectors[0].truncated is False
    assert vectors[1].truncated is False


async def test_embed_raises_when_truncated_length_mismatches_vector_count() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model_id": "sentence-transformers/all-MiniLM-L6-v2",
                "model_revision": "abc123",
                "dimensions": DIMENSIONS,
                "vectors": [_vector(0.1), _vector(0.2)],
                "truncated": [True],
            },
        )

    http, client = _client_with(handler)
    async with http:
        with pytest.raises(EmbeddingUnavailableError):
            await client.embed(("first", "second"))


@pytest.mark.parametrize("status_code", [500, 503, 429])
async def test_embed_raises_on_non_2xx_status(status_code: int) -> None:
    """503 (model still loading) and 429 (at capacity) are exactly as
    unavailable to a caller as any other non-2xx status."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "unavailable"})

    http, client = _client_with(handler)
    async with http:
        with pytest.raises(EmbeddingUnavailableError):
            await client.embed(("hello",))


async def test_embed_raises_on_timeout() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    http, client = _client_with(handler)
    async with http:
        with pytest.raises(EmbeddingUnavailableError):
            await client.embed(("hello",))


async def test_embed_raises_on_malformed_json() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    http, client = _client_with(handler)
    async with http:
        with pytest.raises(EmbeddingUnavailableError):
            await client.embed(("hello",))


@pytest.mark.parametrize(
    "payload",
    [
        # missing "vectors" key
        {"model_id": "m", "model_revision": "r", "dimensions": DIMENSIONS},
        # vector count does not match input text count
        {
            "model_id": "m",
            "model_revision": "r",
            "dimensions": DIMENSIONS,
            "vectors": [[0.1] * DIMENSIONS],
        },
        # dimensions does not match the expected 384
        {
            "model_id": "m",
            "model_revision": "r",
            "dimensions": 128,
            "vectors": [[0.1] * 128, [0.1] * 128],
        },
        # a vector's own length disagrees with the declared dimensions
        {
            "model_id": "m",
            "model_revision": "r",
            "dimensions": DIMENSIONS,
            "vectors": [[0.1] * DIMENSIONS, [0.1] * 10],
        },
        # not a JSON object at all
        [1, 2, 3],
    ],
    ids=[
        "missing_vectors",
        "vector_count_mismatch",
        "dimension_mismatch",
        "per_vector_length_mismatch",
        "non_object_payload",
    ],
)
async def test_embed_raises_on_unexpected_shape(payload: object) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    http, client = _client_with(handler)
    async with http:
        with pytest.raises(EmbeddingUnavailableError):
            await client.embed(("first", "second"))


async def test_embed_passes_the_configured_timeout_to_the_request(
    http: httpx.AsyncClient,
) -> None:
    """The ``timeout`` constructor argument must actually reach the
    outbound request, not just be stored and ignored."""
    captured: dict[str, object] = {}
    original_post = http.post

    async def _capturing_post(*args: object, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        return await original_post(*args, **kwargs)  # type: ignore[arg-type]

    http.post = _capturing_post  # type: ignore[method-assign]
    client = HttpEmbeddingClient(http, UNREACHABLE_URL, timeout=12.5)

    with pytest.raises(EmbeddingUnavailableError):
        await client.embed(("hello",))

    assert captured.get("timeout") == 12.5


def test_compose_embedding_model_id_joins_model_id_and_revision() -> None:
    assert compose_embedding_model_id("sentence-transformers/all-MiniLM-L6-v2", "abc123") == (
        "sentence-transformers/all-MiniLM-L6-v2@abc123"
    )

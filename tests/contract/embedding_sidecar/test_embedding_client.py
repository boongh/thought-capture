"""`HttpEmbeddingClient` (the first-party adapter, not raw HTTP - that is
`test_embed_endpoint.py`'s job) against the real running sidecar container
(docs/adr/0010 §5, docs/plans/khoj-retirement-completion.md T2.7).

Reuses `conftest.py`'s `sidecar` fixture and `EMBEDDING_SIDECAR_BASE_URL`
constant rather than inventing a second base-URL/gating mechanism. Like
`test_embed_endpoint.py`, an unreachable sidecar is deliberately allowed to
propagate as a connection error rather than a skip - `conftest.py`'s
`pytest_sessionfinish` hook is what turns a merely-skipped contract suite
into a failed run when `TC_REQUIRE_CONTRACT=1`.
"""

from __future__ import annotations

import httpx
import pytest

from tc_domain.embedding_ports import EmbeddingUnavailableError
from tc_infrastructure.db.tables import EMBEDDING_DIMENSIONS
from tc_infrastructure.embedding.client import HttpEmbeddingClient

from . import conftest

pytestmark = pytest.mark.contract

# Must match apps/embedding_sidecar/src/tc_embedding_sidecar/settings.py's own
# defaults - duplicated here because the contract tests run in this project's
# own Python 3.14 environment and cannot import the sidecar's Python-3.12-only
# settings module directly (test_embed_endpoint.py does the same).
MAX_BATCH_SIZE = 64
MAX_TEXT_LENGTH = 50_000


@pytest.fixture
def embedding_client(sidecar: httpx.AsyncClient) -> HttpEmbeddingClient:
    """Wraps `conftest.py`'s raw `sidecar` client in the adapter under test.

    `sidecar` already points at `EMBEDDING_SIDECAR_BASE_URL` with a 30s
    timeout; `HttpEmbeddingClient` takes that same `httpx.AsyncClient` plus
    a base URL of its own to build request paths against, so `base_url` here
    is deliberately the same constant, not a second, independent value.
    """
    return HttpEmbeddingClient(sidecar, conftest.EMBEDDING_SIDECAR_BASE_URL, timeout=30.0)


async def test_embed_returns_the_right_count_and_dimensionality(
    embedding_client: HttpEmbeddingClient,
) -> None:
    texts = ("the first sentence", "a second, different sentence", "a third one")

    vectors = await embedding_client.embed(texts)

    assert len(vectors) == len(texts)
    for vector in vectors:
        assert vector.dimensions == EMBEDDING_DIMENSIONS
        assert len(vector.values) == EMBEDDING_DIMENSIONS


async def test_an_over_limit_batch_surfaces_as_embedding_unavailable(
    embedding_client: HttpEmbeddingClient,
) -> None:
    """The sidecar rejects this with 422 (test_embed_endpoint.py proves the
    raw HTTP contract); this proves the adapter's own error-mapping turns
    that into `EmbeddingUnavailableError`, never a raw `httpx.HTTPStatusError`
    a caller's `except EmbeddingUnavailableError` would miss."""
    texts = tuple(["x"] * (MAX_BATCH_SIZE + 1))

    with pytest.raises(EmbeddingUnavailableError):
        await embedding_client.embed(texts)


async def test_an_over_length_text_surfaces_as_embedding_unavailable(
    embedding_client: HttpEmbeddingClient,
) -> None:
    texts = ("x" * (MAX_TEXT_LENGTH + 1),)

    with pytest.raises(EmbeddingUnavailableError):
        await embedding_client.embed(texts)


async def test_embed_is_deterministic_across_two_separate_calls(
    embedding_client: HttpEmbeddingClient,
) -> None:
    text = "determinism check: this exact sentence should embed the same way twice"

    first = await embedding_client.embed((text,))
    second = await embedding_client.embed((text,))

    # sentence-transformers runs in eval mode (no dropout), so identical
    # input is expected to produce bit-for-bit identical output, not merely
    # a close one - exact equality is the correct assertion here, mirroring
    # test_embed_endpoint.py's own determinism test against the raw endpoint.
    assert first[0].values == second[0].values
    assert first[0].model_id == second[0].model_id

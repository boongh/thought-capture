"""Transport-level tests for the sidecar's HTTP contract, against a fake model."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from tc_embedding_sidecar.app import MODEL_NOT_READY_DETAIL, create_app
from tc_embedding_sidecar.settings import get_settings
from tests.conftest import (
    FAKE_DIMENSIONS,
    FAKE_MODEL_ID,
    FAKE_MODEL_REVISION,
    FakeEmbeddingModel,
    _lifespan_with,
)


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


async def test_embed_rejects_a_text_over_the_configured_byte_limit(client: AsyncClient) -> None:
    """ASCII filler: 1 char == 1 byte, so plain `str` length doubles as a
    byte count here, but the limit itself is `max_text_bytes` (UTF-8 bytes),
    not a character count - see the boundary tests below for multi-byte
    filler characters."""
    limit = get_settings().max_text_bytes

    response = await client.post("/embed", json={"texts": ["x" * (limit + 1)]})

    assert response.status_code == 422


async def test_embed_accepts_a_text_at_exactly_the_configured_byte_limit(
    client: AsyncClient,
) -> None:
    limit = get_settings().max_text_bytes

    response = await client.post("/embed", json={"texts": ["x" * limit]})

    assert response.status_code == 200


def _text_of_exact_byte_length(fill_char: str, byte_length: int) -> str:
    """Build a string whose UTF-8 encoding is exactly `byte_length` bytes.

    Multi-byte filler characters (e.g. 2-byte accented letters, 4-byte
    emoji) do not divide `byte_length` evenly in general, so the bulk of the
    string is whole copies of `fill_char` and any remainder is padded with a
    1-byte ASCII filler to land on the exact byte count.
    """
    char_bytes = len(fill_char.encode("utf-8"))
    whole_copies = byte_length // char_bytes
    text = fill_char * whole_copies
    remainder = byte_length - len(text.encode("utf-8"))
    text += "x" * remainder
    assert len(text.encode("utf-8")) == byte_length
    return text


@pytest.mark.parametrize(
    "fill_char",
    [
        pytest.param("x", id="ascii-1-byte"),
        pytest.param("é", id="latin-2-byte"),  # e.g. "e" with acute accent
        pytest.param("\U0001f600", id="emoji-4-byte"),
        pytest.param("\x00", id="control-char-worst-case-json-expansion"),
    ],
)
async def test_embed_accepts_a_request_at_exactly_the_advertised_limits(
    fill_char: str,
) -> None:
    """The boundary this fix exists to prove: a request built to exactly
    `max_batch_size` texts of exactly `max_text_bytes` UTF-8 bytes each must
    be a 200, not a 413 - through the *full* app (`create_app()`, via
    `ASGITransport`) so `MaxBodySizeMiddleware` is actually in the request
    path, not just the route handler's own field checks. A control-character
    filler is the worst case: it is 1 raw UTF-8 byte but expands to 6 bytes
    in the JSON body (`\\u0000`), so it stresses `max_request_bytes`
    hardest even though it never gets close to `max_text_bytes` itself as a
    raw byte count.
    """
    settings = get_settings()
    text = _text_of_exact_byte_length(fill_char, settings.max_text_bytes)
    app = create_app(
        lifespan_handler=_lifespan_with(FakeEmbeddingModel(ready=True)),
    )
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        response = await http.post("/embed", json={"texts": [text] * settings.max_batch_size})

    assert response.status_code == 200
    assert len(response.json()["vectors"]) == settings.max_batch_size


@pytest.mark.parametrize(
    "fill_char",
    [
        pytest.param("é", id="latin-2-byte"),
        pytest.param("\U0001f600", id="emoji-4-byte"),
    ],
)
async def test_embed_rejects_multibyte_text_one_byte_over_the_byte_limit(
    fill_char: str, client: AsyncClient
) -> None:
    """Proves the check is genuinely byte-based, not character-based, on the
    *reject* side too - the ASCII-only rejection tests above can't tell the
    two apart, since for ASCII text 1 character is always 1 byte. Here the
    text's *character* count is far under `max_text_bytes` (a character-based
    check would wrongly accept it), but its UTF-8 *byte* count is exactly one
    byte over the limit, so only a byte-based check correctly rejects it."""
    limit = get_settings().max_text_bytes
    text = _text_of_exact_byte_length(fill_char, limit + 1)
    assert len(text) < limit

    response = await client.post("/embed", json={"texts": [text]})

    assert response.status_code == 422


async def test_embed_rejects_a_single_text_one_byte_over_the_limit_via_its_own_check(
    client: AsyncClient,
) -> None:
    """A single over-limit text, alone in its batch, cannot itself trip
    `MaxBodySizeMiddleware`'s `max_request_bytes` cap at default config (that
    cap is sized for a full `max_batch_size`-text request, not one text) - so
    a 422 here proves the endpoint's own `max_text_bytes` check fired, not
    the middleware's 413."""
    limit = get_settings().max_text_bytes

    response = await client.post("/embed", json={"texts": ["x" * (limit + 1)]})

    assert response.status_code == 422


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

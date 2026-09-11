"""HTTP client for the self-hosted embedding sidecar, implementing
``EmbeddingPort`` (docs/adr/0010 §4-5).

Talks to the sidecar's real contract (`apps/embedding_sidecar/src/
tc_embedding_sidecar/schemas.py`): ``POST /embed`` with
``{"texts": [...]}``, returning ``{"model_id", "model_revision",
"dimensions", "vectors"}``. Every failure mode - unreachable sidecar,
timeout, a non-2xx status (including 503 "still loading" and 429 "at
capacity"), a malformed/missing JSON body, or a response shape that does
not match what was asked for - is mapped to ``EmbeddingUnavailableError``,
mirroring ``HttpKhojClient``'s degrade-explicit contract
(docs/DESIGN.md 7.5): the caller never has to distinguish "sidecar down"
from "sidecar returned nonsense".
"""

from __future__ import annotations

import logging

import httpx

from tc_domain.embedding_ports import EmbeddingUnavailableError, EmbeddingVector
from tc_infrastructure.db.tables import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)


def compose_embedding_model_id(model_id: str, model_revision: str) -> str:
    """The one, single place ``model_id``/``model_revision`` are combined
    into the identity string stored as ``EmbeddingVector.model_id`` /
    ``document_embeddings.embedding_model_id``.

    The sidecar's own ``EmbedResponse.model_revision`` docstring explicitly
    defers this composition to "a future writer" - this is that writer, and
    the sync writer (a later, separately-owned task) must reuse this
    function rather than reimplementing the join, so the two never drift.
    """
    return f"{model_id}@{model_revision}"


class HttpEmbeddingClient:
    """Implements ``tc_domain.embedding_ports.EmbeddingPort`` structurally
    (a ``runtime_checkable`` ``Protocol`` - no base class to inherit,
    matching ``HttpKhojClient``'s equivalent duck-typed shape)."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, *, timeout: float) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def embed(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]:
        if not texts:
            return ()

        try:
            response = await self._http.post(
                f"{self._base_url}/embed",
                json={"texts": list(texts)},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Covers both unreachable/timed-out (httpx.TransportError,
            # including httpx.TimeoutException) and non-2xx statuses
            # (httpx.HTTPStatusError from raise_for_status) - the 503
            # "model still loading" and 429 "at capacity" sidecar responses
            # both land here, same as any other unavailability.
            logger.warning(
                "embedding_client.request_failed",
                extra={"error_class": type(exc).__name__},
            )
            raise EmbeddingUnavailableError("embedding sidecar request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning(
                "embedding_client.malformed_response",
                extra={"error_class": type(exc).__name__},
            )
            raise EmbeddingUnavailableError(
                "embedding sidecar returned a non-JSON response"
            ) from exc

        try:
            return self._parse_response(payload, expected_count=len(texts))
        except (KeyError, TypeError, ValueError) as exc:
            # A shape the sidecar's own contract would never produce is
            # exactly as unusable to a caller as an unreachable sidecar
            # (docs/DESIGN.md 7.5) - never let this surface as a bare
            # KeyError/TypeError a caller's `except EmbeddingUnavailableError`
            # would miss.
            logger.warning(
                "embedding_client.unexpected_shape",
                extra={"error_class": type(exc).__name__},
            )
            raise EmbeddingUnavailableError(
                f"embedding sidecar returned an unexpected shape: {exc}"
            ) from exc

    def _parse_response(
        self, payload: object, *, expected_count: int
    ) -> tuple[EmbeddingVector, ...]:
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")

        model_id = payload["model_id"]
        model_revision = payload["model_revision"]
        dimensions = payload["dimensions"]
        vectors = payload["vectors"]

        if not isinstance(model_id, str) or not isinstance(model_revision, str):
            raise ValueError("model_id/model_revision were not strings")
        if not isinstance(dimensions, int):
            raise ValueError("dimensions was not an integer")
        if dimensions != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"expected {EMBEDDING_DIMENSIONS}-dimensional vectors, got {dimensions}"
            )
        if not isinstance(vectors, list):
            raise ValueError(f"expected vectors to be a list, got {type(vectors).__name__}")
        if len(vectors) != expected_count:
            raise ValueError(f"expected {expected_count} vector(s), got {len(vectors)}")

        combined_model_id = compose_embedding_model_id(model_id, model_revision)
        result = []
        for vector in vectors:
            if not isinstance(vector, (list, tuple)):
                raise ValueError(f"expected a vector, got {type(vector).__name__}")
            values = tuple(float(v) for v in vector)
            if len(values) != dimensions:
                raise ValueError(f"expected a {dimensions}-dimensional vector, got {len(values)}")
            result.append(
                EmbeddingVector(values=values, model_id=combined_model_id, dimensions=dimensions)
            )
        return tuple(result)

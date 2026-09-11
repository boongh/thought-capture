"""Ports for the self-hosted embedding sync/search pipeline
(docs/adr/0010-self-hosted-embedding-search-ask.md, docs/DESIGN.md 8.4).

Mirrors ``tc_domain.khoj_sync_ports``/``tc_domain.khoj_ports`` deliberately:
delivering an embedding to pgvector is the same shape of problem as
delivering an index update to Khoj was - a durable outbox event that must be
turned into an external side effect, at-least-once, with the canonical write
already committed before either consumer ever runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """One text's embedding, plus the identity of the model that produced it.

    ADR-0010 4 sketches this as a pydantic ``BaseModel``; that is
    illustrative pseudocode, not the accepted shape. The domain layer is
    stdlib-only (no pydantic, no sqlalchemy, no httpx), matching every other
    domain value type here (``SearchResult``, ``LLMResponse``, and this
    module's own sibling ports) - so this is a frozen dataclass instead, and
    that is a deliberate deviation from the ADR's code sketch, not an
    oversight.
    """

    values: tuple[float, ...]
    model_id: str
    dimensions: int
    truncated: bool = False
    """``True`` when the embedding model's tokenizer truncated this text
    before encoding, so ``values`` represents only the opening portion of the
    input, not the whole document (docs/plans/khoj-retirement-completion.md
    Decision D). Defaults to ``False`` so existing callers/fixtures that
    construct this type without the field are unaffected."""


@runtime_checkable
class EmbeddingPort(Protocol):
    async def embed(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]:
        """Embed each text, one vector per input, in the same order.

        Returns ``()`` for an empty input tuple without erroring - there is
        nothing to embed, not a failure.
        """
        ...


class EmbeddingUnavailableError(Exception):
    """The embedding sidecar could not be reached, or returned an unexpected
    response.

    Mirrors ``KhojUnavailableError``'s role: docs/DESIGN.md 7.5's
    degrade-explicit contract applies identically here - the search/Ask
    layer catches this and sets ``degraded=true``, it is never swallowed
    silently.
    """


@dataclass(frozen=True, slots=True)
class PendingEmbeddingSync:
    """One claimed ``embedding.sync_requested`` event.

    Carries ``document_id`` only, never a ``revision_id``: the consumer must
    always re-read the document's *current* revision at delivery time
    (``EmbeddingSource.get_revision``), not trust a possibly-superseded id
    from an older queued event - two organize runs on the same document
    close together would otherwise risk the later-delivered but
    earlier-queued event overwriting the stored embedding with stale
    content (same reasoning as ``PendingKhojSync``).
    """

    event_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    attempts: int


@runtime_checkable
class EmbeddingSyncOutbox(Protocol):
    async def claim(self, limit: int) -> tuple[PendingEmbeddingSync, ...]:
        """Lease up to ``limit`` due ``embedding.sync_requested`` events.

        A malformed payload is failed immediately by the implementation
        rather than being returned, since it will never parse successfully
        on retry either.
        """
        ...

    async def mark_delivered(self, event_id: uuid.UUID) -> None: ...

    async def mark_failed(self, event_id: uuid.UUID, *, attempts: int, error: str) -> None:
        """Reschedule with backoff. ``error`` must not contain personal content."""
        ...


@dataclass(frozen=True, slots=True)
class RevisionForEmbedding:
    """The current revision content needed to compute an embedding."""

    document_id: uuid.UUID
    revision_id: uuid.UUID
    revision_number: int
    title: str
    body_markdown: str


@runtime_checkable
class EmbeddingSource(Protocol):
    async def get_revision(
        self, *, workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> RevisionForEmbedding:
        """Raise ``EmbeddingSourceNotFound`` unless the document resolves and
        belongs to this workspace - never on ``document_id`` alone."""
        ...


@runtime_checkable
class EmbeddingWriter(Protocol):
    async def upsert(
        self,
        *,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        revision_number: int,
        vector: EmbeddingVector,
    ) -> bool:
        """Store ``vector`` for ``revision_id``, returning whether the row was
        actually written.

        Returns ``False`` when a strictly-newer revision is already stored,
        or when this exact revision is already embedded by this same
        model - this is the out-of-order-redelivery guard (docs/DESIGN.md
        8.4, write-path guard 1): an at-least-once, out-of-order outbox
        redelivery must never overwrite a newer embedding with an older
        one. The second case is what makes a forced re-embed sync
        (docs/DESIGN.md 8.5) cheap to re-run after partial completion:
        revisions already migrated to the new model are skipped rather than
        rewritten with an identical vector, while a same-revision write
        under a *different* model is still admitted and returns ``True``.

        ``workspace_id`` is part of this Protocol's documented contract but
        is not independently re-verified by the write itself -
        ``document_embeddings`` has no ``workspace_id`` column by design
        (ADR-0010 3). Correctness depends entirely on the caller always
        deriving ``revision_id``/``revision_number`` from the
        workspace-scoped ``EmbeddingSource.get_revision`` before calling
        ``upsert``. This is a documented invariant implementations may rely
        on, not an oversight.
        """
        ...


@runtime_checkable
class EmbeddingForceSyncPort(Protocol):
    async def enqueue_all(self, workspace_id: uuid.UUID) -> int:
        """Enqueue one ``embedding.sync_requested`` event per current
        document (docs/DESIGN.md 10, ``POST /v1/admin/embedding-sync``).
        Return the count enqueued; delivery happens on the periodic sync
        loop's next poll, not inline - this is a trigger, not a blocking
        full reindex."""
        ...

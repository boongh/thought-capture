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
from datetime import datetime
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

    async def current_model_id(self) -> str:
        """The sidecar's currently-loaded model, composed the same way a
        computed ``EmbeddingVector.model_id`` is (F15-A, docs/plans/
        embedding-sync-review-round-4.md).

        This is the one place a ``reembed`` run's target model is allowed to
        come from: the API process holds no ``TC_EMBEDDING_MODEL_ID``/
        ``_REVISION`` of its own, and guessing rather than asking the
        sidecar would risk recording a run against a model the sidecar isn't
        actually serving.

        Raises:
            EmbeddingUnavailableError: the sidecar could not be reached, or
                its ``/health`` response did not match the expected shape -
                mirrors ``embed``'s degrade-explicit contract. Callers must
                never fall back to a guessed model id on this error; the
                caller (the ``/v1/admin/reembed`` handler) turns this into a
                loud 503 instead.
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


class EmbeddingModelConflictError(Exception):
    """Refusing a write that would leave ``document_embeddings`` holding
    vectors from two different embedding models within one workspace.

    The invariant this enforces (docs/DESIGN.md 8.5, 9.2): ``document_embeddings``
    never holds more than one distinct ``embedding_model_id`` within a
    workspace. A model change (a new ``TC_EMBEDDING_MODEL_ID`` or a bumped
    ``TC_EMBEDDING_MODEL_REVISION``) therefore halts ordinary sync for that
    workspace - loudly, via this exception - until an operator rebuilds the
    index under the new model; see ``docs/OPERATING.md``'s recovery runbook.
    ``document_id``, ``stored_model_id``, and ``incoming_model_id`` are all
    non-personal identifiers, safe to log (docs/DESIGN.md 14.2).
    """

    def __init__(
        self,
        *,
        document_id: uuid.UUID,
        stored_model_id: str,
        incoming_model_id: str,
    ) -> None:
        super().__init__(
            f"document_embeddings already holds model {stored_model_id!r} for this "
            f"workspace; refusing to write model {incoming_model_id!r} for document "
            f"{document_id}"
        )
        self.document_id = document_id
        self.stored_model_id = stored_model_id
        self.incoming_model_id = incoming_model_id


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
        rewritten with an identical vector.

        Raises:
            EmbeddingModelConflictError: this workspace's ``document_embeddings``
                already holds at least one row under a different
                ``embedding_model_id`` than ``vector.model_id``. Nothing is
                written in this case - a same-revision write under a
                *different* model is refused, not admitted, because
                ``document_embeddings`` must never hold more than one
                distinct model within a workspace at once (docs/DESIGN.md
                8.5, 9.2).

        ``workspace_id`` is part of this Protocol's documented contract and,
        as of this precondition, *is* independently re-verified by the write
        itself - it is joined against ``documents`` to recover the workspace
        scope ``document_embeddings`` itself has no ``workspace_id`` column
        for (ADR-0010 3), solely to enforce the single-model-per-workspace
        invariant above. This does not re-verify the *caller's* side of the
        contract: correctness of `revision_id`/`revision_number` still
        depends entirely on the caller always deriving them from the
        workspace-scoped ``EmbeddingSource.get_revision`` before calling
        ``upsert``. That part remains a documented invariant implementations
        may rely on, not something this write independently checks.
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


@dataclass(frozen=True, slots=True)
class ReembedRun:
    """One tracked ``runs(kind='reembed')`` row (F15-A, docs/plans/
    embedding-sync-review-round-4.md).

    Carries what an operator or ``POST /v1/admin/reembed`` caller needs to
    know about a model-migration sweep, without exposing the raw ``runs``
    row shape outward.
    """

    run_id: uuid.UUID
    workspace_id: uuid.UUID
    embedding_model_id: str
    status: str
    """One of ``'running'``, ``'succeeded'``, ``'failed'`` (``runs.status``)."""
    enqueued: int
    """Documents enqueued for re-embedding when this run started."""
    started_at: datetime
    """When this sweep began - the boundary ``has_dead_lettered_sync_events``
    uses so a dead letter from before this run started (an unrelated,
    already-stale failure) can never be mistaken for this sweep's own."""


@runtime_checkable
class ReembedRunStore(Protocol):
    async def start(self, workspace_id: uuid.UUID, *, embedding_model_id: str) -> ReembedRun:
        """Begin (or return the already-running) reembed sweep for
        ``workspace_id`` under ``embedding_model_id``.

        In one transaction: insert ``runs(kind='reembed', status='running',
        embedding_model_id=..., started_at=now())``, delete every
        ``document_embeddings`` row belonging to this workspace, and enqueue
        one ``embedding.sync_requested`` event per current document -
        reusing the same enqueue path ``ForceEmbeddingSync``/the organize
        writer already use, so all three trigger sources share one code
        path. The caller (the HTTP handler) must only acknowledge the
        request after this call returns, since it returns only after that
        transaction commits (CLAUDE.md's durability invariant).

        Idempotent while a run is already ``running`` for this workspace:
        returns that existing run's details unchanged rather than starting a
        second one or erroring (owner decision, round-4 F15).
        """
        ...

    async def find_running(self, workspace_id: uuid.UUID) -> ReembedRun | None:
        """The workspace's currently-``running`` reembed run, if any."""
        ...

    # The remaining methods are the narrow primitives
    # ``tc_application.reembed.ReconcileReembedRuns`` composes into the
    # succeeded/failed/still-running decision (docs/DESIGN.md 8.5, 9.2).
    # Deliberately not a single opaque ``reconcile()`` verb: that state
    # transition is exactly the kind of branching business logic this
    # codebase otherwise always keeps in the application layer, testable
    # against fakes without a real database (e.g. ``DeliverEmbeddingSync``
    # above composes ``EmbeddingSource``/``EmbeddingPort``/``EmbeddingWriter``
    # the same way, rather than a store deciding for it).

    async def list_running(self) -> tuple[ReembedRun, ...]:
        """Every workspace's currently-``running`` reembed run."""
        ...

    async def count_current_documents(self, workspace_id: uuid.UUID) -> int:
        """How many of this workspace's documents currently have a revision
        (the same population ``EmbeddingForceSyncPort.enqueue_all`` and
        ``start`` above enqueue against)."""
        ...

    async def count_embedded_under_model(
        self, workspace_id: uuid.UUID, embedding_model_id: str
    ) -> int:
        """How many of this workspace's ``document_embeddings`` rows already
        carry ``embedding_model_id`` for the document's *current* revision -
        joined through ``documents`` on both ``document_id`` and
        ``current_revision_id`` for workspace scope and revision currency,
        since ``document_embeddings`` has no ``workspace_id`` of its own. An
        embedding of a since-superseded revision must not count."""
        ...

    async def has_dead_lettered_sync_events(
        self, workspace_id: uuid.UUID, *, since: datetime
    ) -> bool:
        """True if any of this workspace's ``embedding.sync_requested``
        events created at or after ``since`` has exhausted ``max_attempts``
        without being delivered.

        ``since`` must be the reconciled run's own ``started_at`` - never
        unbounded. A dead letter from before this sweep began is a stale,
        unrelated failure (e.g. an ordinary sync event that dead-lettered
        weeks earlier for its own reasons); nothing in this codebase ever
        clears or ages out a dead-lettered row, so an unbounded check would
        let one old, unrelated failure permanently fail every future reembed
        attempt for that workspace."""
        ...

    async def mark_succeeded(self, run_id: uuid.UUID) -> None:
        """Sets ``status='succeeded'`` and ``finished_at=now()``."""
        ...

    async def mark_failed(self, run_id: uuid.UUID, *, error_code: str) -> None:
        """Sets ``status='failed'``, ``finished_at=now()``, and
        ``error_code`` - never personal content (docs/DESIGN.md 14.2)."""
        ...

"""``DeliverEmbeddingSync``/``ForceEmbeddingSync`` orchestration, driven with
fakes (docs/DESIGN.md 8.4) - same test shapes as ``tests/unit/test_khoj_sync.py``.

Fakes are defined locally rather than added to ``tests/unit/fakes.py``: this
file owns only itself and ``packages/application/src/tc_application/embedding_sync.py``
per the fix brief, and ``tests/unit/fakes.py`` is shared with other in-flight
work.
"""

from __future__ import annotations

import uuid

from tc_application.embedding_sync import DeliverEmbeddingSync, ForceEmbeddingSync
from tc_domain.embedding_ports import (
    EmbeddingUnavailableError,
    EmbeddingVector,
    PendingEmbeddingSync,
)
from tc_domain.errors import EmbeddingSourceNotFound

REVISION_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
VECTOR = EmbeddingVector(values=(0.1, 0.2, 0.3), model_id="fake-model", dimensions=3)


class FakeEmbeddingSyncOutbox:
    """Records claims and settlements rather than touching a real outbox."""

    def __init__(self, pending: list[PendingEmbeddingSync] | None = None) -> None:
        self._pending = pending or []
        self.delivered: list[uuid.UUID] = []
        self.failed: list[dict[str, object]] = []

    async def claim(self, limit: int) -> tuple[PendingEmbeddingSync, ...]:
        claimed = tuple(self._pending[:limit])
        self._pending = self._pending[limit:]
        return claimed

    async def mark_delivered(self, event_id: uuid.UUID) -> None:
        self.delivered.append(event_id)

    async def mark_failed(self, event_id: uuid.UUID, *, attempts: int, error: str) -> None:
        self.failed.append({"event_id": event_id, "attempts": attempts, "error": error})


class FakeEmbeddingSource:
    """Returns a fixed revision for any document, unless told to fail."""

    def __init__(self, revision: object, *, missing: set[uuid.UUID] | None = None) -> None:
        self.revision = revision
        self.missing = missing or set()
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def get_revision(self, *, workspace_id: uuid.UUID, document_id: uuid.UUID) -> object:
        self.calls.append((workspace_id, document_id))
        if document_id in self.missing:
            raise EmbeddingSourceNotFound(f"no such document: {document_id}")
        return self.revision


class FakeEmbeddingPort:
    """Records every embed call; can be told to fail on demand."""

    def __init__(
        self, *, vector: EmbeddingVector = VECTOR, raises: Exception | None = None
    ) -> None:
        self.vector = vector
        self.raises = raises
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]:
        self.calls.append(texts)
        if self.raises is not None:
            raise self.raises
        return tuple(self.vector for _ in texts)


class FakeEmbeddingWriter:
    """Records every upsert call; returns a fixed "was written" result."""

    def __init__(self, *, wrote: bool = True, raises: Exception | None = None) -> None:
        self.wrote = wrote
        self.raises = raises
        self.calls: list[dict[str, object]] = []

    async def upsert(
        self,
        *,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        revision_number: int,
        vector: EmbeddingVector,
    ) -> bool:
        if self.raises is not None:
            raise self.raises
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "document_id": document_id,
                "revision_id": revision_id,
                "revision_number": revision_number,
                "vector": vector,
            }
        )
        return self.wrote


class FakeEmbeddingForceSync:
    """Records the workspace it was asked to enqueue for; returns a fixed count."""

    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.calls: list[uuid.UUID] = []

    async def enqueue_all(self, workspace_id: uuid.UUID) -> int:
        self.calls.append(workspace_id)
        return self.count


class FakeRevision:
    """Stand-in for ``RevisionForEmbedding`` - a plain namespace is enough
    since the fakes above don't validate its type, only read its fields."""

    def __init__(
        self,
        *,
        document_id: uuid.UUID = DOCUMENT_ID,
        revision_id: uuid.UUID = REVISION_ID,
        revision_number: int = 1,
        title: str = "Aurora",
        body_markdown: str = "## Summary\n\nsomething",
    ) -> None:
        self.document_id = document_id
        self.revision_id = revision_id
        self.revision_number = revision_number
        self.title = title
        self.body_markdown = body_markdown


REVISION = FakeRevision()


def a_pending(*, attempts: int = 1, document_id: uuid.UUID | None = None) -> PendingEmbeddingSync:
    return PendingEmbeddingSync(
        event_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        document_id=document_id or uuid.uuid4(),
        attempts=attempts,
    )


def _deliver(
    outbox: FakeEmbeddingSyncOutbox,
    *,
    source: FakeEmbeddingSource | None = None,
    embed: FakeEmbeddingPort | None = None,
    writer: FakeEmbeddingWriter | None = None,
) -> DeliverEmbeddingSync:
    return DeliverEmbeddingSync(
        outbox=outbox,
        source=source or FakeEmbeddingSource(REVISION),
        embed=embed or FakeEmbeddingPort(),
        writer=writer or FakeEmbeddingWriter(),
    )


async def test_no_pending_events_syncs_nothing() -> None:
    outbox = FakeEmbeddingSyncOutbox()
    deliver = _deliver(outbox)

    synced = await deliver()

    assert synced == 0
    assert outbox.delivered == []
    assert outbox.failed == []


async def test_a_successful_sync_marks_the_event_delivered() -> None:
    event = a_pending(document_id=DOCUMENT_ID)
    outbox = FakeEmbeddingSyncOutbox([event])
    embed = FakeEmbeddingPort()
    writer = FakeEmbeddingWriter(wrote=True)
    deliver = _deliver(outbox, embed=embed, writer=writer)

    synced = await deliver()

    assert synced == 1
    assert outbox.delivered == [event.event_id]
    assert outbox.failed == []
    assert embed.calls == [(REVISION.body_markdown,)]
    assert len(writer.calls) == 1
    assert writer.calls[0]["revision_id"] == REVISION.revision_id


async def test_embedding_sidecar_unavailable_marks_the_event_failed_with_a_sanitized_error() -> (
    None
):
    event = a_pending(document_id=DOCUMENT_ID, attempts=2)
    outbox = FakeEmbeddingSyncOutbox([event])
    embed = FakeEmbeddingPort(
        raises=EmbeddingUnavailableError("sidecar returned 503: secret-token")
    )
    deliver = _deliver(outbox, embed=embed)

    synced = await deliver()

    assert synced == 0
    assert outbox.delivered == []
    assert outbox.failed == [
        {"event_id": event.event_id, "attempts": 2, "error": "EmbeddingUnavailableError"}
    ]


async def test_a_stale_revision_still_counts_as_delivered_not_failed() -> None:
    """``writer.upsert`` returning False means a strictly-newer revision was
    already stored - the event was still handled correctly, there was
    nothing newer to write. This must be treated as success, not failure:
    if someone "fixes" this to call mark_failed on a False return, this test
    fails."""
    event = a_pending(document_id=DOCUMENT_ID)
    outbox = FakeEmbeddingSyncOutbox([event])
    writer = FakeEmbeddingWriter(wrote=False)
    deliver = _deliver(outbox, writer=writer)

    synced = await deliver()

    assert synced == 1
    assert outbox.delivered == [event.event_id]
    assert outbox.failed == []


async def test_source_lookup_failure_marks_the_event_failed() -> None:
    event = a_pending()
    outbox = FakeEmbeddingSyncOutbox([event])
    source = FakeEmbeddingSource(REVISION, missing={event.document_id})
    deliver = _deliver(outbox, source=source)

    synced = await deliver()

    assert synced == 0
    assert outbox.delivered == []
    assert len(outbox.failed) == 1
    assert outbox.failed[0]["error"] == "EmbeddingSourceNotFound"


async def test_multiple_events_are_each_synced_independently() -> None:
    good = a_pending(document_id=DOCUMENT_ID)
    bad = a_pending()
    outbox = FakeEmbeddingSyncOutbox([good, bad])
    source = FakeEmbeddingSource(REVISION, missing={bad.document_id})
    deliver = _deliver(outbox, source=source)

    synced = await deliver()

    assert synced == 1
    assert outbox.delivered == [good.event_id]
    assert len(outbox.failed) == 1
    assert outbox.failed[0]["event_id"] == bad.event_id


async def test_the_error_recorded_never_contains_document_content() -> None:
    event = a_pending(document_id=DOCUMENT_ID)
    outbox = FakeEmbeddingSyncOutbox([event])
    embed = FakeEmbeddingPort(
        raises=EmbeddingUnavailableError("body: my secret thought about the merger")
    )
    deliver = _deliver(outbox, embed=embed)

    await deliver()

    assert outbox.failed[0]["error"] == "EmbeddingUnavailableError"
    assert "secret" not in str(outbox.failed[0]["error"])


async def test_force_sync_delegates_to_the_enqueuer_and_returns_its_count() -> None:
    workspace_id = uuid.uuid4()
    enqueuer = FakeEmbeddingForceSync(count=3)
    force = ForceEmbeddingSync(enqueuer)

    enqueued = await force(workspace_id)

    assert enqueued == 3
    assert enqueuer.calls == [workspace_id]

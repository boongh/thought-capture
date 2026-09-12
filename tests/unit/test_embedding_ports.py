"""Runtime-checkable conformance for tc_domain.embedding_ports.

These are pure declarations (T2.1 has no behavior of its own), so the only
thing worth proving here is that a minimal stub implementing each Protocol's
methods actually satisfies `isinstance(stub, ProtocolName)` - i.e. the
Protocols are structurally correct, not just type-checker fiction.
"""

from __future__ import annotations

import uuid

from tc_domain.embedding_ports import (
    EmbeddingForceSyncPort,
    EmbeddingPort,
    EmbeddingSource,
    EmbeddingSyncOutbox,
    EmbeddingVector,
    EmbeddingWriter,
    PendingEmbeddingSync,
    RevisionForEmbedding,
)

WORKSPACE_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
REVISION_ID = uuid.uuid4()
EVENT_ID = uuid.uuid4()


class StubEmbeddingPort:
    async def embed(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]:
        return tuple(
            EmbeddingVector(values=(0.0, 0.0), model_id="stub@v1", dimensions=2) for _ in texts
        )


class StubEmbeddingSyncOutbox:
    async def claim(self, limit: int) -> tuple[PendingEmbeddingSync, ...]:
        return ()

    async def mark_delivered(self, event_id: uuid.UUID) -> None:
        return None

    async def mark_failed(self, event_id: uuid.UUID, *, attempts: int, error: str) -> None:
        return None


class StubEmbeddingSource:
    async def get_revision(
        self, *, workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> RevisionForEmbedding:
        return RevisionForEmbedding(
            document_id=document_id,
            revision_id=REVISION_ID,
            revision_number=1,
            title="stub",
            body_markdown="stub body",
        )


class StubEmbeddingWriter:
    async def upsert(
        self,
        *,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        revision_number: int,
        vector: EmbeddingVector,
    ) -> bool:
        return True


class StubEmbeddingForceSyncPort:
    async def enqueue_all(self, workspace_id: uuid.UUID) -> int:
        return 0


def test_embedding_port_conformance() -> None:
    assert isinstance(StubEmbeddingPort(), EmbeddingPort)


def test_embedding_sync_outbox_conformance() -> None:
    assert isinstance(StubEmbeddingSyncOutbox(), EmbeddingSyncOutbox)


def test_embedding_source_conformance() -> None:
    assert isinstance(StubEmbeddingSource(), EmbeddingSource)


def test_embedding_writer_conformance() -> None:
    assert isinstance(StubEmbeddingWriter(), EmbeddingWriter)


def test_embedding_force_sync_port_conformance() -> None:
    assert isinstance(StubEmbeddingForceSyncPort(), EmbeddingForceSyncPort)


def test_embedding_vector_and_pending_sync_are_plain_value_types() -> None:
    vector = EmbeddingVector(values=(0.1, 0.2), model_id="stub@v1", dimensions=2)
    pending = PendingEmbeddingSync(
        event_id=EVENT_ID,
        workspace_id=WORKSPACE_ID,
        document_id=DOCUMENT_ID,
        attempts=0,
    )

    assert vector.dimensions == 2
    assert pending.document_id == DOCUMENT_ID

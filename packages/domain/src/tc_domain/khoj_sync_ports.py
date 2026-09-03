"""Ports for delivering Khoj index sync (docs/DESIGN.md 6.5, 7.2 step 11).

Mirrors ``tc_domain.digest_ports`` deliberately: index sync and digest
delivery are the same shape of problem - a durable outbox event that must be
turned into an external side effect, at-least-once, with the canonical write
already committed before either consumer ever runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tc_domain.khoj_export import DocumentExport


@dataclass(frozen=True, slots=True)
class PendingKhojSync:
    """One claimed ``khoj.sync_requested`` event.

    Carries ``document_id`` only, never a ``revision_id``: the consumer must
    always re-read the document's *current* revision at delivery time
    (``KhojExportSource.get_export``), not trust a possibly-superseded id from
    an older queued event - two organize runs on the same document close
    together would otherwise risk the later-delivered but earlier-queued
    event overwriting Khoj with stale content.
    """

    event_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    attempts: int


@runtime_checkable
class KhojSyncOutbox(Protocol):
    async def claim(self, limit: int) -> tuple[PendingKhojSync, ...]:
        """Lease up to ``limit`` due ``khoj.sync_requested`` events.

        A malformed payload is failed immediately by the implementation
        rather than being returned, since it will never parse successfully on
        retry either.
        """
        ...

    async def mark_delivered(self, event_id: uuid.UUID) -> None: ...

    async def mark_failed(self, event_id: uuid.UUID, *, attempts: int, error: str) -> None:
        """Reschedule with backoff. ``error`` must not contain personal content."""
        ...


@runtime_checkable
class KhojExportSource(Protocol):
    async def get_export(
        self, *, workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> DocumentExport:
        """Raise ``KhojExportNotFound`` unless the document resolves and belongs
        to this workspace - never on ``document_id`` alone."""
        ...


@runtime_checkable
class KhojForceSyncPort(Protocol):
    async def enqueue_all(self, workspace_id: uuid.UUID) -> int:
        """Enqueue one ``khoj.sync_requested`` event per current document
        (docs/DESIGN.md 10, ``POST /v1/admin/khoj-sync``). Return the count
        enqueued; delivery happens on the periodic sync loop's next poll, not
        inline - this is a trigger, not a blocking full reindex."""
        ...


@runtime_checkable
class KhojIndexRecorder(Protocol):
    """Writes ``khoj_index_items`` (docs/DESIGN.md 6.5): the *last successful*
    sync only. A failed attempt is never recorded here - it stays on the
    outbox event's own ``attempts``/``last_error`` (see migration 0007's
    docstring for why)."""

    async def record_synced(
        self,
        *,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        filename: str,
        revision_id: uuid.UUID,
        body_sha256: str,
    ) -> None: ...

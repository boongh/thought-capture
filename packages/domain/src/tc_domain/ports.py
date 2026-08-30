"""Ports the domain depends on, implemented by ``tc_infrastructure``.

These are ``Protocol`` classes rather than ABCs so that adapters need no import
of the domain to satisfy them structurally, and so that tests can supply plain
fakes. Every contract that matters for correctness is stated in the docstring,
because the type signature alone cannot express atomicity or idempotency.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

from tc_domain.capture import (
    AppendOutcome,
    ArchivedAttachment,
    AttachmentCandidate,
    CaptureSource,
    ThoughtDraft,
    ThoughtId,
)


@runtime_checkable
class Clock(Protocol):
    """Wall-clock time, injected so that time-dependent logic is testable."""

    def now(self) -> dt.datetime:
        """Return the current instant. Must be timezone-aware (UTC)."""
        ...


@runtime_checkable
class AttachmentArchive(Protocol):
    """Copies an attachment from its expiring source URL into durable storage."""

    async def archive(self, candidate: AttachmentCandidate) -> ArchivedAttachment:
        """Stream, hash, and atomically place the attachment.

        Contract:

        - The implementation streams to a temporary file, computes SHA-256, and
          only then moves the object into content-addressed storage, so a
          partial download can never be observed as a complete blob.
        - Storing the same bytes twice is idempotent and returns the same
          ``storage_key``.
        - Raises ``AttachmentArchiveFailed`` on any transport or integrity
          failure. The caller must not acknowledge the capture in that case.
        - A blob written here may be left unreferenced if the later database
          transaction fails. Reclaiming those is the garbage collector's job
          after a safety interval (docs/DESIGN.md 7.1), never the caller's.
        """
        ...


@runtime_checkable
class ThoughtRepository(Protocol):
    """Append-only access to the canonical capture log."""

    async def find_id_by_source_message(
        self, source: CaptureSource, source_message_id: str
    ) -> ThoughtId | None:
        """Return the existing thought for this source message, if any.

        A fast path for redelivery. It is advisory only: the authoritative
        idempotency guarantee is the unique constraint enforced by ``append``.
        """
        ...

    async def append(self, draft: ThoughtDraft) -> AppendOutcome:
        """Append the thought, its blobs, its attachment links, and its outbox event.

        Contract:

        - All of it commits in **one** transaction, or none of it does. The
          acknowledgement the user sees depends on this (docs/DESIGN.md 3.1:
          "Acknowledge only after durable commit").
        - Idempotent on ``(source, source_message_id)``. On conflict the
          implementation returns the existing ``thought_id`` with
          ``created=False`` rather than raising, so that a Discord redelivery
          produces one row and one acknowledgement.
        - Never updates or deletes an existing thought. The database rejects
          both (ADR-0001); this contract simply records that the repository does
          not attempt it.
        - Writes the ``thought.captured`` outbox event in that same transaction,
          so a committed thought and its pending acknowledgement cannot diverge
          (docs/DESIGN.md 6.5). Delivery happens afterwards, with retries.
        """
        ...

"""Ports for delivering the daily digest (docs/DESIGN.md 4.2, 6.5)."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Protocol, runtime_checkable

from tc_domain.digest import DigestContent


@dataclasses.dataclass(frozen=True, slots=True)
class PendingDigest:
    """One claimed ``digest.ready`` event, parsed and ready to deliver."""

    event_id: uuid.UUID
    run_id: uuid.UUID
    document_id: uuid.UUID
    revision_id: uuid.UUID
    attempts: int


@runtime_checkable
class DigestOutbox(Protocol):
    """The subset of outbox behavior digest delivery needs."""

    async def claim(self, limit: int) -> tuple[PendingDigest, ...]:
        """Lease up to ``limit`` due ``digest.ready`` events.

        A malformed payload is failed immediately by the implementation rather
        than being returned, since it will never parse successfully on retry
        either.
        """
        ...

    async def mark_delivered(self, event_id: uuid.UUID) -> None:
        """Record that every chunk of this digest was actually sent."""
        ...

    async def mark_failed(self, event_id: uuid.UUID, *, attempts: int, error: str) -> None:
        """Reschedule with backoff. ``error`` must not contain personal content."""
        ...


@runtime_checkable
class DigestSource(Protocol):
    """Reads the specific document revision an outbox event points at."""

    async def get(self, document_id: uuid.UUID, revision_id: uuid.UUID) -> DigestContent:
        """Raise if the revision no longer resolves (e.g. a corrupted event)."""
        ...


@runtime_checkable
class DigestSender(Protocol):
    """Delivers pre-formatted message chunks to wherever the owner reads digests."""

    async def send(self, chunks: tuple[str, ...]) -> bool:
        """Send every chunk in order. Return whether all of them were delivered.

        A partial failure (some chunks sent, one fails) still returns
        ``False``, so the whole digest is retried - a duplicate chunk is a
        cosmetic fault, a missing one is not (docs/DESIGN.md 6.5).
        """
        ...

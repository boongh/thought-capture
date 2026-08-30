"""In-memory doubles for the domain ports.

They implement the documented contracts in ``tc_domain.ports`` - in particular
idempotency on ``(source, source_message_id)`` - so that use-case tests exercise
the real decision logic rather than a simplification of it.
"""

from __future__ import annotations

import hashlib

from tc_domain.capture import (
    AppendOutcome,
    ArchivedAttachment,
    AttachmentCandidate,
    CaptureSource,
    Sha256,
    ThoughtDraft,
    ThoughtId,
)
from tc_domain.errors import AttachmentArchiveFailed


class FakeThoughtRepository:
    """Append-only in-memory log with the same uniqueness rule as PostgreSQL."""

    def __init__(self) -> None:
        self.appended: list[ThoughtDraft] = []
        self._ids_by_key: dict[tuple[CaptureSource, str], ThoughtId] = {}
        self._next_id = 1
        # When True, `find_id_by_source_message` pretends not to know about a
        # row that `append` will nonetheless conflict with. This reproduces the
        # race between two concurrent deliveries of the same message.
        self.hide_from_lookup = False

    async def find_id_by_source_message(
        self, source: CaptureSource, source_message_id: str
    ) -> ThoughtId | None:
        if self.hide_from_lookup:
            return None
        return self._ids_by_key.get((source, source_message_id))

    async def append(self, draft: ThoughtDraft) -> AppendOutcome:
        key = (draft.source, draft.source_message_id)
        existing = self._ids_by_key.get(key)
        if existing is not None:
            return AppendOutcome(thought_id=existing, created=False)

        thought_id = ThoughtId(self._next_id)
        self._next_id += 1
        self._ids_by_key[key] = thought_id
        self.appended.append(draft)
        return AppendOutcome(thought_id=thought_id, created=True)


class FakeAttachmentArchive:
    """Content-addressed archive that hashes the candidate's URL as stand-in bytes."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.archived: list[AttachmentCandidate] = []
        self.fail_on = fail_on

    async def archive(self, candidate: AttachmentCandidate) -> ArchivedAttachment:
        if self.fail_on is not None and candidate.filename == self.fail_on:
            raise AttachmentArchiveFailed(f"synthetic failure archiving {candidate.filename!r}")

        self.archived.append(candidate)
        digest = hashlib.sha256(candidate.url.encode("utf-8")).hexdigest()
        return ArchivedAttachment(
            sha256=Sha256(digest),
            size_bytes=candidate.size_bytes,
            media_type=candidate.media_type,
            storage_key=f"{digest[:2]}/{digest}",
            source_filename=candidate.filename,
            source_url_expires_at=candidate.url_expires_at,
        )

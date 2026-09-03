"""In-memory doubles for the domain ports.

They implement the documented contracts in ``tc_domain.ports`` - in particular
idempotency on ``(source, source_message_id)`` - so that use-case tests exercise
the real decision logic rather than a simplification of it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

from tc_domain.capture import (
    AppendOutcome,
    ArchivedAttachment,
    AttachmentCandidate,
    CaptureSource,
    Sha256,
    ThoughtDraft,
    ThoughtId,
    WorkspaceId,
)
from tc_domain.context import Tier1Row
from tc_domain.digest import DigestContent
from tc_domain.digest_ports import PendingDigest
from tc_domain.errors import AttachmentArchiveFailed, DigestNotFound, KhojExportNotFound
from tc_domain.khoj_export import DocumentExport
from tc_domain.khoj_ports import KhojIndexFile, KhojSearchResult
from tc_domain.khoj_sync_ports import PendingKhojSync
from tc_domain.organize import OrganizeWriteRequest, OrganizeWriteResult, RunOutcome, WindowThought
from tc_domain.search import SearchPage, SearchQuery


class FakeThoughtRepository:
    """Append-only in-memory log with the same uniqueness rule as PostgreSQL."""

    def __init__(self) -> None:
        self.appended: list[ThoughtDraft] = []
        self._ids_by_key: dict[tuple[WorkspaceId, CaptureSource, str], ThoughtId] = {}
        self._next_id = 1
        # When True, `find_id_by_source_message` pretends not to know about a
        # row that `append` will nonetheless conflict with. This reproduces the
        # race between two concurrent deliveries of the same message.
        self.hide_from_lookup = False

    async def find_id_by_source_message(
        self,
        workspace_id: WorkspaceId,
        source: CaptureSource,
        source_message_id: str,
    ) -> ThoughtId | None:
        if self.hide_from_lookup:
            return None
        return self._ids_by_key.get((workspace_id, source, source_message_id))

    async def append(self, draft: ThoughtDraft) -> AppendOutcome:
        key = (draft.workspace_id, draft.source, draft.source_message_id)
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


class FakeThoughtWindowReader:
    """Ignores the requested bounds and returns whatever the test set up.

    Real window filtering is PostgreSQL's job (``PostgresThoughtReader.list_between``,
    proven by integration tests); this fake exists to drive the pipeline's
    *orchestration*, not to re-prove the query.
    """

    def __init__(self, thoughts: list[WindowThought]) -> None:
        self.thoughts = thoughts

    async def list_between(
        self, workspace_id: WorkspaceId, start: dt.datetime, end: dt.datetime
    ) -> list[WindowThought]:
        return self.thoughts


class FakeContextIndex:
    def __init__(
        self, *, index: tuple[Tier1Row, ...] = (), bodies: dict[str, str] | None = None
    ) -> None:
        self.index = index
        self.bodies = bodies or {}

    async def tier1_index(self, workspace_id: WorkspaceId) -> tuple[Tier1Row, ...]:
        return self.index

    async def bodies_for(
        self, workspace_id: WorkspaceId, stable_keys: frozenset[str]
    ) -> dict[str, str]:
        return {key: body for key, body in self.bodies.items() if key in stable_keys}


class FakeExactSearch:
    """Records the query it was given and returns whatever the test set up.

    Real filtering/ranking is PostgreSQL's job (``PostgresExactSearch``,
    proven by integration tests); this fake exists to drive the ``Search``
    use case's mode-gating, not to re-prove the query.
    """

    def __init__(self, page: SearchPage | None = None) -> None:
        self.page = page if page is not None else SearchPage(items=(), next_cursor=None)
        self.calls: list[tuple[WorkspaceId, SearchQuery]] = []

    async def search(self, workspace_id: WorkspaceId, query: SearchQuery) -> SearchPage:
        self.calls.append((workspace_id, query))
        return self.page


class FakeOrganizeWriter:
    """Records the request it was given rather than writing anything."""

    def __init__(self) -> None:
        self.calls: list[OrganizeWriteRequest] = []
        self.outcomes: list[RunOutcome] = []

    async def write(
        self,
        *,
        workspace_id: WorkspaceId,
        run_id: uuid.UUID,
        request: OrganizeWriteRequest,
        outcome: RunOutcome,
    ) -> OrganizeWriteResult:
        self.calls.append(request)
        self.outcomes.append(outcome)
        document_ids = {doc.stable_key: uuid.uuid4() for doc in request.documents}
        revision_ids = {doc.stable_key: uuid.uuid4() for doc in request.documents}
        digest_id = next(
            (
                document_ids[doc.stable_key]
                for doc in request.documents
                if doc.kind == "daily_digest"
            ),
            None,
        )
        return OrganizeWriteResult(
            document_ids=document_ids, revision_ids=revision_ids, digest_document_id=digest_id
        )


class FakeRunLedger:
    """Records lifecycle transitions for one pipeline run."""

    def __init__(self) -> None:
        self.started: list[tuple[WorkspaceId, dt.datetime, dt.datetime, str]] = []
        self.succeeded: list[dict[str, object]] = []
        self.failed: list[dict[str, object]] = []

    async def start(
        self,
        workspace_id: WorkspaceId,
        *,
        window_start: dt.datetime,
        window_end: dt.datetime,
        kind: str = "organize",
    ) -> uuid.UUID:
        self.started.append((workspace_id, window_start, window_end, kind))
        return uuid.uuid4()

    async def succeed(
        self,
        run_id: uuid.UUID,
        *,
        model_provider: str | None,
        model_id: str | None,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        context_recall: float | None,
        context_degraded: bool,
    ) -> None:
        self.succeeded.append(
            {
                "run_id": run_id,
                "model_provider": model_provider,
                "model_id": model_id,
                "prompt_version": prompt_version,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "context_recall": context_recall,
                "context_degraded": context_degraded,
            }
        )

    async def fail(self, run_id: uuid.UUID, *, error_code: str, error_detail: str) -> None:
        self.failed.append(
            {"run_id": run_id, "error_code": error_code, "error_detail": error_detail}
        )


class FakeDigestOutbox:
    """Records claims and settlements rather than touching a real outbox."""

    def __init__(self, pending: list[PendingDigest] | None = None) -> None:
        self._pending = pending or []
        self.delivered: list[uuid.UUID] = []
        self.failed: list[dict[str, object]] = []

    async def claim(self, limit: int) -> tuple[PendingDigest, ...]:
        claimed = tuple(self._pending[:limit])
        self._pending = self._pending[limit:]
        return claimed

    async def mark_delivered(self, event_id: uuid.UUID) -> None:
        self.delivered.append(event_id)

    async def mark_failed(self, event_id: uuid.UUID, *, attempts: int, error: str) -> None:
        self.failed.append({"event_id": event_id, "attempts": attempts, "error": error})


class FakeDigestSource:
    """Returns a fixed body for any document/revision, unless told to fail."""

    def __init__(
        self, content: DigestContent | None = None, *, missing: set[uuid.UUID] | None = None
    ) -> None:
        self.content = content
        self.missing = missing or set()
        self.calls: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]] = []

    async def get(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> DigestContent:
        self.calls.append((workspace_id, run_id, document_id, revision_id))
        if revision_id in self.missing:
            raise DigestNotFound(f"no such revision: {revision_id}")
        assert self.content is not None
        return self.content


class FakeDigestSender:
    """Records every send; can be told to fail or raise on demand."""

    def __init__(self, *, succeed: bool = True, raises: Exception | None = None) -> None:
        self.succeed = succeed
        self.raises = raises
        self.sent: list[tuple[str, ...]] = []

    async def send(self, chunks: tuple[str, ...]) -> bool:
        if self.raises is not None:
            raise self.raises
        self.sent.append(chunks)
        return self.succeed


class FakeKhojSyncOutbox:
    """Records claims and settlements rather than touching a real outbox."""

    def __init__(self, pending: list[PendingKhojSync] | None = None) -> None:
        self._pending = pending or []
        self.delivered: list[uuid.UUID] = []
        self.failed: list[dict[str, object]] = []

    async def claim(self, limit: int) -> tuple[PendingKhojSync, ...]:
        claimed = tuple(self._pending[:limit])
        self._pending = self._pending[limit:]
        return claimed

    async def mark_delivered(self, event_id: uuid.UUID) -> None:
        self.delivered.append(event_id)

    async def mark_failed(self, event_id: uuid.UUID, *, attempts: int, error: str) -> None:
        self.failed.append({"event_id": event_id, "attempts": attempts, "error": error})


class FakeKhojExportSource:
    """Returns a fixed export for any document, unless told to fail."""

    def __init__(
        self, export: DocumentExport | None = None, *, missing: set[uuid.UUID] | None = None
    ) -> None:
        self.export = export
        self.missing = missing or set()
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def get_export(
        self, *, workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> DocumentExport:
        self.calls.append((workspace_id, document_id))
        if document_id in self.missing:
            raise KhojExportNotFound(f"no such document: {document_id}")
        assert self.export is not None
        return self.export


class FakeKhojPort:
    """Records every index/search call; can be told to fail on demand."""

    def __init__(
        self,
        *,
        search_results: tuple[KhojSearchResult, ...] = (),
        raises: Exception | None = None,
    ) -> None:
        self.indexed: list[tuple[KhojIndexFile, ...]] = []
        self.deleted: list[tuple[str, ...]] = []
        self.search_results = search_results
        self.search_queries: list[str] = []
        self.raises = raises

    async def index(self, files: tuple[KhojIndexFile, ...]) -> None:
        if self.raises is not None:
            raise self.raises
        self.indexed.append(files)

    async def delete(self, filenames: tuple[str, ...]) -> None:
        if self.raises is not None:
            raise self.raises
        self.deleted.append(filenames)

    async def search(self, q: str, *, limit: int = 5) -> tuple[KhojSearchResult, ...]:
        if self.raises is not None:
            raise self.raises
        self.search_queries.append(q)
        return self.search_results


class FakeKhojIndexRecorder:
    """Records every ``khoj_index_items`` write rather than touching a real table."""

    def __init__(self) -> None:
        self.recorded: list[dict[str, object]] = []

    async def record_synced(
        self,
        *,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        filename: str,
        revision_id: uuid.UUID,
        body_sha256: str,
    ) -> None:
        self.recorded.append(
            {
                "workspace_id": workspace_id,
                "document_id": document_id,
                "filename": filename,
                "revision_id": revision_id,
                "body_sha256": body_sha256,
            }
        )


class FakeKhojForceSync:
    """Records the workspace it was asked to enqueue for; returns a fixed count."""

    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.calls: list[uuid.UUID] = []

    async def enqueue_all(self, workspace_id: uuid.UUID) -> int:
        self.calls.append(workspace_id)
        return self.count

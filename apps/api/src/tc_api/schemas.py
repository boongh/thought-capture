"""Request and response models for the /v1 surface.

Timestamps are emitted as UTC with offsets (docs/DESIGN.md 10). The workspace is
never accepted from the client: it comes from authentication.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from tc_domain.ask import AskChunk, AskReference
from tc_domain.search import SearchPage, SearchResult
from tc_infrastructure.db.document_reader import DocumentDetail, DocumentSummary
from tc_infrastructure.db.entity_reader import EntityRecord
from tc_infrastructure.db.thought_reader import ThoughtPage, ThoughtRecord


class CaptureRequest(BaseModel):
    """API and import capture (docs/DESIGN.md 10).

    Idempotency comes from the `Idempotency-Key` header rather than the body, so
    a client can retry a request byte-for-byte.
    """

    body: str = Field(default="", max_length=100_000)
    client_created_at: dt.datetime | None = Field(
        default=None,
        description="When the thought occurred. Defaults to now. Must carry an offset.",
    )
    client_timezone: str | None = Field(
        default=None, description="IANA name. Defaults to the workspace timezone."
    )
    content_language: str = Field(default="en", max_length=35)
    correction_of: int | None = Field(
        default=None, description="The thought this one corrects. Appends, never edits."
    )


class CaptureResponse(BaseModel):
    thought_id: int
    deduplicated: bool = Field(
        description="True when this key had already been captured; no new row was created."
    )
    attachment_count: int


class AttachmentResponse(BaseModel):
    sha256: str
    media_type: str
    size_bytes: int
    source_filename: str
    extracted_status: str = Field(
        description=(
            "Always 'not_supported' in Release 1: attachments are archived, never read. "
            "The absence of extracted content is explicit rather than implied."
        )
    )


class ThoughtResponse(BaseModel):
    id: int
    source: str
    source_message_id: str
    source_channel_id: str | None
    body: str
    client_created_at: dt.datetime
    client_timezone: str
    client_local_date: dt.date
    client_local_time: dt.time
    received_at: dt.datetime
    content_language: str
    correction_of: int | None
    attachments: list[AttachmentResponse]

    @classmethod
    def of(cls, record: ThoughtRecord) -> ThoughtResponse:
        return cls(
            id=int(record.id),
            source=record.source,
            source_message_id=record.source_message_id,
            source_channel_id=record.source_channel_id,
            body=record.body,
            client_created_at=record.client_created_at,
            client_timezone=record.client_timezone,
            client_local_date=record.client_local_date,
            client_local_time=record.client_local_time,
            received_at=record.received_at,
            content_language=record.content_language,
            correction_of=int(record.correction_of) if record.correction_of is not None else None,
            attachments=[
                AttachmentResponse(
                    sha256=a.sha256,
                    media_type=a.media_type,
                    size_bytes=a.size_bytes,
                    source_filename=a.source_filename,
                    extracted_status=a.extracted_status,
                )
                for a in record.attachments
            ],
        )


class ThoughtListResponse(BaseModel):
    items: list[ThoughtResponse]
    next_cursor: str | None = Field(
        default=None, description="Opaque. Pass as `cursor` for the next page; null when exhausted."
    )

    @classmethod
    def of(cls, page: ThoughtPage) -> ThoughtListResponse:
        return cls(
            items=[ThoughtResponse.of(record) for record in page.items],
            next_cursor=page.next_cursor,
        )


class DocumentSummaryResponse(BaseModel):
    id: uuid.UUID
    kind: str
    stable_key: str
    title: str
    revision_number: int
    change_summary: str
    updated_at: dt.datetime

    @classmethod
    def of(cls, record: DocumentSummary) -> DocumentSummaryResponse:
        return cls(
            id=record.id,
            kind=record.kind,
            stable_key=record.stable_key,
            title=record.title,
            revision_number=record.revision_number,
            change_summary=record.change_summary,
            updated_at=record.updated_at,
        )


class DocumentListResponse(BaseModel):
    items: list[DocumentSummaryResponse]

    @classmethod
    def of(cls, records: list[DocumentSummary]) -> DocumentListResponse:
        return cls(items=[DocumentSummaryResponse.of(r) for r in records])


class DocumentDetailResponse(BaseModel):
    id: uuid.UUID
    kind: str
    stable_key: str
    title: str
    revision_number: int
    body_markdown: str
    change_summary: str
    updated_at: dt.datetime
    source_thought_ids: list[int]

    @classmethod
    def of(cls, record: DocumentDetail) -> DocumentDetailResponse:
        return cls(
            id=record.id,
            kind=record.kind,
            stable_key=record.stable_key,
            title=record.title,
            revision_number=record.revision_number,
            body_markdown=record.body_markdown,
            change_summary=record.change_summary,
            updated_at=record.updated_at,
            source_thought_ids=list(record.source_thought_ids),
        )


class EntityResponse(BaseModel):
    id: uuid.UUID
    entity_type: str
    canonical_name: str
    stable_key: str
    aliases: list[str]
    mention_count: int
    last_mentioned_at: dt.datetime | None
    created_at: dt.datetime

    @classmethod
    def of(cls, record: EntityRecord) -> EntityResponse:
        return cls(
            id=record.id,
            entity_type=record.entity_type,
            canonical_name=record.canonical_name,
            stable_key=record.stable_key,
            aliases=list(record.aliases),
            mention_count=record.mention_count,
            last_mentioned_at=record.last_mentioned_at,
            created_at=record.created_at,
        )


class EntityListResponse(BaseModel):
    items: list[EntityResponse]

    @classmethod
    def of(cls, records: list[EntityRecord]) -> EntityListResponse:
        return cls(items=[EntityResponse.of(r) for r in records])


class SearchResultResponse(BaseModel):
    result_id: str
    document_id: uuid.UUID
    revision_id: uuid.UUID
    thought_ids: list[int]
    kind: str
    title: str
    snippet: str
    updated_at: dt.datetime
    entities: list[str]
    channels: list[str]
    rank: float

    @classmethod
    def of(cls, record: SearchResult) -> SearchResultResponse:
        return cls(
            result_id=record.result_id,
            document_id=record.document_id,
            revision_id=record.revision_id,
            thought_ids=[int(t) for t in record.thought_ids],
            kind=record.kind,
            title=record.title,
            snippet=record.snippet,
            updated_at=record.updated_at,
            entities=list(record.entities),
            channels=list(record.channels),
            rank=record.rank,
        )


class SearchResponse(BaseModel):
    items: list[SearchResultResponse]
    next_cursor: str | None = Field(
        default=None, description="Opaque. Pass as `cursor` for the next page; null when exhausted."
    )
    degraded: bool = Field(description="True only when a requested channel could not run at all.")

    @classmethod
    def of(cls, page: SearchPage) -> SearchResponse:
        return cls(
            items=[SearchResultResponse.of(r) for r in page.items],
            next_cursor=page.next_cursor,
            degraded=page.degraded,
        )


class KhojSyncResponse(BaseModel):
    enqueued: int = Field(
        description=(
            "Documents just enqueued for Khoj index sync. Delivery happens on the "
            "worker's next sync-loop poll, not inline - this is a trigger, not a "
            "blocking full reindex."
        )
    )


class AskRequest(BaseModel):
    """docs/DESIGN.md 10's ``POST /v1/ask`` body. Filter fields mirror
    ``GET /v1/search``'s (docs/adr/0003's "Ask proxy" amendment, "Strict
    filters"): any of them set means "strict" - Ask cannot honor a
    structured filter today (Khoj's evidence-injection path is unverified),
    so it returns the equivalent exact-search results instead of silently
    querying Khoj's whole corpus (docs/DESIGN.md 7.6).
    """

    q: str = Field(min_length=1, max_length=2_000, description="The question to ask")
    phrase: str | None = Field(default=None, description="Exact, case-insensitive substring")
    include: list[str] = Field(default_factory=list, description="Required words")
    exclude: list[str] = Field(default_factory=list, description="Forbidden words")
    entity_id: uuid.UUID | None = None
    kind: str | None = Field(default=None, description="e.g. 'daily_digest', 'project', 'person'")
    source: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    local_time_from: dt.time | None = None
    local_time_to: dt.time | None = None


class AskReferenceResponse(BaseModel):
    document_id: uuid.UUID
    title: str
    snippet: str

    @classmethod
    def of(cls, record: AskReference) -> AskReferenceResponse:
        return cls(document_id=record.document_id, title=record.title, snippet=record.snippet)


class AskEvent(BaseModel):
    """One newline-delimited JSON object of the ``POST /v1/ask`` response
    stream (docs/DESIGN.md 7.6: "The gateway streams the answer").

    ``enabled``/``degraded``/``strict_unsupported`` are constant across every
    event of one call. Exactly one of ``delta`` (non-empty), ``references``
    (not null), or ``fallback`` (not null) is the reason a given event
    exists; the stream ends with exactly one event carrying ``done: true``.
    """

    enabled: bool = Field(
        description="False when this deployment has not opted into Ask (docs/adr/0003)."
    )
    degraded: bool = Field(
        description="True when Ask is enabled but Khoj could not answer right now."
    )
    strict_unsupported: bool = Field(
        description="True when structured filters were requested and could not be honored; "
        "see `fallback`."
    )
    delta: str = Field(default="", description="Incremental answer text.")
    references: list[AskReferenceResponse] | None = Field(
        default=None, description="The full, deduped citation list - sent once."
    )
    fallback: SearchResponse | None = Field(
        default=None,
        description="Exact-search results in place of an answer, when strict_unsupported.",
    )
    done: bool = Field(default=False, description="True on the final event of the stream.")

    @classmethod
    def of(cls, chunk: AskChunk) -> AskEvent:
        return cls(
            enabled=chunk.enabled,
            degraded=chunk.degraded,
            strict_unsupported=chunk.strict_unsupported,
            delta=chunk.text_delta,
            references=(
                [AskReferenceResponse.of(r) for r in chunk.references]
                if chunk.references is not None
                else None
            ),
            fallback=SearchResponse.of(chunk.fallback) if chunk.fallback is not None else None,
            done=chunk.done,
        )


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    database: str
    pending_outbox_events: int

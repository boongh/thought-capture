"""Request and response models for the /v1 surface.

Timestamps are emitted as UTC with offsets (docs/DESIGN.md 10). The workspace is
never accepted from the client: it comes from authentication.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

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


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    database: str
    pending_outbox_events: int

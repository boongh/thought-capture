"""Capture domain types.

Values are immutable. A ``CaptureCommand`` describes an intent to record a
message; a ``ThoughtDraft`` is the fully-derived row the repository appends; a
``CaptureResult`` is what the transport acknowledges.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NewType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tc_domain.errors import InvalidCaptureCommand

ThoughtId = NewType("ThoughtId", int)
WorkspaceId = NewType("WorkspaceId", uuid.UUID)
UserId = NewType("UserId", uuid.UUID)
Sha256 = NewType("Sha256", str)


class CaptureSource(StrEnum):
    """Matches the `source` CHECK constraint on `thoughts`."""

    DISCORD = "discord"
    API = "api"
    IMPORT = "import"


class ExtractedStatus(StrEnum):
    """Whether an attachment's content has been extracted.

    Release 1 performs no OCR, vision inference, or transcription, so the
    absence of extracted content is explicit rather than implied
    (docs/DESIGN.md 4.1).
    """

    NOT_SUPPORTED = "not_supported"


@dataclass(frozen=True, slots=True)
class AttachmentCandidate:
    """An attachment as described by the source platform, not yet archived.

    ``url`` is typically signed and short-lived: Discord attachment URLs expire,
    so the worker must copy the bytes during ingestion (docs/DESIGN.md 4.1).
    """

    filename: str
    media_type: str
    size_bytes: int
    url: str
    url_expires_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class ArchivedAttachment:
    """An attachment that now exists in content-addressed storage."""

    sha256: Sha256
    size_bytes: int
    media_type: str
    storage_key: str
    source_filename: str
    source_url_expires_at: dt.datetime | None = None
    extracted_status: ExtractedStatus = ExtractedStatus.NOT_SUPPORTED


@dataclass(frozen=True, slots=True)
class CaptureCommand:
    """An intent to capture one message."""

    workspace_id: WorkspaceId
    author_user_id: UserId
    source: CaptureSource
    source_message_id: str
    body: str
    client_created_at: dt.datetime
    client_timezone: str
    source_channel_id: str | None = None
    attachments: tuple[AttachmentCandidate, ...] = ()
    content_language: str = "en"
    correction_of: ThoughtId | None = None

    def __post_init__(self) -> None:
        if self.client_created_at.tzinfo is None:
            raise InvalidCaptureCommand(
                "client_created_at must be timezone-aware; a naive timestamp cannot be "
                "resolved to a local calendar date"
            )
        if not self.source_message_id:
            raise InvalidCaptureCommand("source_message_id is required for idempotency")
        try:
            ZoneInfo(self.client_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidCaptureCommand(
                f"{self.client_timezone!r} is not a known IANA timezone"
            ) from exc

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to record at all."""
        return not self.body.strip() and not self.attachments


@dataclass(frozen=True, slots=True)
class ThoughtDraft:
    """A fully-derived thought, ready to append.

    The local calendar fields are derived once, here, so that every downstream
    consumer agrees on which day a thought belongs to.
    """

    workspace_id: WorkspaceId
    author_user_id: UserId
    source: CaptureSource
    source_message_id: str
    source_channel_id: str | None
    body: str
    client_created_at: dt.datetime
    client_timezone: str
    client_local_date: dt.date
    client_local_time: dt.time
    content_language: str
    correction_of: ThoughtId | None
    attachments: tuple[ArchivedAttachment, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class AppendOutcome:
    """The result of appending a draft.

    ``created`` is False when the unique constraint on
    ``(source, source_message_id)`` matched an existing row, which is the normal
    outcome of a Discord redelivery.
    """

    thought_id: ThoughtId
    created: bool


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """What the transport acknowledges to the user."""

    thought_id: ThoughtId
    deduplicated: bool
    attachment_count: int


def local_calendar_fields(instant: dt.datetime, timezone: str) -> tuple[dt.date, dt.time]:
    """Resolve an instant to its local calendar date and wall-clock time.

    Raw events keep their true local date and time so that date and time-of-day
    search behave the way the person remembers them (docs/DESIGN.md 4.2).
    """
    local = instant.astimezone(ZoneInfo(timezone))
    return local.date(), local.time()

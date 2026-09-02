"""Read access to the canonical log.

Separate from the append-only repository: reads carry filters, pagination, and
joins that have nothing to do with the write path, and mixing them would make
the write contract harder to reason about.

Every query is workspace-scoped. The workspace comes from authentication, never
from an untrusted parameter (docs/DESIGN.md 10).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.organize import WindowThought
from tc_infrastructure.db.tables import blobs, thought_attachments, thoughts

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    sha256: str
    media_type: str
    size_bytes: int
    source_filename: str
    extracted_status: str


@dataclass(frozen=True, slots=True)
class ThoughtRecord:
    id: ThoughtId
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
    correction_of: ThoughtId | None
    attachments: tuple[AttachmentRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ThoughtPage:
    items: tuple[ThoughtRecord, ...]
    next_cursor: str | None


class PostgresThoughtReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, workspace_id: WorkspaceId, thought_id: ThoughtId) -> ThoughtRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(thoughts).where(
                        thoughts.c.id == thought_id,
                        thoughts.c.workspace_id == workspace_id,
                    )
                )
            ).first()
            if row is None:
                return None
            attachments = await self._attachments_for(session, [ThoughtId(row.id)])

        return _to_record(row, attachments.get(ThoughtId(row.id), ()))

    async def list_thoughts(
        self,
        workspace_id: WorkspaceId,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        local_date_from: dt.date | None = None,
        local_date_to: dt.date | None = None,
        source: str | None = None,
    ) -> ThoughtPage:
        """Newest first, keyset-paginated on the identity column.

        Keyset rather than OFFSET: the log only grows at the head, so an offset
        would shift under a reader paging through it.

        Named ``list_thoughts`` rather than ``list``: a method called ``list``
        shadows the builtin inside the class body, which silently breaks every
        ``list[...]`` annotation in the same class.
        """
        limit = max(1, min(limit, MAX_PAGE_SIZE))

        conditions = [thoughts.c.workspace_id == workspace_id]
        if cursor is not None:
            conditions.append(thoughts.c.id < _decode_cursor(cursor))
        if local_date_from is not None:
            conditions.append(thoughts.c.client_local_date >= local_date_from)
        if local_date_to is not None:
            conditions.append(thoughts.c.client_local_date <= local_date_to)
        if source is not None:
            conditions.append(thoughts.c.source == source)

        # One extra row tells us whether another page exists without a count.
        statement = (
            sa.select(thoughts).where(*conditions).order_by(thoughts.c.id.desc()).limit(limit + 1)
        )

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            attachments = await self._attachments_for(session, [ThoughtId(row.id) for row in rows])

        items = tuple(_to_record(row, attachments.get(ThoughtId(row.id), ())) for row in rows)
        next_cursor = _encode_cursor(items[-1].id) if has_more and items else None
        return ThoughtPage(items=items, next_cursor=next_cursor)

    async def first_capture_at(self, workspace_id: WorkspaceId) -> dt.datetime | None:
        """When this workspace's oldest thought was captured, or ``None`` if empty.

        Anchors the scheduler's catch-up for a workspace that has never been
        organized (``tc_worker.scheduler.OrganizeScheduler.catch_up``).
        """
        async with self._session_factory() as session:
            earliest: dt.datetime | None = await session.scalar(
                sa.select(sa.func.min(thoughts.c.client_created_at)).where(
                    thoughts.c.workspace_id == workspace_id
                )
            )
        return earliest

    async def list_between(
        self, workspace_id: WorkspaceId, start: dt.datetime, end: dt.datetime
    ) -> list[WindowThought]:
        """Every thought in ``(start, end]``, for the organize pipeline (docs/DESIGN.md 7.2).

        Unbounded, unlike ``list_thoughts``: a capture window is organized as
        a whole, not paged. docs/DESIGN.md 14.1 calls for deterministic
        chunking with overlap on a very large window; that is not implemented
        yet, so an exceptionally large window is read in full rather than
        silently truncated - a caller that wants to guard against one is
        expected to check the returned count itself.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(
                        thoughts.c.id,
                        thoughts.c.body,
                        thoughts.c.client_local_date,
                        thoughts.c.client_local_time,
                    )
                    .where(
                        thoughts.c.workspace_id == workspace_id,
                        thoughts.c.client_created_at > start,
                        thoughts.c.client_created_at <= end,
                    )
                    .order_by(thoughts.c.client_created_at, thoughts.c.id)
                )
            ).all()
        return [
            WindowThought(
                id=ThoughtId(row.id),
                body=row.body,
                client_local_date=row.client_local_date.isoformat(),
                client_local_time=row.client_local_time.isoformat(),
            )
            for row in rows
        ]

    async def _attachments_for(
        self, session: AsyncSession, thought_ids: list[ThoughtId]
    ) -> dict[ThoughtId, tuple[AttachmentRecord, ...]]:
        """One query for the whole page rather than one per row."""
        if not thought_ids:
            return {}

        rows = (
            await session.execute(
                sa.select(
                    thought_attachments.c.thought_id,
                    thought_attachments.c.blob_sha256,
                    thought_attachments.c.source_filename,
                    thought_attachments.c.extracted_status,
                    blobs.c.media_type,
                    blobs.c.size_bytes,
                )
                .join(blobs, blobs.c.sha256 == thought_attachments.c.blob_sha256)
                .where(thought_attachments.c.thought_id.in_(thought_ids))
                .order_by(
                    thought_attachments.c.thought_id,
                    thought_attachments.c.source_filename,
                )
            )
        ).all()

        grouped: dict[ThoughtId, list[AttachmentRecord]] = {}
        for row in rows:
            grouped.setdefault(ThoughtId(row.thought_id), []).append(
                AttachmentRecord(
                    sha256=row.blob_sha256,
                    media_type=row.media_type,
                    size_bytes=row.size_bytes,
                    source_filename=row.source_filename,
                    extracted_status=row.extracted_status,
                )
            )
        return {key: tuple(value) for key, value in grouped.items()}


def _to_record(row: sa.Row[Any], attachments: tuple[AttachmentRecord, ...]) -> ThoughtRecord:
    return ThoughtRecord(
        id=ThoughtId(row.id),
        source=row.source,
        source_message_id=row.source_message_id,
        source_channel_id=row.source_channel_id,
        body=row.body,
        client_created_at=row.client_created_at,
        client_timezone=row.client_timezone,
        client_local_date=row.client_local_date,
        client_local_time=row.client_local_time,
        received_at=row.received_at,
        content_language=row.content_language,
        correction_of=ThoughtId(row.correction_of) if row.correction_of is not None else None,
        attachments=attachments,
    )


def _encode_cursor(thought_id: ThoughtId) -> str:
    """Opaque to callers, so the pagination key can change without breaking them."""
    return uuid.uuid5(uuid.NAMESPACE_OID, str(thought_id)).hex[:8] + f".{thought_id}"


def _decode_cursor(cursor: str) -> int:
    try:
        return int(cursor.rsplit(".", 1)[1])
    except (IndexError, ValueError) as exc:
        raise InvalidCursorError(f"{cursor!r} is not a valid pagination cursor") from exc


class InvalidCursorError(ValueError):
    """The supplied pagination cursor could not be interpreted."""

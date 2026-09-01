"""PostgreSQL implementation of the append-only thought repository.

Implements the contract stated in ``tc_domain.ports.ThoughtRepository``: the
thought, its blobs, its attachment links, and its outbox event all commit in one
transaction, or none of them do. That is what makes "acknowledge only after
durable commit" (docs/DESIGN.md 3.1) true rather than aspirational.
"""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import (
    AppendOutcome,
    CaptureSource,
    ThoughtDraft,
    ThoughtId,
    WorkspaceId,
)
from tc_infrastructure.db.tables import blobs, outbox_events, thought_attachments, thoughts

THOUGHT_CAPTURED_EVENT = "thought.captured"

# The bot acknowledges on the fast path, immediately after this transaction
# commits, and then marks the event delivered. The outbox preserves the work
# when that does not happen - the process died, or Discord was unreachable -
# although the persistent retry runtime is not wired yet.
# Holding the event back briefly keeps the safety net from racing the fast path
# and sending a second acknowledgement for a message that already got one.
ACK_DELIVERY_GRACE = dt.timedelta(seconds=60)


class PostgresThoughtRepository:
    """Append-only access to the canonical log, backed by PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_id_by_source_message(
        self,
        workspace_id: WorkspaceId,
        source: CaptureSource,
        source_message_id: str,
    ) -> ThoughtId | None:
        """Advisory fast path for redelivery; the constraint is authoritative."""
        async with self._session_factory() as session:
            found = await session.scalar(
                sa.select(thoughts.c.id).where(
                    thoughts.c.workspace_id == workspace_id,
                    thoughts.c.source == str(source),
                    thoughts.c.source_message_id == source_message_id,
                )
            )
        return ThoughtId(found) if found is not None else None

    async def append(self, draft: ThoughtDraft) -> AppendOutcome:
        """Commit the thought and everything that must be true alongside it."""
        async with self._session_factory() as session, session.begin():
            thought_id = await self._insert_thought(session, draft)
            if thought_id is None:
                # The unique constraint matched a concurrent or earlier
                # delivery. Return that row rather than raising: one Discord
                # message means one thought and one acknowledgement.
                existing = await session.scalar(
                    sa.select(thoughts.c.id).where(
                        thoughts.c.workspace_id == draft.workspace_id,
                        thoughts.c.source == str(draft.source),
                        thoughts.c.source_message_id == draft.source_message_id,
                    )
                )
                if existing is None:  # pragma: no cover - would mean the row vanished
                    raise RuntimeError(
                        "insert conflicted but the conflicting thought could not be read; "
                        "the canonical log is append-only, so this should be impossible"
                    )
                return AppendOutcome(thought_id=ThoughtId(existing), created=False)

            await self._link_attachments(session, thought_id, draft)
            await self._enqueue_captured_event(session, thought_id, draft)

        return AppendOutcome(thought_id=thought_id, created=True)

    async def _insert_thought(self, session: AsyncSession, draft: ThoughtDraft) -> ThoughtId | None:
        """Insert the thought, returning None when it already exists.

        ``ON CONFLICT DO NOTHING`` rather than a pre-check: two workers handling
        the same redelivery would both pass a pre-check.
        """
        statement = (
            pg_insert(thoughts)
            .values(
                workspace_id=draft.workspace_id,
                author_user_id=draft.author_user_id,
                source=str(draft.source),
                source_message_id=draft.source_message_id,
                source_channel_id=draft.source_channel_id,
                body=draft.body,
                client_created_at=draft.client_created_at,
                client_timezone=draft.client_timezone,
                client_local_date=draft.client_local_date,
                client_local_time=draft.client_local_time,
                content_language=draft.content_language,
                correction_of=draft.correction_of,
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "source", "source_message_id"])
            .returning(thoughts.c.id)
        )
        inserted = await session.scalar(statement)
        return ThoughtId(inserted) if inserted is not None else None

    async def _link_attachments(
        self, session: AsyncSession, thought_id: ThoughtId, draft: ThoughtDraft
    ) -> None:
        for attachment in draft.attachments:
            # Blobs are content-addressed, so the same bytes arriving twice is
            # normal and not an error.
            await session.execute(
                pg_insert(blobs)
                .values(
                    sha256=attachment.sha256,
                    size_bytes=attachment.size_bytes,
                    media_type=attachment.media_type,
                    storage_key=attachment.storage_key,
                )
                .on_conflict_do_nothing(index_elements=["sha256"])
            )
            await session.execute(
                pg_insert(thought_attachments)
                .values(
                    thought_id=thought_id,
                    # Carried explicitly so the composite foreign key can check
                    # that the link and its thought share a workspace.
                    workspace_id=draft.workspace_id,
                    blob_sha256=attachment.sha256,
                    source_filename=attachment.source_filename,
                    source_url_expires_at=attachment.source_url_expires_at,
                    extracted_status=str(attachment.extracted_status),
                )
                .on_conflict_do_nothing(
                    index_elements=["thought_id", "blob_sha256", "source_filename"]
                )
            )

    async def _enqueue_captured_event(
        self, session: AsyncSession, thought_id: ThoughtId, draft: ThoughtDraft
    ) -> None:
        """Record the intent to acknowledge, inside the same transaction.

        The payload carries identifiers and shapes only. Body text stays in
        ``thoughts``; copying it here would put personal content into a queue
        that is logged and retried (docs/DESIGN.md 14.2).
        """
        await session.execute(
            sa.insert(outbox_events).values(
                id=uuid.uuid4(),
                workspace_id=draft.workspace_id,
                event_type=THOUGHT_CAPTURED_EVENT,
                aggregate_id=str(thought_id),
                available_at=sa.func.now() + ACK_DELIVERY_GRACE,
                payload={
                    "thought_id": thought_id,
                    "source": str(draft.source),
                    "source_message_id": draft.source_message_id,
                    "source_channel_id": draft.source_channel_id,
                    "attachment_count": len(draft.attachments),
                },
            )
        )

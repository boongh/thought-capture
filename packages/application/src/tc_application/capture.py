"""The capture use case.

This is the critical path of the whole product: "Build a self-hosted personal
memory system whose critical path is Discord capture, not organization"
(docs/DESIGN.md 1). It must be fast, idempotent, and must never acknowledge
anything it has not durably committed.
"""

from __future__ import annotations

import logging

from tc_domain.capture import (
    CaptureCommand,
    CaptureResult,
    ThoughtDraft,
    local_calendar_fields,
)
from tc_domain.errors import CaptureRejected
from tc_domain.policy import AttachmentPolicy
from tc_domain.ports import AttachmentArchive, ThoughtRepository

logger = logging.getLogger(__name__)


class CaptureThought:
    """Record one message as an immutable thought.

    Ordering is deliberate:

    1. Reject an empty message before doing any work.
    2. Validate every attachment against policy *before* downloading anything,
       so an oversized or blocked file costs no bandwidth.
    3. Short-circuit a known redelivery, so retries do not re-download.
    4. Archive attachments, then commit. If the commit fails, the blobs are left
       unreferenced and reclaimed later by the garbage collector; nothing is
       acknowledged (docs/DESIGN.md 7.1).
    """

    def __init__(
        self,
        thoughts: ThoughtRepository,
        archive: AttachmentArchive,
        attachment_policy: AttachmentPolicy,
    ) -> None:
        self._thoughts = thoughts
        self._archive = archive
        self._policy = attachment_policy

    async def execute(self, command: CaptureCommand) -> CaptureResult:
        if command.is_empty:
            raise CaptureRejected("message has neither text nor attachments")

        for candidate in command.attachments:
            self._policy.validate(candidate)

        existing_id = await self._thoughts.find_id_by_source_message(
            command.workspace_id, command.source, command.source_message_id
        )
        if existing_id is not None:
            # Normal for a Discord redelivery. The same acknowledgement is
            # returned so the user sees one consistent thought ID.
            logger.info(
                "capture.deduplicated",
                extra={
                    "thought_id": existing_id,
                    "source": str(command.source),
                    "source_message_id": command.source_message_id,
                },
            )
            return CaptureResult(
                thought_id=existing_id,
                deduplicated=True,
                attachment_count=len(command.attachments),
            )

        archived = tuple(
            [await self._archive.archive(candidate) for candidate in command.attachments]
        )

        local_date, local_time = local_calendar_fields(
            command.client_created_at, command.client_timezone
        )

        draft = ThoughtDraft(
            workspace_id=command.workspace_id,
            author_user_id=command.author_user_id,
            source=command.source,
            source_message_id=command.source_message_id,
            source_channel_id=command.source_channel_id,
            body=command.body,
            client_created_at=command.client_created_at,
            client_timezone=command.client_timezone,
            client_local_date=local_date,
            client_local_time=local_time,
            content_language=command.content_language,
            correction_of=command.correction_of,
            attachments=archived,
        )

        outcome = await self._thoughts.append(draft)

        # Log identifiers and shapes only. Never the body (docs/DESIGN.md 14.2).
        logger.info(
            "capture.committed" if outcome.created else "capture.deduplicated",
            extra={
                "thought_id": outcome.thought_id,
                "source": str(command.source),
                "attachment_count": len(archived),
                "body_length": len(command.body),
            },
        )

        return CaptureResult(
            thought_id=outcome.thought_id,
            deduplicated=not outcome.created,
            attachment_count=len(archived),
        )

"""The raw capture log: append and read.

Raw thoughts are append-only, so there is no PUT, PATCH, or DELETE here and
never will be. A correction is a new thought that points at the one it corrects
(ADR-0001).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Header, Query, status

from tc_api.dependencies import Authenticated, Context
from tc_api.problems import bad_request, not_found, unprocessable
from tc_api.schemas import (
    CaptureRequest,
    CaptureResponse,
    ThoughtListResponse,
    ThoughtResponse,
)
from tc_domain.capture import CaptureCommand, CaptureSource, ThoughtId
from tc_domain.errors import CaptureRejected, InvalidCaptureCommand
from tc_infrastructure.db.thought_reader import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidCursorError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/thoughts", tags=["thoughts"], dependencies=[Authenticated])


@router.post(
    "",
    response_model=CaptureResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Capture a thought from the API or an import",
)
async def capture(
    payload: CaptureRequest,
    context: Context,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Required. Replaying the same key returns the original thought.",
        ),
    ] = None,
) -> CaptureResponse:
    if not idempotency_key or not idempotency_key.strip():
        raise bad_request(
            "idempotency-key-required",
            "an Idempotency-Key header is required so that a retry cannot create a duplicate",
        )

    timezone = payload.client_timezone or context.settings.workspace_timezone
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise unprocessable(
            "unknown-timezone", f"{timezone!r} is not a known IANA timezone"
        ) from exc

    created_at = payload.client_created_at or dt.datetime.now(dt.UTC)
    if created_at.tzinfo is None:
        raise unprocessable(
            "naive-timestamp",
            "client_created_at must carry a UTC offset; a naive timestamp cannot be "
            "resolved to a local calendar date",
        )

    try:
        command = CaptureCommand(
            workspace_id=context.workspace_id,
            author_user_id=context.user_id,
            source=CaptureSource.API,
            source_message_id=idempotency_key.strip(),
            body=payload.body,
            client_created_at=created_at,
            client_timezone=timezone,
            content_language=payload.content_language,
            correction_of=ThoughtId(payload.correction_of)
            if payload.correction_of is not None
            else None,
        )
    except InvalidCaptureCommand as exc:
        raise unprocessable("invalid-capture", str(exc)) from exc

    try:
        result = await context.capture.execute(command)
    except CaptureRejected as exc:
        raise unprocessable("capture-rejected", str(exc)) from exc

    return CaptureResponse(
        thought_id=int(result.thought_id),
        deduplicated=result.deduplicated,
        attachment_count=result.attachment_count,
    )


@router.get("", response_model=ThoughtListResponse, summary="Paginated raw log, newest first")
async def list_thoughts(
    context: Context,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page")] = None,
    date_from: Annotated[
        dt.date | None, Query(alias="from", description="Local calendar date, inclusive")
    ] = None,
    date_to: Annotated[
        dt.date | None, Query(alias="to", description="Local calendar date, inclusive")
    ] = None,
    source: Annotated[CaptureSource | None, Query()] = None,
) -> ThoughtListResponse:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise bad_request("invalid-range", "`from` must not be later than `to`")

    try:
        page = await context.reader.list_thoughts(
            context.workspace_id,
            limit=limit,
            cursor=cursor,
            local_date_from=date_from,
            local_date_to=date_to,
            source=str(source) if source is not None else None,
        )
    except InvalidCursorError as exc:
        raise bad_request("invalid-cursor", str(exc)) from exc

    return ThoughtListResponse.of(page)


@router.get(
    "/{thought_id}",
    response_model=ThoughtResponse,
    summary="One raw thought and its archival attachment metadata",
)
async def get_thought(thought_id: int, context: Context) -> ThoughtResponse:
    record = await context.reader.get(context.workspace_id, ThoughtId(thought_id))
    if record is None:
        # Same response whether it does not exist or belongs to another
        # workspace: existence is not something to leak across a boundary.
        raise not_found(f"no thought {thought_id} in this workspace")
    return ThoughtResponse.of(record)

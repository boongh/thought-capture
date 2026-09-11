"""Operator-triggered maintenance actions (docs/DESIGN.md 10)."""

from __future__ import annotations

from fastapi import APIRouter

from tc_api.dependencies import Authenticated, Context
from tc_api.schemas import EmbeddingSyncResponse, KhojSyncResponse

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Authenticated])


@router.post(
    "/khoj-sync",
    response_model=KhojSyncResponse,
    summary="Force current-document export/index sync",
)
async def force_khoj_sync(context: Context) -> KhojSyncResponse:
    enqueued = await context.force_khoj_sync(context.workspace_id)
    return KhojSyncResponse(enqueued=enqueued)


@router.post(
    "/embedding-sync",
    response_model=EmbeddingSyncResponse,
    summary="Force current-document embedding sync",
)
async def force_embedding_sync(context: Context) -> EmbeddingSyncResponse:
    enqueued = await context.force_embedding_sync(context.workspace_id)
    return EmbeddingSyncResponse(enqueued=enqueued)

"""Operator-triggered maintenance actions (docs/DESIGN.md 10)."""

from __future__ import annotations

from fastapi import APIRouter

from tc_api.dependencies import Authenticated, Context, ReembedUseCase
from tc_api.problems import service_unavailable
from tc_api.schemas import EmbeddingSyncResponse, KhojSyncResponse, ReembedResponse
from tc_domain.embedding_ports import EmbeddingUnavailableError

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


@router.post(
    "/reembed",
    response_model=ReembedResponse,
    summary="Begin a tracked embedding-model migration sweep",
)
async def start_reembed(context: Context, use_case: ReembedUseCase) -> ReembedResponse:
    """F15-A (docs/plans/embedding-sync-review-round-4.md): separate from
    ``/embedding-sync`` above - this wipes the workspace's existing
    ``document_embeddings`` and records a tracked ``runs(kind='reembed')``
    row targeting whatever model the sidecar currently serves, replacing the
    former hand-run ``DELETE`` runbook step (docs/incidents/0001).
    Idempotent while a sweep is already running for this workspace: returns
    that run unchanged rather than erroring or starting a second one.

    ``context.workspace_id`` only, never a request parameter (docs/DESIGN.md
    10's workspace-scoping requirement) - there is deliberately no field on
    this endpoint's request for it, since there is no request body at all.
    """
    try:
        run = await use_case(context.workspace_id)
    except EmbeddingUnavailableError as exc:
        raise service_unavailable("the embedding sidecar is not reachable") from exc
    return ReembedResponse(
        run_id=run.run_id, enqueued=run.enqueued, embedding_model_id=run.embedding_model_id
    )

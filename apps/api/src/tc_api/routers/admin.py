"""Operator-triggered maintenance actions (docs/DESIGN.md 10).

Unlike search/ask, a sync the operator explicitly asked for should fail
loudly rather than degrade silently - there is no partial-success shape for
"I asked you to sync and you didn't".
"""

from __future__ import annotations

from fastapi import APIRouter

from tc_api.dependencies import Authenticated, Context
from tc_api.problems import service_unavailable
from tc_api.schemas import KhojSyncResponse
from tc_domain.khoj_ports import KhojUnavailableError

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Authenticated])


@router.post(
    "/khoj-sync",
    response_model=KhojSyncResponse,
    summary="Force a full re-export of current documents into Khoj (docs/adr/0010)",
)
async def khoj_sync(context: Context) -> KhojSyncResponse:
    try:
        result = await context.khoj_sync(context.workspace_id)
    except KhojUnavailableError as exc:
        raise service_unavailable(f"Khoj could not be synced: {exc}") from exc
    return KhojSyncResponse.of(result)

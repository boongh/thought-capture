"""Ask over generated documents via Khoj (docs/DESIGN.md 7.6, docs/adr/0010).

Never errors when Ask is unavailable: ``AskResponse.enabled``/``.degraded``
carry that explicitly (docs/DESIGN.md 7.5's degrade-explicit contract), so a
caller can tell "not configured" from "configured but Khoj could not answer"
from "answered, no relevant notes found" without parsing prose.
"""

from __future__ import annotations

from fastapi import APIRouter

from tc_api.dependencies import Authenticated, Context
from tc_api.schemas import AskRequest, AskResponse

router = APIRouter(prefix="/v1/ask", tags=["ask"], dependencies=[Authenticated])


@router.post("", response_model=AskResponse, summary="Ask a question over generated documents")
async def ask_question(request: AskRequest, context: Context) -> AskResponse:
    answer = await context.ask(context.workspace_id, request.q)
    return AskResponse.of(answer)

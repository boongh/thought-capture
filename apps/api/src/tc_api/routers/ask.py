"""Ask over generated documents via Khoj (docs/DESIGN.md 7.6, docs/adr/0003's
"Ask proxy" amendment).

Streams the response (docs/DESIGN.md 7.6: "The gateway streams the answer")
as newline-delimited JSON (one ``AskEvent`` per line) rather than a single
buffered JSON body - the first cut of this endpoint buffered the whole
answer, which independent review correctly flagged as a deviation from the
accepted contract, not a stylistic choice. Never errors when Ask is
unavailable: every ``AskEvent`` carries ``enabled``/``degraded``/
``strict_unsupported`` explicitly (docs/DESIGN.md 7.5's degrade-explicit
contract), so a caller can tell "not configured" from "configured but Khoj
could not answer" from "filters requested, exact-search fallback instead"
from "answered, no relevant notes found" without parsing prose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from tc_api.dependencies import Authenticated, Context
from tc_api.schemas import AskEvent, AskRequest
from tc_domain.search import SearchQuery

router = APIRouter(prefix="/v1/ask", tags=["ask"], dependencies=[Authenticated])

_MEDIA_TYPE = "application/x-ndjson"


@router.post(
    "",
    summary="Ask a question over generated documents (streamed, newline-delimited JSON)",
    description=f"Each response line is one `AskEvent` JSON object, media type `{_MEDIA_TYPE}`.",
    response_class=StreamingResponse,
)
async def ask_question(request: AskRequest, context: Context) -> StreamingResponse:
    query = SearchQuery(
        q=request.q,
        phrase=request.phrase,
        include=tuple(request.include),
        exclude=tuple(request.exclude),
        entity_id=request.entity_id,
        kind=request.kind,
        source=request.source,
        date_from=request.date_from,
        date_to=request.date_to,
        local_time_from=request.local_time_from,
        local_time_to=request.local_time_to,
    )

    async def events() -> AsyncIterator[bytes]:
        async for chunk in context.ask(context.workspace_id, query):
            line = AskEvent.of(chunk).model_dump_json()
            yield f"{line}\n".encode()

    return StreamingResponse(events(), media_type=_MEDIA_TYPE)

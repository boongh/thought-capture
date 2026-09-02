"""Operator debug pages: plain HTML views over the same readers /v1 uses.

docs/adr/0009 (amended 2026-09-02 to add `/debug/runs`): read-only, fixed
pages, no search, no Ask, no write path.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from tc_api import debug_templates
from tc_api.dependencies import Context, DebugAuthenticated
from tc_infrastructure.db.llm_call_reader import DEFAULT_PAGE_SIZE as RUNS_PAGE_SIZE
from tc_infrastructure.db.thought_reader import DEFAULT_PAGE_SIZE

router = APIRouter(prefix="/debug", tags=["debug"], dependencies=[DebugAuthenticated])

DIGEST_KIND = "daily_digest"

# These pages render raw thoughts and generated documents - personal memory
# content. A shared or corporate proxy caching that content on disk would be
# as much a disclosure as logging it; `no-store` forbids any cache (browser
# or intermediary) from retaining the response at all.
_NO_STORE = {"Cache-Control": "no-store"}


def _html(content: str) -> HTMLResponse:
    return HTMLResponse(content=content, headers=_NO_STORE)


@router.get("/thoughts", response_class=HTMLResponse, summary="Raw capture log, with metadata")
async def debug_thoughts(
    context: Context, cursor: Annotated[str | None, Query()] = None
) -> HTMLResponse:
    page = await context.reader.list_thoughts(
        context.workspace_id, limit=DEFAULT_PAGE_SIZE, cursor=cursor
    )
    return _html(debug_templates.thoughts_page(list(page.items), next_cursor=page.next_cursor))


@router.get("/entities", response_class=HTMLResponse, summary="Resolved entities and aliases")
async def debug_entities(context: Context) -> HTMLResponse:
    records = await context.entities.list_entities(context.workspace_id)
    return _html(debug_templates.entities_page(records))


@router.get("/digests", response_class=HTMLResponse, summary="Generated daily digests")
async def debug_digests(context: Context) -> HTMLResponse:
    summaries = await context.documents.list_documents(context.workspace_id, kind=DIGEST_KIND)
    bodies = {}
    for summary in summaries:
        detail = await context.documents.get_document(context.workspace_id, summary.id)
        if detail is not None:
            bodies[summary.id] = detail.body_markdown
    return _html(debug_templates.digests_page(summaries, bodies))


@router.get("/runs", response_class=HTMLResponse, summary="Recent journaled LLM calls")
async def debug_runs(
    context: Context, cursor: Annotated[str | None, Query()] = None
) -> HTMLResponse:
    page = await context.llm_calls.list_llm_calls(
        context.workspace_id, limit=RUNS_PAGE_SIZE, cursor=cursor
    )
    return _html(debug_templates.runs_page(list(page.items), next_cursor=page.next_cursor))

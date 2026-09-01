"""Operator debug pages: plain HTML views over the same readers /v1 uses.

docs/adr/0009. Not the future unified UI (docs/DESIGN.md 4.4): read-only,
three fixed pages, no search, no Ask, no write path.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from tc_api import debug_templates
from tc_api.dependencies import Context, DebugAuthenticated
from tc_infrastructure.db.thought_reader import DEFAULT_PAGE_SIZE

router = APIRouter(prefix="/debug", tags=["debug"], dependencies=[DebugAuthenticated])

DIGEST_KIND = "daily_digest"


@router.get("/thoughts", response_class=HTMLResponse, summary="Raw capture log, with metadata")
async def debug_thoughts(
    context: Context,
    cursor: Annotated[str | None, Query()] = None,
    token: Annotated[str | None, Query()] = None,
) -> str:
    page = await context.reader.list_thoughts(
        context.workspace_id, limit=DEFAULT_PAGE_SIZE, cursor=cursor
    )
    return debug_templates.thoughts_page(
        list(page.items), next_cursor=page.next_cursor, token=token
    )


@router.get("/entities", response_class=HTMLResponse, summary="Resolved entities and aliases")
async def debug_entities(context: Context, token: Annotated[str | None, Query()] = None) -> str:
    records = await context.entities.list_entities(context.workspace_id)
    return debug_templates.entities_page(records, token=token)


@router.get("/digests", response_class=HTMLResponse, summary="Generated daily digests")
async def debug_digests(context: Context, token: Annotated[str | None, Query()] = None) -> str:
    summaries = await context.documents.list_documents(context.workspace_id, kind=DIGEST_KIND)
    bodies = {}
    for summary in summaries:
        detail = await context.documents.get_document(context.workspace_id, summary.id)
        if detail is not None:
            bodies[summary.id] = detail.body_markdown
    return debug_templates.digests_page(summaries, bodies, token=token)

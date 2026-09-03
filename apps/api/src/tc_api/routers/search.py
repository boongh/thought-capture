"""Exact, semantic, and hybrid search over generated documents (docs/DESIGN.md 9, 10).

``mode=exact`` (the default), ``semantic``, and ``hybrid`` are all
implemented; an unrecognized mode string still returns a 501 problem document
rather than silently falling back to exact-only results - docs/DESIGN.md 7.5
requires that a degraded or missing channel is always explicit, never an
implied "no memory exists". ``cursor``/keyset pagination applies to
``mode=exact`` only - Khoj's own search API has no equivalent, so
``semantic``/``hybrid`` always return a single page (``next_cursor`` is
always ``null`` for those two modes); a ``cursor`` supplied together with
``semantic``/``hybrid`` is rejected with a 400 problem document rather than
silently ignored, since ``tc_application.search.Search`` would otherwise
advance only the exact channel to a later page while semantic silently
restarted at page one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from tc_api.dependencies import Authenticated, Context
from tc_api.problems import bad_request, not_implemented
from tc_api.schemas import SearchResponse
from tc_application.search import UnsupportedCursorError, UnsupportedSearchModeError
from tc_domain.search import DEFAULT_LIMIT, MAX_LIMIT, SearchQuery

router = APIRouter(prefix="/v1/search", tags=["search"], dependencies=[Authenticated])


@router.get("", response_model=SearchResponse, summary="Exact, semantic, or hybrid search")
async def search(
    context: Context,
    q: Annotated[str | None, Query(description="Free text, English full-text, ranked")] = None,
    mode: Annotated[str, Query(description="'exact' (default), 'semantic', or 'hybrid'")] = "exact",
    phrase: Annotated[str | None, Query(description="Exact, case-insensitive substring")] = None,
    include: Annotated[list[str], Query(description="Required words")] = [],  # noqa: B006
    exclude: Annotated[list[str], Query(description="Forbidden words")] = [],  # noqa: B006
    entity_id: uuid.UUID | None = None,
    kind: Annotated[
        str | None, Query(description="e.g. 'daily_digest', 'project', 'person'")
    ] = None,
    source: str | None = None,
    date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
    local_time_from: dt.time | None = None,
    local_time_to: dt.time | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> SearchResponse:
    query = SearchQuery(
        q=q,
        phrase=phrase,
        include=tuple(include),
        exclude=tuple(exclude),
        entity_id=entity_id,
        kind=kind,
        source=source,
        date_from=date_from,
        date_to=date_to,
        local_time_from=local_time_from,
        local_time_to=local_time_to,
        limit=limit,
        cursor=cursor,
    )
    try:
        page = await context.search(context.workspace_id, query, mode=mode)
    except UnsupportedSearchModeError as exc:
        raise not_implemented("unsupported-search-mode", str(exc)) from exc
    except UnsupportedCursorError as exc:
        raise bad_request("cursor-unsupported-for-mode", str(exc)) from exc
    return SearchResponse.of(page)

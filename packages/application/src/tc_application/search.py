"""Search use case (docs/DESIGN.md 7.5, 10).

``mode="exact"`` delegates to PostgreSQL alone. ``mode="semantic"`` delegates
to Khoj alone, hydrated back into normalized ``SearchResult``s.
``mode="hybrid"`` runs both concurrently and fuses them by Reciprocal Rank
Fusion (``tc_domain.search_fusion.fuse_rrf``). Khoj being unreachable never
raises past this use case: docs/DESIGN.md 7.5 requires an explicit
``degraded=True`` rather than an empty success that implies no memory
exists - semantic degrades to an empty page, hybrid degrades to exact-only.
"""

from __future__ import annotations

import asyncio
import dataclasses

from tc_domain.capture import WorkspaceId
from tc_domain.khoj_ports import KhojPort, KhojUnavailableError
from tc_domain.search import (
    ExactSearchPort,
    SearchPage,
    SearchQuery,
    SearchResult,
    SemanticHydrator,
    text_query_string,
)
from tc_domain.search_fusion import fuse_rrf

SUPPORTED_MODES = ("exact", "semantic", "hybrid")


class UnsupportedSearchModeError(Exception):
    """Raised for a search mode this application does not implement."""


class Search:
    def __init__(self, exact: ExactSearchPort, khoj: KhojPort, hydrate: SemanticHydrator) -> None:
        self._exact = exact
        self._khoj = khoj
        self._hydrate = hydrate

    async def __call__(
        self, workspace_id: WorkspaceId, query: SearchQuery, *, mode: str = "exact"
    ) -> SearchPage:
        if mode not in SUPPORTED_MODES:
            raise UnsupportedSearchModeError(
                f"mode={mode!r} is not implemented; available modes are {SUPPORTED_MODES!r}"
            )
        if mode == "exact":
            return await self._exact.search(workspace_id, query)
        if mode == "semantic":
            return await self._semantic(workspace_id, query)
        return await self._hybrid(workspace_id, query)

    async def _semantic(self, workspace_id: WorkspaceId, query: SearchQuery) -> SearchPage:
        text = text_query_string(query)
        if text is None:
            # No free text to search semantically on - a pure filter/phrase
            # query is an exact-search concern; this is a valid empty answer,
            # not a degradation.
            return SearchPage(items=(), next_cursor=None, degraded=False)
        try:
            items = await self._semantic_results(workspace_id, text, query.limit)
        except KhojUnavailableError:
            return SearchPage(items=(), next_cursor=None, degraded=True)
        return SearchPage(items=items, next_cursor=None, degraded=False)

    async def _hybrid(self, workspace_id: WorkspaceId, query: SearchQuery) -> SearchPage:
        text = text_query_string(query)

        exact_task = asyncio.ensure_future(self._exact.search(workspace_id, query))
        semantic_task = asyncio.ensure_future(
            self._semantic_results_or_none(workspace_id, text, query.limit)
        )
        exact_page, semantic_items = await asyncio.gather(exact_task, semantic_task)

        fused = fuse_rrf(
            exact_page.items,
            semantic_items or (),
            has_phrase_match=query.phrase is not None,
        )
        return SearchPage(items=fused, next_cursor=None, degraded=semantic_items is None)

    async def _semantic_results_or_none(
        self, workspace_id: WorkspaceId, text: str | None, limit: int
    ) -> tuple[SearchResult, ...] | None:
        """``None`` distinguishes "Khoj was unreachable" (degraded) from "no
        free text to search on" / "no matches" (both a valid empty tuple) -
        ``asyncio.gather`` cannot itself carry an exception past a task
        without failing the other one too, so this wrapper turns
        ``KhojUnavailableError`` into a sentinel instead."""
        if text is None:
            return ()
        try:
            return await self._semantic_results(workspace_id, text, limit)
        except KhojUnavailableError:
            return None

    async def _semantic_results(
        self, workspace_id: WorkspaceId, text: str, limit: int
    ) -> tuple[SearchResult, ...]:
        results = await self._khoj.search(text, limit=limit)
        hydrated = await self._hydrate.hydrate(workspace_id, tuple(r.filename for r in results))
        items: list[SearchResult] = []
        for result in results:
            match = hydrated.get(result.filename)
            if match is None:
                continue
            items.append(dataclasses.replace(match, rank=result.score))
        return tuple(items)

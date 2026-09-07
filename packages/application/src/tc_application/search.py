"""Search use case (docs/DESIGN.md 7.5, 10).

``mode="exact"`` delegates to PostgreSQL alone. ``mode="semantic"`` delegates
to Khoj alone, hydrated back into normalized ``SearchResult``s - hydration
also re-enforces every structured filter in trusted PostgreSQL
(``SemanticHydrator.hydrate``'s own contract), since a semantic hit's
relevance score never implies it satisfies them. ``mode="hybrid"`` runs both
concurrently and fuses them by Reciprocal Rank Fusion
(``tc_domain.search_fusion.fuse_rrf``). Khoj being unreachable never raises
past this use case: docs/DESIGN.md 7.5 requires an explicit ``degraded=True``
rather than an empty success that implies no memory exists - semantic
degrades to an empty page, hybrid degrades to exact-only.

Keyset pagination (``query.cursor``) is an exact-search-only concern: Khoj has
no equivalent, so a cursor threaded through hybrid would advance the exact
channel to a later page while semantic silently restarted at its first page
every time, fusing two pages that were never aligned. Rather than allow that
silently, a cursor supplied for ``semantic``/``hybrid`` is rejected outright
(``UnsupportedCursorError``) - docs/DESIGN.md 7.5's filters are "honored or
rejected explicitly", never silently reinterpreted.
"""

from __future__ import annotations

import asyncio
import dataclasses

from tc_domain.capture import WorkspaceId
from tc_domain.khoj_ports import KhojPort, KhojUnavailableError
from tc_domain.search import (
    MAX_LIMIT,
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


class UnsupportedCursorError(Exception):
    """Raised when a pagination cursor is supplied for a mode that cannot
    honor it (``semantic``/``hybrid`` - see module docstring)."""


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
        if mode != "exact" and query.cursor is not None:
            raise UnsupportedCursorError(
                f"mode={mode!r} does not support pagination; a cursor is only valid for "
                "mode='exact'"
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
            items = await self._semantic_results(workspace_id, query, text)
        except KhojUnavailableError:
            return SearchPage(items=(), next_cursor=None, degraded=True)
        return SearchPage(items=items, next_cursor=None, degraded=False)

    async def _hybrid(self, workspace_id: WorkspaceId, query: SearchQuery) -> SearchPage:
        text = text_query_string(query)

        exact_task = asyncio.ensure_future(self._exact.search(workspace_id, query))
        semantic_task = asyncio.ensure_future(
            self._semantic_results_or_none(workspace_id, query, text)
        )
        exact_page, semantic_items = await asyncio.gather(exact_task, semantic_task)

        fused = fuse_rrf(
            exact_page.items,
            semantic_items or (),
            has_phrase_match=query.phrase is not None,
        )
        # Each channel is independently capped to query.limit, but the fused
        # union (deduplicated by revision_id, not truncated by fuse_rrf
        # itself) can hold up to twice that many items when the two channels
        # barely overlap - a realistic case, not an edge case. The page's
        # own limit must still hold after fusion.
        limit = max(1, min(query.limit, MAX_LIMIT))
        return SearchPage(items=fused[:limit], next_cursor=None, degraded=semantic_items is None)

    async def _semantic_results_or_none(
        self, workspace_id: WorkspaceId, query: SearchQuery, text: str | None
    ) -> tuple[SearchResult, ...] | None:
        """``None`` distinguishes "Khoj was unreachable" (degraded) from "no
        free text to search on" / "no matches" (both a valid empty tuple) -
        ``asyncio.gather`` cannot itself carry an exception past a task
        without failing the other one too, so this wrapper turns
        ``KhojUnavailableError`` into a sentinel instead."""
        if text is None:
            return ()
        try:
            return await self._semantic_results(workspace_id, query, text)
        except KhojUnavailableError:
            return None

    async def _semantic_results(
        self, workspace_id: WorkspaceId, query: SearchQuery, text: str
    ) -> tuple[SearchResult, ...]:
        """Deduped by filename before hydrating and again while building the
        results: Khoj can chunk one uploaded document into more than one
        indexed entry and return more than one hit for the same document in
        the same search (its own filename is identical across chunks of the
        same upload), which would otherwise surface the same document twice
        in ``mode="semantic"`` and inflate its RRF contribution in
        ``mode="hybrid"`` (``fuse_rrf`` accumulates one contribution per
        entry in the ``semantic`` tuple it is given). Khoj already orders
        hits by relevance, so keeping the first occurrence keeps the
        best-ranked one.
        """
        results = await self._khoj.search(text, limit=query.limit)
        filenames = tuple(dict.fromkeys(r.filename for r in results))
        hydrated = await self._hydrate.hydrate(workspace_id, query, filenames)

        seen: set[str] = set()
        items: list[SearchResult] = []
        for result in results:
            if result.filename in seen:
                continue
            match = hydrated.get(result.filename)
            if match is None:
                continue
            seen.add(result.filename)
            items.append(dataclasses.replace(match, rank=result.score))
        return tuple(items)

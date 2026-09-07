"""Search use case (docs/DESIGN.md 7.5, 10, docs/adr/0010).

Exact-only originally; this slice adds ``semantic`` (Khoj) and ``hybrid``
(RRF-fused) modes behind the same seam the exact-only version already
established, so a caller asking for hybrid search today is not a breaking
change for anything that called ``Search`` before either mode existed.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import parse_khoj_filename
from tc_domain.khoj_ports import KhojPort, KhojSearchResult, KhojUnavailableError
from tc_domain.search import ExactSearchPort, SearchPage, SearchQuery, SearchResult

SUPPORTED_MODES = ("exact", "semantic", "hybrid")

# docs/DESIGN.md 7.5: "fuse with Reciprocal Rank Fusion (RRF) using k=60".
_RRF_K = 60
# "exact phrase matches receive a documented deterministic boost before rank
# assignment" (docs/DESIGN.md 7.5 item 4): a phrase hit is a verified,
# case-insensitive substring match (search_reader._filter_conditions), which
# is a stronger guarantee than any semantic-similarity score, so it always
# outranks a non-phrase-matching result in a hybrid page.
_PHRASE_BOOST = 1.0


class UnsupportedSearchModeError(Exception):
    """Raised for a search mode this slice does not implement yet."""


class Search:
    def __init__(self, exact: ExactSearchPort, khoj: KhojPort | None = None) -> None:
        self._exact = exact
        # ``None`` when no Khoj adapter is wired for this deployment
        # (docs/adr/0010) - treated identically to a wired-but-unreachable
        # Khoj: semantic/hybrid degrade explicitly rather than erroring.
        self._khoj = khoj

    async def __call__(
        self, workspace_id: WorkspaceId, query: SearchQuery, *, mode: str = "exact"
    ) -> SearchPage:
        if mode not in SUPPORTED_MODES:
            raise UnsupportedSearchModeError(
                f"mode={mode!r} is not implemented; available modes are {SUPPORTED_MODES!r}"
            )
        if mode == "exact":
            return await self._exact.search(workspace_id, query)

        if self._khoj is None:
            if mode == "semantic":
                return SearchPage(items=(), next_cursor=None, degraded=True)
            exact_page = await self._exact.search(workspace_id, query)
            return replace(exact_page, degraded=True)

        if mode == "semantic":
            items, degraded = await self._semantic(workspace_id, query)
            return SearchPage(items=items[: query.limit], next_cursor=None, degraded=degraded)

        return await self._hybrid(workspace_id, query)

    async def _semantic(
        self, workspace_id: WorkspaceId, query: SearchQuery
    ) -> tuple[tuple[SearchResult, ...], bool]:
        assert self._khoj is not None
        text = query.q or query.phrase
        if not text:
            # Khoj has no equivalent to an exact-search filters-only query -
            # nothing to embed is a well-formed empty answer, not a
            # degradation.
            return (), False
        try:
            hits = await self._khoj.search(text, limit=query.limit)
        except KhojUnavailableError:
            return (), True

        # Preserve Khoj's own result order (its rank, not its raw score,
        # feeds RRF fusion in `_hybrid` - see that method's comment).
        # `_documents_in_workspace` already dedupes by document id.
        matched = _documents_in_workspace(workspace_id, hits)
        document_ids = tuple(document_id for document_id, _ in matched)
        hydrated = await self._exact.hydrate(workspace_id, document_ids, query)

        results: list[SearchResult] = []
        for document_id, score in matched:
            base = hydrated.get(document_id)
            if base is None:
                # Indexed in Khoj but no longer the current revision in
                # PostgreSQL (deleted, or superseded since the last
                # `/v1/admin/khoj-sync`) - PostgreSQL remains the source of
                # truth for what is citable (docs/DESIGN.md 8.2), so a stale
                # Khoj hit is dropped rather than surfaced with nothing
                # behind it.
                continue
            results.append(replace(base, channels=("semantic",), rank=score))
        return tuple(results), False

    async def _hybrid(self, workspace_id: WorkspaceId, query: SearchQuery) -> SearchPage:
        exact_page = await self._exact.search(workspace_id, query)
        semantic_items, semantic_degraded = await self._semantic(workspace_id, query)

        # A phrase match is only ever produced by the exact channel
        # (search_reader's ILIKE filter); recorded up front because the
        # fusion loop below overwrites each result's own `channels`.
        phrase_matched_ids = {r.revision_id for r in exact_page.items} if query.phrase else set()

        fused: dict[uuid.UUID, tuple[float, SearchResult]] = {}
        for rank, result in enumerate(exact_page.items, start=1):
            fused[result.revision_id] = (1.0 / (_RRF_K + rank), result)
        for rank, result in enumerate(semantic_items, start=1):
            rrf_score = 1.0 / (_RRF_K + rank)
            existing = fused.get(result.revision_id)
            if existing is None:
                fused[result.revision_id] = (rrf_score, replace(result, channels=("semantic",)))
            else:
                existing_score, existing_result = existing
                fused[result.revision_id] = (
                    existing_score + rrf_score,
                    replace(existing_result, channels=("exact", "semantic")),
                )

        boosted = [
            (score + (_PHRASE_BOOST if revision_id in phrase_matched_ids else 0.0), result)
            for revision_id, (score, result) in fused.items()
        ]
        boosted.sort(key=lambda pair: pair[0], reverse=True)
        limited = boosted[: query.limit]

        items = tuple(replace(result, rank=score) for score, result in limited)
        return SearchPage(
            items=items,
            # RRF pagination would need re-fusing the whole result set on
            # every page rather than a simple keyset offset (docs/adr/0010) -
            # a documented follow-up, not implemented this slice.
            next_cursor=None,
            degraded=semantic_degraded,
        )


def _documents_in_workspace(
    workspace_id: WorkspaceId, hits: tuple[KhojSearchResult, ...]
) -> list[tuple[uuid.UUID, float]]:
    """Parse each hit's filename and keep only ones this workspace actually
    owns, deduped by document id, in Khoj's own returned order.

    docs/adr/0003's single anonymous-instance deployment makes a
    cross-workspace hit unreachable today (Khoj holds no other workspace's
    content to return), but this must never silently trust a filename as a
    security boundary if that ever changes - so a mismatched or unparseable
    filename is dropped, not surfaced.

    Dedup is required, not defensive: Khoj can chunk one uploaded Markdown
    file into more than one indexed "entry" and return several hits sharing
    the same filename (docs/adr/0003's contract spike only ever uploaded one
    small test file and never observed this) - independent review caught
    that without this, the same document could appear twice in a `semantic`
    page, and `_hybrid`'s RRF loop would add a second score term for it
    purely because Khoj split it, inflating its fused rank and mislabeling
    `channels` as ``("exact", "semantic")`` from a same-channel collision
    alone. Only the first (best-ranked, since Khoj already orders by
    relevance) occurrence is kept.
    """
    seen: set[uuid.UUID] = set()
    matched: list[tuple[uuid.UUID, float]] = []
    for hit in hits:
        parts = parse_khoj_filename(hit.filename)
        if parts is None or parts.workspace_id != workspace_id:
            continue
        if parts.document_id in seen:
            continue
        seen.add(parts.document_id)
        matched.append((parts.document_id, hit.score))
    return matched

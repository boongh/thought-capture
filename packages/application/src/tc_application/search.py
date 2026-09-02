"""Search use case (docs/DESIGN.md 7.5, 10).

Exact-only for this slice. Semantic search and RRF fusion are a later slice,
added by giving this the same seam a hybrid orchestrator will use: callers
ask for a ``mode`` and get back a normalized ``SearchPage`` regardless of
which channels actually ran, so ``mode="hybrid"`` becoming real later is not
a breaking change for anything calling ``Search`` today.
"""

from __future__ import annotations

from tc_domain.capture import WorkspaceId
from tc_domain.search import ExactSearchPort, SearchPage, SearchQuery

SUPPORTED_MODES = ("exact",)


class UnsupportedSearchModeError(Exception):
    """Raised for a search mode this slice does not implement yet."""


class Search:
    def __init__(self, exact: ExactSearchPort) -> None:
        self._exact = exact

    async def __call__(
        self, workspace_id: WorkspaceId, query: SearchQuery, *, mode: str = "exact"
    ) -> SearchPage:
        if mode not in SUPPORTED_MODES:
            raise UnsupportedSearchModeError(
                f"mode={mode!r} is not implemented yet; only {SUPPORTED_MODES!r} is "
                "available until the Khoj adapter and hybrid fusion slices land"
            )
        return await self._exact.search(workspace_id, query)

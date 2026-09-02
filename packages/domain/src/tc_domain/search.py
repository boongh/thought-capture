"""Search domain types and the exact-search port (docs/DESIGN.md 9, 10).

Exact search is PostgreSQL-only (docs/DESIGN.md 9.1); semantic search and RRF
fusion (docs/DESIGN.md 7.5) are a later slice. ``SearchQuery``/``SearchPage``
are shaped so that slice adds a second port and a fusing use case without
changing this request/response contract - a caller asking for hybrid search
today and exact search after Khoj ships should not need different types.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from tc_domain.capture import ThoughtId, WorkspaceId

Channel = Literal["exact", "semantic"]

MAX_LIMIT = 100
DEFAULT_LIMIT = 25


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """docs/DESIGN.md 10's ``GET /v1/search`` parameters, normalized.

    ``q`` is free text matched against generated-document titles and bodies
    (English FTS, ranked). ``phrase`` is matched as an exact, case-insensitive
    substring - not tokenized or stemmed - for the "I know I wrote this exact
    sentence" case docs/DESIGN.md 9.1 calls out separately from FTS terms.
    ``include``/``exclude`` are additional required/forbidden words, combined
    with ``q`` the way docs/DESIGN.md 9.1's own worked filters do.

    ``date_from``/``date_to`` and ``local_time_from``/``local_time_to`` filter
    on a *source thought's* own captured time (docs/DESIGN.md 9.1: "B-tree
    predicates on client_created_at, local date, and local time"), not on
    when a document was last revised: a result is included if at least one
    thought it cites falls in the window.
    """

    q: str | None = None
    phrase: str | None = None
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    entity_id: uuid.UUID | None = None
    kind: str | None = None
    source: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    local_time_from: dt.time | None = None
    local_time_to: dt.time | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    result_id: str
    document_id: uuid.UUID
    revision_id: uuid.UUID
    thought_ids: tuple[ThoughtId, ...]
    kind: str
    title: str
    snippet: str
    updated_at: dt.datetime
    entities: tuple[str, ...]
    channels: tuple[Channel, ...]
    rank: float


@dataclass(frozen=True, slots=True)
class SearchPage:
    items: tuple[SearchResult, ...]
    next_cursor: str | None
    degraded: bool = False
    """True only when a requested channel could not run at all (docs/DESIGN.md
    7.5: "it never returns an empty success that implies no memory exists").
    Exact search alone is never degraded - there is no channel to lose."""


@runtime_checkable
class ExactSearchPort(Protocol):
    async def search(self, workspace_id: WorkspaceId, query: SearchQuery) -> SearchPage:
        """PostgreSQL-only precision retrieval (docs/DESIGN.md 9.1).

        Never raises for "no results" - an empty ``items`` tuple is a valid,
        non-degraded answer.
        """
        ...

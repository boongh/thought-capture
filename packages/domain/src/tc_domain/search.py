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


@runtime_checkable
class SemanticHydrator(Protocol):
    async def hydrate(
        self, workspace_id: WorkspaceId, query: SearchQuery, filenames: tuple[str, ...]
    ) -> dict[str, SearchResult]:
        """Resolve Khoj search-result filenames back to normalized ``SearchResult``s.

        Keyed by filename (not ``document_id``) so the caller can re-attach
        each result's own Khoj score without a second lookup. ``query`` is the
        original request's structured filters (kind, source, date/time,
        entity, phrase, required word ``include``, forbidden word
        ``exclude``) - a semantic hit's relevance score never implies it
        satisfies any of them, so every one of them (``q`` alone excepted -
        a semantic hit need not literally contain it, that is what makes
        this search semantic rather than exact) must be
        re-checked here in trusted PostgreSQL, the same way ``ExactSearchPort``
        enforces them for its own channel (docs/DESIGN.md 7.5: filters are
        honored or rejected explicitly, never silently bypassed by a channel
        that cannot itself evaluate them). A filename that does not parse,
        belongs to a different workspace, does not satisfy ``query``'s
        filters, or is no longer Khoj's own acknowledged current revision
        (docs/DESIGN.md 7.5: a stale synced revision must never be relabeled
        as the current one) is simply absent from the returned dict - never
        raised - matching ``ExactSearchPort``'s "no results is a valid answer"
        contract.
        """
        ...


def has_strict_filters(query: SearchQuery) -> bool:
    """True when ``query`` carries a structured filter beyond free text
    (docs/DESIGN.md 7.6, docs/adr/0003's "Ask proxy" amendment).

    ``q`` itself is exempt: for Ask, ``q`` *is* the question, which Khoj's
    chat model must see regardless. Every other field is a precision
    constraint ("only documents of this kind", "only citing this entity")
    that Ask cannot honor today - Khoj's evidence-injection path for
    constraining chat retrieval was never verified (docs/DESIGN.md 7.6,
    docs/adr/0003 "Contract spike findings" item 3) - so a caller (Ask) that
    sees any of these set must not silently query Khoj's whole corpus instead
    (docs/DESIGN.md 7.6: "it does not silently answer from an unconstrained
    corpus").
    """
    return bool(
        query.phrase
        or query.include
        or query.exclude
        or query.entity_id is not None
        or query.kind is not None
        or query.source is not None
        or query.date_from is not None
        or query.date_to is not None
        or query.local_time_from is not None
        or query.local_time_to is not None
    )


def text_query_string(query: SearchQuery) -> str | None:
    """One combined query string from ``q``, ``include``, and ``exclude``.

    Shared by the exact-search full-text query (``websearch_to_tsquery``
    understands the same ``-word`` exclusion syntax) and the semantic-search
    text sent to Khoj, so both channels search for the same thing. ``None``
    when the query carries no free text at all (a pure filter/phrase-only
    query) - a caller must not send an empty string to either channel.
    """
    parts: list[str] = []
    if query.q:
        parts.append(query.q)
    parts.extend(query.include)
    parts.extend(f"-{word}" for word in query.exclude)
    return " ".join(parts) if parts else None

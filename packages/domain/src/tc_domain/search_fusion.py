"""Reciprocal Rank Fusion for hybrid search (docs/DESIGN.md 7.5, 9)."""

from __future__ import annotations

import dataclasses
import uuid

from tc_domain.search import Channel, SearchResult

DEFAULT_K = 60
DEFAULT_PHRASE_BOOST = 1.0


def fuse_rrf(
    exact: tuple[SearchResult, ...],
    semantic: tuple[SearchResult, ...],
    *,
    k: int = DEFAULT_K,
    phrase_boost: float = DEFAULT_PHRASE_BOOST,
    has_phrase_match: bool = False,
) -> tuple[SearchResult, ...]:
    """Normalize each channel's own rank order and fuse by Reciprocal Rank
    Fusion, deduplicating by ``revision_id`` (docs/DESIGN.md 7.5).

    A result present in both channels sums both contributions and carries
    every channel it appeared in. ``has_phrase_match`` adds ``phrase_boost``
    to every exact-channel result once - ``docs/DESIGN.md`` 7.5's "exact
    phrase matches receive a documented deterministic boost." This is sound
    because ``PostgresExactSearch`` ANDs ``phrase`` together with every other
    filter (never ORs), so whenever ``query.phrase`` was set, *every* exact
    result is definitionally a phrase match - there is no "some exact results
    matched by phrase, others didn't" case to distinguish.

    Sort order is fully deterministic: descending fused score, ties broken by
    ``revision_id`` (matching ``search_reader.py``'s own id tie-break), so
    two runs over the same inputs always return the same order.
    """
    scores: dict[uuid.UUID, float] = {}
    channels: dict[uuid.UUID, set[Channel]] = {}
    representative: dict[uuid.UUID, SearchResult] = {}

    for rank, result in enumerate(exact, start=1):
        contribution = 1.0 / (k + rank)
        if has_phrase_match:
            contribution += phrase_boost
        _accumulate(scores, channels, representative, result, contribution)

    for rank, result in enumerate(semantic, start=1):
        contribution = 1.0 / (k + rank)
        _accumulate(scores, channels, representative, result, contribution)

    fused = [
        dataclasses.replace(
            representative[revision_id],
            channels=tuple(sorted(channels[revision_id])),
            rank=score,
        )
        for revision_id, score in scores.items()
    ]
    fused.sort(key=lambda r: (-r.rank, str(r.revision_id)))
    return tuple(fused)


def _accumulate(
    scores: dict[uuid.UUID, float],
    channels: dict[uuid.UUID, set[Channel]],
    representative: dict[uuid.UUID, SearchResult],
    result: SearchResult,
    contribution: float,
) -> None:
    scores[result.revision_id] = scores.get(result.revision_id, 0.0) + contribution
    channels.setdefault(result.revision_id, set()).update(result.channels)
    representative.setdefault(result.revision_id, result)

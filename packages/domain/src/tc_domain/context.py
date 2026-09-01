"""Context assembly for organization (docs/DESIGN.md 7.3).

Pure policy: given the workspace's complete entity-document index and the raw
text of the window being organized, decide which documents' bodies are worth
sending in full. The actual reads (documents, entity mentions, aliases) are
PostgreSQL queries and live in ``tc_infrastructure``; the LLM ``select`` call
is a network round trip and lives in ``tc_application``. This module has
neither, so the deterministic half of assembly is testable without either.

Two invariants from docs/DESIGN.md 7.3.4 are the shape of this module:

- **The index is complete** (invariant 1): every ``Tier1Row`` the caller
  passes in is assumed to already be "every entity document in the
  workspace" - nothing here filters or samples it.
- **Selection is additive** (invariant 2): each signal function returns a set
  of stable keys to *add*; nothing in this module ever removes a key another
  signal already selected.

v1 scope, documented rather than silently dropped (see docs/adr, this
package's callers, and the organize-pipeline slice's commit report): only
``alias``, ``recency``, and ``open_thread`` are implemented here as
deterministic signals. ``cooccurrence`` and the embedding signal
(docs/DESIGN.md 7.3.6, explicitly a "second wave" even in the design itself)
are not - a workspace with few enough entities to fit the whole index in a
prompt (docs/DESIGN.md 7.3.1) does not yet need them to avoid fragmentation,
and adding an unproven signal before ``context_recall`` has a baseline is
exactly what docs/DESIGN.md 7.3.6 warns against ("Enabling every signal at
once makes an underperforming or redundant signal impossible to identify").
Likewise inclusion is binary here (``full`` or ``index_only``) rather than
the design's three-way grade - the ``partial`` truncation mode
(docs/DESIGN.md 7.3.3) is deferred; ``context_recall`` (7.3.5) is unaffected,
since it only distinguishes ``index_only`` from everything else.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from tc_domain.entities import normalize_entity_name

Inclusion = Literal["full", "index_only"]

# Every signal that can add a document to the selected set. Mirrors the
# `signals text[]` column on `run_context_selections` (docs/DESIGN.md 7.3.5).
Signal = Literal["alias", "recency", "open_thread", "selector"]


@dataclass(frozen=True, slots=True)
class ContextAssemblyConfig:
    """Workspace-scoped, versioned with the prompt (docs/DESIGN.md 7.3.6)."""

    max_selected_documents: int = 12
    recency_days: float = 3.0


DEFAULT_CONTEXT_ASSEMBLY_CONFIG = ContextAssemblyConfig()


@dataclass(frozen=True, slots=True)
class Tier1Row:
    """One line of the complete entity-document index (docs/DESIGN.md 7.3.1)."""

    stable_key: str
    entity_type: str
    canonical_name: str
    aliases: tuple[str, ...]
    summary: str
    last_mentioned_at: dt.datetime | None
    open_thread_count: int


@dataclass(frozen=True, slots=True)
class Selection:
    """One document's assembled inclusion, and why."""

    stable_key: str
    inclusion: Inclusion
    signals: tuple[Signal, ...]


def alias_signal(window_text: str, index: tuple[Tier1Row, ...]) -> frozenset[str]:
    """Entities named or misspelled-but-recognizable in the window text.

    A plain normalized-substring check rather than a second trigram query:
    the index is already in memory by the time this runs, and this signal
    exists to catch exact or near-exact naming, not fuzzy recall - fuzzy
    recall is what ``selector`` and, later, the embedding signal are for.
    """
    haystack = normalize_entity_name(window_text)
    matched = set()
    for row in index:
        names = (row.canonical_name, *row.aliases)
        if any(normalize_entity_name(name) in haystack for name in names if name.strip()):
            matched.add(row.stable_key)
    return frozenset(matched)


def recency_signal(
    index: tuple[Tier1Row, ...], *, now: dt.datetime, recency_days: float
) -> frozenset[str]:
    """Entities mentioned recently enough that an unnamed continuation likely means them.

    ``recency_days`` approximates docs/DESIGN.md 7.3.2's "last
    ``context.recency_windows`` windows": a capture window is very close to
    one calendar day (docs/DESIGN.md 4.2), so a day count is the same signal
    without needing to look up how many *windows*, specifically, elapsed.
    """
    if now.tzinfo is None:
        raise ValueError("recency_signal needs a timezone-aware `now`")
    cutoff = now - dt.timedelta(days=recency_days)
    return frozenset(
        row.stable_key
        for row in index
        if row.last_mentioned_at is not None and row.last_mentioned_at >= cutoff
    )


def open_thread_signal(index: tuple[Tier1Row, ...]) -> frozenset[str]:
    """Entities with an unresolved todo or decision attached.

    Standing context, included whether or not the window names them
    (docs/DESIGN.md 7.3.2's ``open_thread`` row).
    """
    return frozenset(row.stable_key for row in index if row.open_thread_count > 0)


def assemble(
    index: tuple[Tier1Row, ...],
    *,
    deterministic: dict[Signal, frozenset[str]],
    selector_keys: frozenset[str] = frozenset(),
    selector_degraded: bool = False,
    config: ContextAssemblyConfig = DEFAULT_CONTEXT_ASSEMBLY_CONFIG,
) -> tuple[tuple[Selection, ...], bool]:
    """Combine every signal, apply the budget, and record why each document was included.

    Returns the per-document selection (covering the *entire* index, so a
    caller can persist ``run_context_selections`` for every document in one
    pass - invariant 6) and whether the run should be marked degraded.

    Budget: at most ``config.max_selected_documents`` get ``full`` inclusion.
    Over budget, the *deterministic* signals win first - alias, recency, and
    open-thread hits are precisely what invariant 2 says the selector can
    never override - and the excess (deterministic overflow or any
    selector-only pick beyond the cap) falls back to ``index_only`` rather
    than being dropped from the index entirely.
    """
    signals_by_key: dict[str, set[Signal]] = {}
    for signal_name, keys in deterministic.items():
        for key in keys:
            signals_by_key.setdefault(key, set()).add(signal_name)
    for key in selector_keys:
        signals_by_key.setdefault(key, set()).add("selector")

    deterministic_keys = {key for keys in deterministic.values() for key in keys}
    # Deterministic hits first, in a stable order; selector-only picks after.
    # Sorting by stable_key makes which documents get truncated under a tight
    # budget reproducible across two runs of the same window, which matters
    # for docs/DESIGN.md 15.3's regression diffs.
    ordered = sorted(signals_by_key, key=lambda key: (key not in deterministic_keys, key))
    full = set(ordered[: config.max_selected_documents])

    selections = tuple(
        Selection(
            stable_key=row.stable_key,
            inclusion="full" if row.stable_key in full else "index_only",
            signals=tuple(sorted(signals_by_key.get(row.stable_key, ()))),
        )
        for row in index
    )
    return selections, selector_degraded
